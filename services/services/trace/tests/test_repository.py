"""repository 单测 —— 验证 WHERE 拼接 + stats 聚合 + 异常降级。

不连真 ClickHouse：monkeypatch ch.query_all / query_one 返回 fixture 数据。
"""

from datetime import datetime

import pytest
from trace_svc import repository as repo
from trace_svc.models import CallQuery, CallStatusFilter


class TestBuildWhere:
    def test_empty(self):
        where, params = repo._build_where(CallQuery(), viewer_tenant_id=None)
        assert where == ""
        assert params == {}

    def test_viewer_tenant_forced(self):
        """普通用户：强制 viewer_tenant_id 过滤（String，原样透传）。"""
        where, params = repo._build_where(CallQuery(), viewer_tenant_id="100")
        assert "tenant_id = %(tenant_id)s" in where
        assert params["tenant_id"] == "100"

    def test_viewer_tenant_string_passthrough(self):
        """tenant_id 非数字（如 'system'）→ 原样透传 String。"""
        where, params = repo._build_where(CallQuery(), viewer_tenant_id="system")
        assert params["tenant_id"] == "system"

    def test_status_success(self):
        q = CallQuery(status=CallStatusFilter.SUCCESS)
        where, _ = repo._build_where(q, viewer_tenant_id=None)
        assert "is_success = 1" in where

    def test_status_failed(self):
        q = CallQuery(status=CallStatusFilter.FAILED)
        where, _ = repo._build_where(q, viewer_tenant_id=None)
        assert "is_success = 0" in where

    def test_status_timeout(self):
        q = CallQuery(status=CallStatusFilter.TIMEOUT)
        where, params = repo._build_where(q, viewer_tenant_id=None)
        assert "error_code LIKE %(timeout_pat)s" in where
        assert params["timeout_pat"] == "%timeout%"

    def test_all_filters(self):
        q = CallQuery(
            api_id="api_x",
            app_id="app_y",
            trace_id="tr_z",
            status=CallStatusFilter.FAILED,
            since=datetime(2026, 7, 1),
            until=datetime(2026, 7, 2),
        )
        where, params = repo._build_where(q, viewer_tenant_id=None)
        assert "api_id = %(api_id)s" in where
        assert "app_id = %(app_id)s" in where
        assert "trace_id = %(trace_id)s" in where
        assert "is_success = 0" in where
        assert "ts >= %(since)s" in where
        assert "ts < %(until)s" in where
        assert params["api_id"] == "api_x"
        assert params["app_id"] == "app_y"
        assert params["trace_id"] == "tr_z"
        assert params["since"] == "2026-07-01 00:00:00"
        assert params["until"] == "2026-07-02 00:00:00"


class TestListCalls:
    async def test_returns_rows(self, fake_ch):
        fake_ch["rows"] = [
            {
                "trace_id": "t1",
                "api_id": "api_a",
                "path": "/echo",
                "method": "GET",
                "api_version_id": "v1",
                "app_id": "app_x",
                "client_ip": "10.0.0.1",
                "status_code": 200,
                "is_success": 1,
                "latency_ms": 12,
                "error_code": "",
                "error_msg": "",
                "ts": datetime(2026, 7, 1),
            }
        ]
        rows = await repo.list_calls(CallQuery(), use_admin_session=True)
        assert len(rows) == 1
        assert rows[0]["trace_id"] == "t1"
        # 验证 ch.query_union_peer 被调用一次（admin 走跨区 peer 路径）
        assert len(fake_ch["calls"]) == 1
        call_kind, sql, params, force = fake_ch["calls"][0]
        assert call_kind == "union"
        assert force is None  # admin session

    async def test_clickhouse_unavailable_returns_empty(self, monkeypatch):
        """CH 没初始化 → 返回空列表（不抛）。"""
        from apihub_core import clickhouse as ch_mod

        def _raise(sql, params=None, *, force_tenant_id="sentinel"):
            raise RuntimeError("CH not initialized")

        monkeypatch.setattr(ch_mod, "query_all", _raise)
        rows = await repo.list_calls(CallQuery(), use_admin_session=True)
        assert rows == []


