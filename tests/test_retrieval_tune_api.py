"""检索调参 API 轻量测试（路由注册 + service 报告）。

权限链路（admin 200 / operator 403）需完整 auth flow，conftest 无 auth_client fixture，
靠端到端手动验证（见 plan Task 6 验收清单）。
"""


def test_tune_routes_registered():
    """/api/system/retrieval/tune + /tune/report 已挂载。"""
    from app.main import app
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/api/system/retrieval/tune" in paths
    assert "/api/system/retrieval/tune/report" in paths


def test_get_tune_report_returns_dict():
    """get_tune_report 始终返回 dict（无缓存 {empty:True} / 有缓存含 baseline）。"""
    from app.services.retrieval_tune_service import get_tune_report
    rep = get_tune_report()
    assert isinstance(rep, dict)
    assert "empty" in rep or "baseline" in rep
