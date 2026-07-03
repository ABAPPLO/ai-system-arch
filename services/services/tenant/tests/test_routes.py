"""HTTP 端点测试 —— httpx ASGITransport 直打 app。

mock 策略：repository 层每个函数按需替换 + fakeredis。
覆盖：租户 CRUD、状态机、成员管理、配额、用量、权限矩阵。
"""

from datetime import datetime

from tenant import repository as repo_mod


def _row(**overrides):
    """构造一个 tenant row（dict）。"""
    base = {
        "id": "t_test",
        "parent_id": None,
        "name": "测试租户",
        "slug": "test",
        "type": "internal",
        "status": "active",
        "tier": "standard",
        "metadata": {},
        "created_at": datetime(2026, 7, 1),
        "updated_at": datetime(2026, 7, 1),
    }
    base.update(overrides)
    return base


# ========== 租户 CRUD ==========


class TestCreateTenant:
    async def test_admin_creates_tenant(
        self, client, fake_redis, as_platform_admin, monkeypatch
    ):
        captured = {}

        async def _create(payload):
            captured["payload"] = payload
            return _row(id="t_new", name=payload.name, slug=payload.slug)

        monkeypatch.setattr(repo_mod, "create_tenant", _create)

        resp = await client.post(
            "/v1/tenant/tenants",
            json={
                "id": "t_new",
                "name": "新租户",
                "slug": "new",
                "type": "internal",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["id"] == "t_new"
        # 超管能看到完整 name
        assert body["name"] == "新租户"
        # 缓存被写入
        cached = await fake_redis.get("t:t_new:meta")
        assert cached is not None

    async def test_normal_user_forbidden(self, client, fake_redis, as_normal_user):
        as_normal_user("user_bob")
        resp = await client.post(
            "/v1/tenant/tenants",
            json={"id": "t_x", "name": "XX", "slug": "xx"},
        )
        assert resp.status_code == 403


class TestListTenants:
    async def test_admin_lists_all(
        self, client, fake_redis, as_platform_admin, monkeypatch
    ):
        async def _list_tenants(
            *,
            parent_id=None,
            type_filter=None,
            status_filter=None,
            limit=100,
            offset=0,
        ):
            return [_row(id="t1"), _row(id="t2")]

        monkeypatch.setattr(repo_mod, "list_tenants", _list_tenants)

        resp = await client.get("/v1/tenant/tenants")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2

    async def test_normal_user_lists_own(
        self, client, fake_redis, as_normal_user, monkeypatch
    ):
        as_normal_user("user_bob")

        async def _user_tenants(user_id):
            assert user_id == "user_bob"
            return [_row(id="t_bob", name="Bob 租户")]

        monkeypatch.setattr(repo_mod, "get_user_tenants", _user_tenants)

        resp = await client.get("/v1/tenant/tenants")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        # 普通用户 name 脱敏
        assert body[0]["name"] != "Bob 租户"
        assert "*" in body[0]["name"]


class TestGetTenant:
    async def test_member_can_view(
        self, client, fake_redis, as_normal_user, monkeypatch
    ):
        as_normal_user("user_alice", tenant_id="tenant_a")

        async def _get_membership(tenant_id, user_id):
            return "developer"

        async def _get_tenant(tenant_id):
            return _row(id=tenant_id, name="完整名")

        monkeypatch.setattr(repo_mod, "get_membership", _get_membership)
        monkeypatch.setattr(repo_mod, "get_tenant", _get_tenant)

        resp = await client.get("/v1/tenant/tenants/tenant_a")
        assert resp.status_code == 200
        # developer 不是超管 → 脱敏
        assert "*" in resp.json()["name"]

    async def test_non_member_forbidden(
        self, client, fake_redis, as_normal_user, monkeypatch
    ):
        as_normal_user("user_stranger")

        async def _get_membership(tenant_id, user_id):
            return None

        monkeypatch.setattr(repo_mod, "get_membership", _get_membership)

        resp = await client.get("/v1/tenant/tenants/tenant_a")
        assert resp.status_code == 403

    async def test_admin_views_no_mask(
        self, client, fake_redis, as_platform_admin, monkeypatch
    ):
        async def _get_tenant(tenant_id):
            return _row(id=tenant_id, name="完整名")

        monkeypatch.setattr(repo_mod, "get_tenant", _get_tenant)

        resp = await client.get("/v1/tenant/tenants/tenant_a")
        assert resp.status_code == 200
        assert resp.json()["name"] == "完整名"


# ========== 状态机 ==========


class TestStatusMachine:
    async def test_suspend_invalidates_cache(
        self, client, fake_redis, as_platform_admin, monkeypatch
    ):
        await fake_redis.set("t:t1:meta", '{"old":true}')

        async def _change_status(tid, new):
            assert new == "suspended"
            return _row(id="t1", status="suspended")

        monkeypatch.setattr(repo_mod, "change_status", _change_status)

        resp = await client.post("/v1/tenant/tenants/t1/suspend")
        assert resp.status_code == 200

        # 缓存被清
        cached = await fake_redis.get("t:t1:meta")
        assert cached is None

    async def test_resume_warm_caches(
        self, client, fake_redis, as_platform_admin, monkeypatch
    ):
        async def _change_status(tid, new):
            return _row(id="t1", status="active")

        monkeypatch.setattr(repo_mod, "change_status", _change_status)

        resp = await client.post("/v1/tenant/tenants/t1/resume")
        assert resp.status_code == 200

        cached = await fake_redis.get("t:t1:meta")
        assert cached is not None
        assert '"active"' in cached

    async def test_close_invalidates_cache(
        self, client, fake_redis, as_platform_admin, monkeypatch
    ):
        await fake_redis.set("t:t1:meta", '{"old":true}')

        async def _change_status(tid, new):
            return _row(id="t1", status="closed")

        monkeypatch.setattr(repo_mod, "change_status", _change_status)

        resp = await client.post("/v1/tenant/tenants/t1/close")
        assert resp.status_code == 200

        cached = await fake_redis.get("t:t1:meta")
        assert cached is None

    async def test_status_transition_conflict(
        self, client, fake_redis, as_platform_admin, monkeypatch
    ):
        from apihub_core.errors import ApiError, ErrorCode

        async def _change_status(tid, new):
            raise ApiError(ErrorCode.CONFLICT, "cannot transition")

        monkeypatch.setattr(repo_mod, "change_status", _change_status)

        resp = await client.post("/v1/tenant/tenants/t1/suspend")
        assert resp.status_code == 409


# ========== 成员管理 ==========


class TestMembers:
    async def test_add_member_as_owner(
        self, client, fake_redis, as_normal_user, monkeypatch
    ):
        as_normal_user("user_alice", tenant_id="t1")

        async def _get_membership(tid, uid):
            return "owner"

        async def _add_member(tid, uid, role):
            assert role == "developer"
            return {
                "id": "tm_1",
                "tenant_id": tid,
                "user_id": uid,
                "role": role,
                "created_at": datetime(2026, 7, 1),
            }

        monkeypatch.setattr(repo_mod, "get_membership", _get_membership)
        monkeypatch.setattr(repo_mod, "add_member", _add_member)

        resp = await client.post(
            "/v1/tenant/tenants/t1/members",
            json={"user_id": "user_bob", "role": "developer"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["role"] == "developer"

    async def test_add_member_as_viewer_forbidden(
        self, client, fake_redis, as_normal_user, monkeypatch
    ):
        as_normal_user("user_viewer", tenant_id="t1")

        async def _get_membership(tid, uid):
            return "viewer"

        monkeypatch.setattr(repo_mod, "get_membership", _get_membership)

        resp = await client.post(
            "/v1/tenant/tenants/t1/members",
            json={"user_id": "user_bob", "role": "developer"},
        )
        assert resp.status_code == 403

    async def test_update_member_role(
        self, client, fake_redis, as_normal_user, monkeypatch
    ):
        as_normal_user("user_alice", tenant_id="t1")

        async def _get_membership(tid, uid):
            return "admin"

        async def _update_member_role(tid, uid, role):
            assert role == "owner"
            return {
                "id": "tm_1",
                "tenant_id": tid,
                "user_id": uid,
                "role": role,
                "created_at": datetime(2026, 7, 1),
            }

        monkeypatch.setattr(repo_mod, "get_membership", _get_membership)
        monkeypatch.setattr(repo_mod, "update_member_role", _update_member_role)

        resp = await client.put(
            "/v1/tenant/tenants/t1/members/user_bob",
            json={"role": "owner"},
        )
        assert resp.status_code == 200

    async def test_remove_member(
        self, client, fake_redis, as_normal_user, monkeypatch
    ):
        as_normal_user("user_alice", tenant_id="t1")

        async def _get_membership(tid, uid):
            return "owner"

        removed = {}

        async def _remove_member(tid, uid):
            removed["tid"] = tid
            removed["uid"] = uid

        monkeypatch.setattr(repo_mod, "get_membership", _get_membership)
        monkeypatch.setattr(repo_mod, "remove_member", _remove_member)

        resp = await client.delete("/v1/tenant/tenants/t1/members/user_bob")
        assert resp.status_code == 204
        assert removed == {"tid": "t1", "uid": "user_bob"}


# ========== 配额 ==========


class TestQuota:
    async def test_member_reads_quota(
        self, client, fake_redis, as_normal_user, monkeypatch
    ):
        as_normal_user("user_alice", tenant_id="t1")

        async def _get_membership(tid, uid):
            return "developer"

        async def _get_quota(tid):
            return {"day_limit": 1000, "rate_limit": {"minute": 100}}

        async def _get_tenant(tid):
            return _row(id=tid)

        monkeypatch.setattr(repo_mod, "get_membership", _get_membership)
        monkeypatch.setattr(repo_mod, "get_quota", _get_quota)
        monkeypatch.setattr(repo_mod, "get_tenant", _get_tenant)

        resp = await client.get("/v1/tenant/tenants/t1/quota")
        assert resp.status_code == 200
        body = resp.json()
        assert body["day_limit"] == 1000

    async def test_admin_writes_quota_invalidates_cache(
        self, client, fake_redis, as_platform_admin, monkeypatch
    ):
        await fake_redis.set("t:t1:meta", '{"old":true}')

        async def _set_quota(tid, quota):
            return _row(id=tid, metadata={"quota": quota})

        monkeypatch.setattr(repo_mod, "set_quota", _set_quota)

        resp = await client.put(
            "/v1/tenant/tenants/t1/quota",
            json={"day_limit": 5000, "rate_limit": {}},
        )
        assert resp.status_code == 200
        assert resp.json()["day_limit"] == 5000

        # 缓存失效
        cached = await fake_redis.get("t:t1:meta")
        assert cached is None

    async def test_normal_user_cannot_change_quota(
        self, client, fake_redis, as_normal_user, monkeypatch
    ):
        as_normal_user("user_alice", tenant_id="t1")

        async def _get_membership(tid, uid):
            return "owner"  # 即使是 owner 也不能改配额

        monkeypatch.setattr(repo_mod, "get_membership", _get_membership)

        resp = await client.put(
            "/v1/tenant/tenants/t1/quota",
            json={"day_limit": 5000},
        )
        assert resp.status_code == 403


# ========== 用量 ==========


class TestUsage:
    async def test_usage_returns_day_limit(
        self, client, fake_redis, as_normal_user, monkeypatch
    ):
        as_normal_user("user_alice", tenant_id="t1")

        async def _get_membership(tid, uid):
            return "viewer"

        async def _get_quota(tid):
            return {"day_limit": 1000}

        async def _get_tenant(tid):
            return _row(id=tid)

        monkeypatch.setattr(repo_mod, "get_membership", _get_membership)
        monkeypatch.setattr(repo_mod, "get_quota", _get_quota)
        monkeypatch.setattr(repo_mod, "get_tenant", _get_tenant)

        resp = await client.get("/v1/tenant/tenants/t1/usage")
        assert resp.status_code == 200
        body = resp.json()
        assert body["day_limit"] == 1000
        assert body["day_used"] == 0
        assert body["remaining"] == 1000


# ========== 子租户 ==========


class TestChildren:
    async def test_list_children(
        self, client, fake_redis, as_platform_admin, monkeypatch
    ):
        async def _list_children(pid):
            return [
                _row(id="child1", parent_id=pid),
                _row(id="child2", parent_id=pid),
            ]

        monkeypatch.setattr(repo_mod, "list_children", _list_children)

        resp = await client.get("/v1/tenant/tenants/t1/children")
        assert resp.status_code == 200
        assert len(resp.json()) == 2


# ========== 健康 ==========


class TestHealth:
    async def test_health(self, client, fake_redis):
        resp = await client.get("/v1/tenant/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "service": "tenant"}


# ========== 名字脱敏边界 ==========


class TestMasking:
    def test_admin_no_mask(self):
        from tenant.routes import _mask_name

        assert _mask_name("某互联网公司", is_admin=True) == "某互联网公司"

    def test_user_masked(self):
        from tenant.routes import _mask_name

        masked = _mask_name("某互联网公司", is_admin=False)
        assert masked != "某互联网公司"
        assert masked[0] == "某"
        assert "*" in masked

    def test_short_name_no_mask(self):
        """名字太短（<=2）不脱敏，否则全星号。"""
        from tenant.routes import _mask_name

        assert _mask_name("X", is_admin=False) == "X"
        assert _mask_name("AB", is_admin=False) == "AB"