class TestGetCall:
    async def test_found(self, fake_ch):
        fake_ch["row"] = {
            "trace_id": "t1",
            "api_id": "api_a",
            "path": "/echo",
            "method": "GET",
            "api_version_id": "v1",
            "app_id": "app_x",
            "client_ip": "10.0.0.1",
            "request_id": "r1",
            "request_size": 100,
            "response_size": 200,
            "status_code": 200,
            "is_success": 1,
            "latency_ms": 5,
            "backend_latency_ms": 4,
            "ai_streaming": 0,
            "token_prompt": 0,
            "token_completion": 0,
            "token_total": 0,
            "ai_model": "",
            "error_code": "",
            "error_msg": "",
            "ts": datetime(2026, 7, 1),
        }
        row = await repo.get_call("t1", use_admin_session=True)
        assert row["trace_id"] == "t1"

    async def test_not_found(self, fake_ch):
        fake_ch["row"] = None
        from apihub_core.errors import ApiError

        with pytest.raises(ApiError):
            await repo.get_call("missing", use_admin_session=True)

    async def test_normal_user_tenant_filter(self, fake_ch):
        fake_ch["row"] = None
        from apihub_core.errors import ApiError

        with pytest.raises(ApiError):
            await repo.get_call("t1", viewer_tenant_id="100")

        # SQL 包含 tenant_id 过滤
        _, sql, params, force = fake_ch["calls"][0]
        assert "tenant_id = %(tenant_id)s" in sql
        assert params["tenant_id"] == "100"


