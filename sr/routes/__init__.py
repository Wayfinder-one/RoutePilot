"""§9 admin 端点按域拆分的 APIRouter（D2/A1：自 sr/server.py 机械迁出）。

依赖方向：sr.server → sr.routes → sr.routes._deps（单向，无反向 import）。
对外行为零变化（路径/状态码/字段/日志逐字保持）。
"""

from __future__ import annotations

from sr.routes.admin_config import router as admin_config
from sr.routes.admin_eval import router as admin_eval
from sr.routes.admin_models import router as admin_models
from sr.routes.admin_providers import router as admin_providers
from sr.routes.admin_traffic import router as admin_traffic
from sr.routes.admin_tree import router as admin_tree

ROUTERS = [admin_providers, admin_models, admin_tree, admin_traffic,
           admin_eval, admin_config]
