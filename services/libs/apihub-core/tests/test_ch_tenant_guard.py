from unittest.mock import MagicMock, patch

from apihub_core import clickhouse as ch


def _set_client():
    ch._client = MagicMock()
    ch._peer_client = None
    r = MagicMock()
    r.column_names = ("c",)
    r.result_rows = [(1,)]
    ch._client.query.return_value = r


def test_tenant_scope_missing_token_raises():
    _set_client()
    with patch("apihub_core.clickhouse.get_tenant_context") as gtc:
        gtc.return_value = MagicMock(tenant_id="t_a")
        try:
            ch.query_all("SELECT * FROM t WHERE ts>%(s)s", {"s": "x"},
                          force_tenant_id="sentinel")
            raise AssertionError("expected ValueError (missing %(tenant_id)s)")
        except ValueError as e:
            assert "%(tenant_id)s" in str(e)


def test_tenant_scope_spoofed_tenant_raises():
    _set_client()
    with patch("apihub_core.clickhouse.get_tenant_context") as gtc:
        gtc.return_value = MagicMock(tenant_id="t_a")
        try:
            ch.query_all("SELECT * FROM t WHERE tenant_id=%(tenant_id)s",
                         {"tenant_id": "t_b"}, force_tenant_id="sentinel")
            raise AssertionError("expected ValueError (spoofed tenant_id)")
        except ValueError as e:
            assert "tenant_id param does not match" in str(e)


def test_tenant_scope_valid_passes():
    _set_client()
    with patch("apihub_core.clickhouse.get_tenant_context") as gtc:
        gtc.return_value = MagicMock(tenant_id="t_a")
        rows = ch.query_all("SELECT c FROM t WHERE tenant_id=%(tenant_id)s",
                            {"tenant_id": "t_a"}, force_tenant_id="sentinel")
        assert rows == [{"c": 1}]


def test_admin_opt_out_no_validation_audit():
    _set_client()
    with patch("apihub_core.clickhouse.get_tenant_context") as gtc:
        gtc.return_value = MagicMock(tenant_id="t_a")
        with patch.object(ch.log, "info") as log_info:
            rows = ch.query_all("SELECT * FROM t", None, force_tenant_id=None)
            assert rows == [{"c": 1}]
        log_info.assert_any_call("ch_admin_scope_query", sql="SELECT * FROM t")


def test_query_union_peer_still_admin_only():
    _set_client()
    with patch("apihub_core.clickhouse.get_tenant_context") as gtc:
        gtc.return_value = MagicMock(tenant_id="t_a")
        try:
            ch.query_union_peer("SELECT 1", "SELECT 1", None,
                                 force_tenant_id="sentinel")
            raise AssertionError("expected ValueError (M-2 guard)")
        except ValueError as e:
            assert "peer_sql requires admin scope" in str(e)