class TestStats:
    async def test_full_aggregation(self, fake_ch, monkeypatch):
        """stats admin 路径走 query_union_peer 3 次（base/top_apis/by_hour），merge 后聚合。

        base query_union_peer 返回单行（本地 Region proxy）；top_apis / by_hour 返回单行。
        验证 merge 后 base counts/quantiles + top_apis success_rate + by_hour。
        """
        call_count = {"n": 0}

        def _query_union_peer(local_sql, peer_sql=None, params=None, *, force_tenant_id="sentinel"):
            call_count["n"] += 1
            if "GROUP BY api_id" in local_sql:
                return [{"api_id": "api_a", "path": "/echo", "n": 500, "success_n": 490}]
            if "GROUP BY toStartOfHour(ts)" in local_sql:
                return [{"hour": "2026-07-01 00:00:00", "n": 100, "success_n": 95}]
            # base
            return [
                {
                    "total": 1000,
                    "success_count": 950,
                    "failed_count": 50,
                    "timeout_count": 10,
                    "p50_latency_ms": 10.0,
                    "p95_latency_ms": 100.0,
                    "p99_latency_ms": 500.0,
                    "avg_latency_ms": 25.0,
                }
            ]

        from apihub_core import clickhouse as ch_mod

        monkeypatch.setattr(ch_mod, "query_union_peer", _query_union_peer)

        result = await repo.stats(CallQuery(), use_admin_session=True)
        assert result["total"] == 1000
        assert result["success_count"] == 950
        assert result["success_rate"] == 0.95
        assert result["p95_latency_ms"] == 100.0
        assert len(result["top_apis"]) == 1
        assert result["top_apis"][0]["success_rate"] == 0.98
        assert len(result["by_hour"]) == 1
        # 3 个查询全走 query_union_peer
        assert call_count["n"] == 3

    async def test_stats_merges_cross_region_counts(self, fake_ch, monkeypatch):
        """I-1/I-2: 双 Region base counts 求和、top_apis/by_hour 按 key 合并。"""

        def _query_union_peer(local_sql, peer_sql=None, params=None, *, force_tenant_id="sentinel"):
            if "GROUP BY api_id" in local_sql:
                # 两 Region 都有 api_a，by_hour 也各有同一小时
                return [
                    {"api_id": "api_a", "path": "/echo", "n": 500, "success_n": 490},
                    {"api_id": "api_a", "path": "/echo", "n": 300, "success_n": 290},
                    {"api_id": "api_b", "path": "/x", "n": 100, "success_n": 90},
                ]
            if "GROUP BY toStartOfHour(ts)" in local_sql:
                return [
                    {"hour": "2026-07-01 00:00:00", "n": 100, "success_n": 95},
                    {"hour": "2026-07-01 00:00:00", "n": 50, "success_n": 45},
                ]
            return [
                {
                    "total": 600,
                    "success_count": 580,
                    "failed_count": 20,
                    "timeout_count": 2,
                    "p50_latency_ms": 10.0,
                    "p95_latency_ms": 100.0,
                    "p99_latency_ms": 500.0,
                    "avg_latency_ms": 25.0,
                },
                {
                    "total": 400,
                    "success_count": 370,
                    "failed_count": 30,
                    "timeout_count": 3,
                    "p50_latency_ms": 8.0,
                    "p95_latency_ms": 90.0,
                    "p99_latency_ms": 400.0,
                    "avg_latency_ms": 20.0,
                },
            ]

        from apihub_core import clickhouse as ch_mod

        monkeypatch.setattr(ch_mod, "query_union_peer", _query_union_peer)

        result = await repo.stats(CallQuery(), use_admin_session=True)
        # counts 跨区求和
        assert result["total"] == 1000
        assert result["success_count"] == 950
        assert result["failed_count"] == 50
        assert result["timeout_count"] == 5
        # quantiles 取首行（本地）proxy
        assert result["p95_latency_ms"] == 100.0
        # top_apis: api_a 合并 n=800, api_b n=100 → 排序后 api_a 在前
        assert result["top_apis"][0]["api_id"] == "api_a"
        assert result["top_apis"][0]["n"] == 800
        assert result["top_apis"][1]["api_id"] == "api_b"
        # by_hour: 同 hour 合并 n=150
        assert len(result["by_hour"]) == 1
        assert result["by_hour"][0]["n"] == 150

    async def test_empty_when_ch_down(self, monkeypatch):
        from apihub_core import clickhouse as ch_mod

        def _raise(*args, **kwargs):
            raise RuntimeError("down")

        monkeypatch.setattr(ch_mod, "query_union_peer", _raise)
        monkeypatch.setattr(ch_mod, "query_one", _raise)
        monkeypatch.setattr(ch_mod, "query_all", _raise)

        result = await repo.stats(CallQuery(), use_admin_session=True)
        assert result["total"] == 0
        assert result["success_rate"] == 0.0
        assert result["top_apis"] == []
        assert result["by_hour"] == []

    async def test_qps_with_explicit_window(self, fake_ch, monkeypatch):
        """since + until 决定 qps 时间窗口（admin 路径 base 走 query_union_peer）。"""
        from apihub_core import clickhouse as ch_mod

        def _query_union_peer(local_sql, peer_sql=None, params=None, *, force_tenant_id="sentinel"):
            if "GROUP BY" in local_sql:
                return []
            return [
                {
                    "total": 600,
                    "success_count": 600,
                    "failed_count": 0,
                    "timeout_count": 0,
                    "p50_latency_ms": 5,
                    "p95_latency_ms": 20,
                    "p99_latency_ms": 50,
                    "avg_latency_ms": 10,
                }
            ]

        monkeypatch.setattr(ch_mod, "query_union_peer", _query_union_peer)

        q = CallQuery(
            since=datetime(2026, 7, 1, 0, 0, 0),
            until=datetime(2026, 7, 1, 0, 10, 0),  # 600 秒
        )
        result = await repo.stats(q, use_admin_session=True)
        # 600 / 600 = 1.0 qps
        assert result["qps"] == pytest.approx(1.0, abs=0.01)
