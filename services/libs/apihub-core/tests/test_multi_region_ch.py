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
