from unittest.mock import MagicMock

import pytest
from apihub_core import clickhouse as ch


def test_query_union_peer_peer_unset_returns_local_only():
    ch._client = MagicMock()
    ch._peer_client = None
    r = MagicMock()
    r.column_names = ("c",)
    r.result_rows = [(1,), (2,)]
    ch._client.query.return_value = r
    rows = ch.query_union_peer("SELECT c FROM t", None, None, force_tenant_id=None)
    assert rows == [{"c": 1}, {"c": 2}]
    ch._client.query.assert_called_once()


def test_query_union_peer_concatenates_both():
    ch._client = MagicMock()
    ch._peer_client = MagicMock()

    def mk(vals):
        r = MagicMock()
        r.column_names = ("c",)
        r.result_rows = [(v,) for v in vals]
        return r

    ch._client.query.return_value = mk([1, 2])
    ch._peer_client.query.return_value = mk([3, 4])
    rows = ch.query_union_peer("SELECT c FROM t", "SELECT c FROM t", None, force_tenant_id=None)
    assert {r["c"] for r in rows} == {1, 2, 3, 4}


def test_query_union_peer_no_local_client_raises():
    ch._client = None
    ch._peer_client = None
    with pytest.raises(RuntimeError):
        ch.query_union_peer("SELECT 1", None, None, force_tenant_id=None)


def test_query_union_peer_peer_sql_but_no_peer_client_returns_local_only():
    """M-1: peer_sql 非空但 _peer_client is None（单 Region degrade）→
    返回 local-only 行（peer_sql 被忽略，不报错）。"""
    ch._client = MagicMock()
    ch._peer_client = None
    r = MagicMock()
    r.column_names = ("c",)
    r.result_rows = [(1,), (2,)]
    ch._client.query.return_value = r
    rows = ch.query_union_peer("SELECT c FROM t", "SELECT c FROM t", None, force_tenant_id=None)
    assert rows == [{"c": 1}, {"c": 2}]
    ch._client.query.assert_called_once()


def test_query_union_peer_peer_sql_with_non_admin_scope_rejected():
    """M-2: peer_sql + force_tenant_id 非 None → 防跨租户泄漏，ValueError 拒绝。"""
    ch._client = MagicMock()
    ch._peer_client = MagicMock()
    # sentinel（默认）
    with pytest.raises(ValueError):
        ch.query_union_peer("SELECT c FROM t", "SELECT c FROM t", None)
    # 显式 tenant str
    with pytest.raises(ValueError):
        ch.query_union_peer("SELECT c FROM t", "SELECT c FROM t", None, force_tenant_id="t_demo")
    ch._client.query.assert_not_called()
    ch._peer_client.query.assert_not_called()


def test_query_union_peer_peer_failure_degrades_to_local_only():
    """M-4: peer leg 抛错 → log + degrade-to-local（返回已收集的 local 行），不向上抛。"""
    ch._client = MagicMock()
    ch._peer_client = MagicMock()

    def mk_local():
        r = MagicMock()
        r.column_names = ("c",)
        r.result_rows = [(1,), (2,)]
        return r

    ch._client.query.return_value = mk_local()
    ch._peer_client.query.side_effect = RuntimeError("peer CH down")
    rows = ch.query_union_peer("SELECT c FROM t", "SELECT c FROM t", None, force_tenant_id=None)
    # local 行原样返回
    assert rows == [{"c": 1}, {"c": 2}]
