"""D2/A1：admin 端点拆 APIRouter 后的**路由契约快照**（零行为变化）。

基线 = 搬迁前 sr/server.py 的 33 条路由 + audit step 3 新增 1 条（AST 抽取）。本测试钉住：
`(method, path)` 集合与拆分前完全一致；且 sr/routes/* 不反向 import sr.server。
"""
import pathlib

from sr import db
from sr.server import create_app
from sr.tagger import HeuristicTagger

BASELINE: list[tuple[str, str]] = [
    ("GET", "/"),
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/messages"),
    ("GET", "/admin/providers"),
    ("POST", "/admin/providers"),
    ("GET", "/admin/models"),
    ("GET", "/admin/tree"),
    ("GET", "/admin/settings"),
    ("GET", "/admin/bands"),
    ("GET", "/admin/traffic"),
    ("GET", "/admin/traffic/{rid}"),
    ("POST", "/admin/traffic/{rid}/replay"),
    ("GET", "/admin/i18n/{lang}"),
    ("PUT", "/admin/tree"),
    ("POST", "/admin/tree/audit"),
    ("POST", "/admin/tree/try"),
    ("POST", "/admin/tree/estimate"),
    ("GET", "/admin/tree/estimates"),
    ("POST", "/admin/tree/estimates/backfill"),
    ("POST", "/admin/eval"),
    ("GET", "/admin/evals"),
    ("POST", "/admin/tree/nodes"),
    ("DELETE", "/admin/tree/nodes/{idx}"),
    ("POST", "/admin/tree/template"),
    ("POST", "/admin/models"),
    ("PUT", "/admin/models/{mid}"),
    ("POST", "/admin/models/{mid}/reset"),
    ("POST", "/admin/models/{mid}/test"),
    ("PUT", "/admin/settings"),
    ("PUT", "/admin/bands"),
    ("GET", "/admin/export"),
    ("POST", "/admin/import"),
    ("GET", "/metrics"),
    ("GET", "/v1/models"),
]


def _route_set(app) -> set[tuple[str, str]]:
    """展开 app.routes（含 include_router 的 _IncludedRouter 子树）。"""
    out: set[tuple[str, str]] = set()

    def walk(routes):
        for r in routes:
            sub = getattr(r, "routes", None) or getattr(
                getattr(r, "original_router", None), "routes", None)
            if sub:                                   # _IncludedRouter / Mount
                walk(sub)
                continue
            path = getattr(r, "path", None)
            if not path or path.startswith("/openapi"):
                continue
            for m in getattr(r, "methods", None) or ():
                if m not in {"HEAD", "OPTIONS"}:
                    out.add((m, path))

    walk(app.routes)
    return out


def test_route_contract_matches_pre_split_baseline(tmp_path):
    path = str(tmp_path / "t.db")
    db.init_db(path).close()
    app = create_app(path, HeuristicTagger())
    got = _route_set(app)
    assert got == set(BASELINE), (
        f"多出 {sorted(got - set(BASELINE))}；缺失 {sorted(set(BASELINE) - got)}")


def test_routes_package_never_imports_server():
    """依赖方向：sr.server → sr.routes → sr.routes._deps，单向无环。"""
    bad = []
    for f in sorted(pathlib.Path("sr/routes").glob("*.py")):
        text = f.read_text()
        if "import sr.server" in text or "from sr.server" in text:
            bad.append(str(f))
    assert bad == [], f"反向 import：{bad}"


def test_console_is_still_hosted_at_root(tmp_path):
    """D2 搬迁回归：`_CONSOLE_DEFAULT` 随 `_deps.py` 换了目录深度，写错会让
    `GET /` 404（原路径 `parent.parent` 在 sr/routes/ 下指向 sr/）。"""
    import asyncio
    import httpx

    path = str(tmp_path / "t.db")
    db.init_db(path).close()
    app = create_app(path, HeuristicTagger())

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.get("/")

    # 用**局部**事件环：asyncio.run() 在 3.14 会清掉线程当前环，把同线程后续
    # 依赖 get_event_loop() 的测试一起打挂（实测 50 failed）。
    loop = asyncio.new_event_loop()
    try:
        r = loop.run_until_complete(go())
    finally:
        loop.close()
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "<html" in r.text[:2000].lower()
