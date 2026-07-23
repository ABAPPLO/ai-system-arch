"""Admin 钉钉 SSO 单测。"""

from apihub_core.config import Settings


def test_bootstrap_unionids_parses_csv():
    s = Settings(dingtalk_client_id="x")  # 其余必填走 conftest env
    s.bootstrap_admin_dingtalk_unionids = "uid1, uid2 ,, uid3"
    from auth.identity import _bootstrap_admin_unionids

    assert _bootstrap_admin_unionids(s) == {"uid1", "uid2", "uid3"}


def test_bootstrap_unionids_empty():
    s = Settings(dingtalk_client_id="x")
    s.bootstrap_admin_dingtalk_unionids = ""
    from auth.identity import _bootstrap_admin_unionids

    assert _bootstrap_admin_unionids(s) == set()
