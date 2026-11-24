"""控制面服务层异常：由 routes_* / app 级 handler 统一映射为 HTTP 状态。

- ``ValidationFailed`` → 422（Issue 数组）
- ``MissingError`` → 404（detail 字符串）
- ``AlreadyExists`` / ``ReferencedByError`` → 409（detail 字符串 / dict）
- ``DefaultGraphRequired`` → 409（detail 为
  ``{"code": "default_graph_required", "tree_id": ...}``）
"""


class ValidationFailed(ValueError):
    def __init__(self, issues):
        super().__init__("configuration validation failed")
        self.issues = list(issues)


class MissingError(Exception):
    """写用例的目标资源不存在（服务抛出，API 映射 404）。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class AlreadyExists(Exception):
    """写用例的目标资源已存在（服务抛出，API 映射 409）。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ReferencedByError(Exception):
    """写用例的目标仍被引用（服务抛出，API 映射 409，detail 直接透传）。"""

    def __init__(self, detail: dict):
        super().__init__(detail.get("message", "referenced"))
        self.detail = detail


class DefaultGraphRequired(Exception):
    """默认图之外仍有启用图时禁止直接停用默认图（服务抛出，API 映射 409）。

    结构化载荷固定为 ``{"code": "default_graph_required", "tree_id": <id>}``：
    客户端必须先调用 ``POST /admin/trees/{id}/default`` 切换默认图。
    """

    def __init__(self, tree_id: str):
        super().__init__("default_graph_required")
        self.tree_id = tree_id
