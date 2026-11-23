"""导入方向回归 + 已移除运行时符号守卫。"""
import ast
from pathlib import Path

#: 严格自包含画布后不得再出现的旧运行时符号（代码、注释与用户文案都算）。
#: 只扫 ``router/``（前端页面、API 与领域层都在这棵树下）；不含迁移脚本与
#: 测试，因为它们允许在文档里描述历史，而生产代码不允许再有这些路径。
FORBIDDEN_RUNTIME_MARKERS = (
    "runtime: null",
    "runtime:null",
    "_materialize",
    "effective_runtime",
    "runtime_for_tree",
    "build_enable_context",
    "JevConfigRepo",
    "/admin/jev/window",
    "_validate_runtime_control",
    "runtime_from_db",
    "read_jev_window",
    "first_jev_settings",
    "effective_jev",
    "effective_window",
    "effective_periods",
    "sync_db_from_runtime",
    "stored_jev_key",
    "_sync_graph_runtimes",
    "_seed_db_from_active_runtime",
    "MODEL_SPECS",
)

#: 窄豁免表（文件, 标记）。每一项都是「严格解析器把被拒绝的旧形态写进报错
#: 文案/注释」——出现只是为了拒绝它，不是仍在使用旧行为。
#:
#: - ``graph.js``：``runtime: null`` / ``runtime:null`` 只出现在旧文档拒绝
#:   分支的注释与 zh/en 错误文案里（导入 legacy 文档会被硬拒绝）。
#: - ``controller.js``：``runtime:null`` 只在「不再发送该控制字段」的注释里。
#:
#: 不要为「仍有旧行为」的代码增加豁免；先改代码，再改这张表。守卫同时断言
#: 每个豁免项仍真实出现，避免删掉发生后留下无意义的宽豁免。
ALLOWED_RUNTIME_MARKERS = {
    ("router/console/routing/graph.js", "runtime: null"),
    ("router/console/routing/graph.js", "runtime:null"),
    ("router/console/routing/controller.js", "runtime:null"),
}


def test_lower_layers_never_import_api():
    roots = [
        Path("router/control"), Path("router/routing"),
        Path("router/upstream"), Path("router/store"),
        Path("router/request_logging.py"),
    ]
    paths = [path for root in roots
             for path in ([root] if root.is_file() else root.rglob("*.py"))]
    offenders = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            else:
                continue
            if any(module == "router.api" or module.startswith("router.api.")
                   for module in modules):
                offenders.append(str(path))
    assert offenders == []


def test_no_legacy_runtime_fallback_symbols():
    hits: set[tuple[str, str]] = set()
    for path in sorted(Path("router").rglob("*")):
        if path.suffix not in {".py", ".js", ".html"}:
            continue
        text = path.read_text(encoding="utf-8")
        for marker in FORBIDDEN_RUNTIME_MARKERS:
            if marker in text:
                hits.add((path.as_posix(), marker))
    unexpected = sorted(hits - ALLOWED_RUNTIME_MARKERS)
    assert unexpected == [], (
        "旧运行时路径的符号仍有残留（改成当前行为，或说明为何必须保留）："
        + ", ".join(f"{path}: {marker}" for path, marker in unexpected))
    stale_allowlist = sorted(ALLOWED_RUNTIME_MARKERS - hits)
    assert stale_allowlist == [], (
        "豁免表里的条目已不存在，请删除以免扩大豁免范围："
        + ", ".join(f"{path}: {marker}" for path, marker in stale_allowlist))
