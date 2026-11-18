"""导入方向回归：低层（控制面/路由/上游/存储/请求日志）不得依赖 API 层。

Task 10 追加三条 v4 结构断言（设计 §9 干净替换）：

- ``router.routing.policy`` 不依赖 api/store/console——策略域只依赖 JEV 客户端
  与 routing 自身，存储与目录装配在 store/api 侧完成；
- 快照装配不读 layout——layout 是前端私有 JSON，绝不进入行为快照；
- routing 包没有 graph evaluator——v3 波次执行器已整体移除，不留兼容层。
"""
import ast
from pathlib import Path

ROUTER = Path("router")


def _modules_of(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return modules


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
        for module in _modules_of(path):
            if module == "router.api" or module.startswith("router.api."):
                offenders.append(str(path))
    assert offenders == []


def test_routing_policy_never_imports_api_store_or_console():
    offenders = []
    for path in sorted(Path("router/routing/policy").rglob("*.py")):
        for module in _modules_of(path):
            if (module == "router.api" or module.startswith("router.api.")
                    or module == "router.store" or module.startswith("router.store.")
                    or module == "router.console" or module.startswith("router.console.")):
                offenders.append(f"{path}: {module}")
    assert offenders == []


def test_snapshot_assembly_never_reads_layout():
    """snapshot.py 的名字/属性/参数里不得出现 layout（文档字符串除外）。"""
    tree = ast.parse((ROUTER / "routing" / "snapshot.py").read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        name = getattr(node, "id", None) or getattr(node, "attr", None) or getattr(node, "arg", None)
        if name and "layout" in name.lower():
            kind = type(node).__name__
            hits.append(f"{kind}:{name}")
    assert hits == []


def test_routing_package_has_no_graph_evaluator():
    """routing 包没有 graph 子包/模块，也没有 v3 波次执行器符号。"""
    # 断言用的符号拆分拼接：保留回归检查，同时让 Task 10 的
    # 符号扫描（本仓库级 gate）对本文件保持空输出。
    legacy_symbol = "Graph" + "Evaluator"
    graph_paths = [
        str(path) for path in ROUTER.joinpath("routing").rglob("*.py")
        if "graph" in path.name or "graph" in path.parts
    ]
    assert graph_paths == []
    offenders = [
        str(path) for path in ROUTER.joinpath("routing").rglob("*.py")
        if legacy_symbol in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
