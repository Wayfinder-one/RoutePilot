"""导入方向回归：低层（控制面/路由/上游/存储/请求日志）不得依赖 API 层。"""
import ast
from pathlib import Path


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
