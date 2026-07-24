"""change_request workflow 测试 —— 状态机 + 路由权限 + 环境分级。"""


class TestSubmit:
    async def test_dev_auto_applied(self, admin_client, stub_cr):
        """dev 环境 submit → status=applied（自助）。"""
        resp = await admin_client.post(
            "/v1/change-requests",
            json={
                "api_id": 100,
                "target_version": "v1",
                "change_type": "create",
                "target_env": "dev",
                "proposed_config": {"name": "demo"},
                "submitted_by": "u_alice",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "approved"
        # dev 自助 → apply 已触发
        assert stub_cr["applied"] == [data["request_id"]]

    async def test_prod_pending(self, admin_client, stub_cr):
        """prod 环境 submit → pending，不自动 apply。"""
        resp = await admin_client.post(
            "/v1/change-requests",
            json={
                "api_id": 100,
                "target_version": "v1",
                "change_type": "publish",
                "target_env": "prod",
                "proposed_config": {},
                "submitted_by": "u_alice",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["status"] == "pending"
        # 没自动 apply
        assert stub_cr["applied"] == []

    async def test_dingtalk_id_for_staging(self, admin_client, stub_cr):
        """staging submit → 走钉钉，回填 dingtalk_approval_id。"""
        stub_cr["dingtalk_returns"] = {"staging": "dt_staging_123"}

        resp = await admin_client.post(
            "/v1/change-requests",
            json={
                "api_id": 100,
                "target_version": "v1",
                "change_type": "publish",
                "target_env": "staging",
                "proposed_config": {},
                "submitted_by": "u_alice",
            },
        )
        rid = resp.json()["request_id"]
        req = stub_cr["requests"][rid]
        assert req.dingtalk_approval_id == "dt_staging_123"


class TestReviewPermissions:
    async def test_approve_requires_platform_admin(self, normal_client, stub_cr):
        """非超管 approve → 403。

        注意：不能用 admin_client + normal_client 双 fixture，
        二者都 monkeypatch 同一个 auth_mod.authenticate_request，
        后激活的会覆盖前者。改为直接在 stub_cr 里注入 pending 请求。
        """
        from datetime import UTC, datetime

        from api_registry.change_request import (
            ChangeRequest,
            ChangeRequestStatus,
            ChangeType,
            TargetEnv,
        )

        cr_obj = ChangeRequest(
            id=1,
            tenant_id=42,
            api_id=100,
            target_version="v1",
            change_type=ChangeType.PUBLISH,
            target_env=TargetEnv.PROD,
            proposed_config={},
            status=ChangeRequestStatus.PENDING,
            submitted_by="u_alice",
            submitted_at=datetime.now(UTC),
        )
        stub_cr["requests"][1] = cr_obj
        stub_cr["next_id"][0] = 2

        resp = await normal_client.post(
            "/v1/change-requests/1/approve",
            json={"review_comment": "ok"},
        )
        assert resp.status_code == 403

    async def test_approve_by_admin(self, admin_client, stub_cr):
        from api_registry.change_request import ChangeRequestStatus

        resp = await admin_client.post(
            "/v1/change-requests",
            json={
                "api_id": 1,
                "target_version": "v1",
                "change_type": "publish",
                "target_env": "prod",
                "proposed_config": {},
                "submitted_by": "u_alice",
            },
        )
        rid = resp.json()["request_id"]
        assert stub_cr["requests"][rid].status == ChangeRequestStatus.PENDING

        resp = await admin_client.post(
            f"/v1/change-requests/{rid}/approve",
            json={"review_comment": "lgtm"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"
        assert stub_cr["requests"][rid].status == ChangeRequestStatus.APPROVED

    async def test_reject_by_admin(self, admin_client, stub_cr):
        from api_registry.change_request import ChangeRequestStatus

        resp = await admin_client.post(
            "/v1/change-requests",
            json={
                "api_id": 1,
                "target_version": "v1",
                "change_type": "publish",
                "target_env": "prod",
                "proposed_config": {},
                "submitted_by": "u_alice",
            },
        )
        rid = resp.json()["request_id"]

        resp = await admin_client.post(
            f"/v1/change-requests/{rid}/reject",
            json={"review_comment": "needs more thought"},
        )
        assert resp.status_code == 200
        assert stub_cr["requests"][rid].status == ChangeRequestStatus.REJECTED


class TestApply:
    async def test_apply_requires_platform_admin(self, normal_client, stub_cr):
        """非超管 apply → 403（apply 执行实际副作用，必须超管）。"""
        from datetime import UTC, datetime

        from api_registry.change_request import (
            ChangeRequest,
            ChangeRequestStatus,
            ChangeType,
            TargetEnv,
        )

        cr_obj = ChangeRequest(
            id=2,
            tenant_id=42,
            api_id=100,
            target_version="v1",
            change_type=ChangeType.PUBLISH,
            target_env=TargetEnv.PROD,
            proposed_config={},
            status=ChangeRequestStatus.APPROVED,
            submitted_by="u_alice",
            submitted_at=datetime.now(UTC),
        )
        stub_cr["requests"][2] = cr_obj

        resp = await normal_client.post("/v1/change-requests/2/apply")
        assert resp.status_code == 403

    async def test_apply_approved_request(self, admin_client, stub_cr):
        """approved → apply → applied。"""
        # 提交一个 prod 请求（pending）→ admin approve → apply
        resp = await admin_client.post(
            "/v1/change-requests",
            json={
                "api_id": 1,
                "target_version": "v1",
                "change_type": "publish",
                "target_env": "prod",
                "proposed_config": {},
                "submitted_by": "u_alice",
            },
        )
        rid = resp.json()["request_id"]
        await admin_client.post(f"/v1/change-requests/{rid}/approve")

        resp = await admin_client.post(f"/v1/change-requests/{rid}/apply")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "applied"
        assert "summary" in data

    async def test_apply_not_approved_409(self, admin_client, stub_cr):
        """对 pending 请求 apply → 409。"""
        resp = await admin_client.post(
            "/v1/change-requests",
            json={
                "api_id": 1,
                "target_version": "v1",
                "change_type": "publish",
                "target_env": "prod",
                "proposed_config": {},
                "submitted_by": "u_alice",
            },
        )
        rid = resp.json()["request_id"]
        # 直接 apply（未 approve）
        resp = await admin_client.post(f"/v1/change-requests/{rid}/apply")
        assert resp.status_code == 409

    async def test_apply_failed_500(self, admin_client, stub_cr):
        """apply 抛 RuntimeError → 500。"""
        resp = await admin_client.post(
            "/v1/change-requests",
            json={
                "api_id": 1,
                "target_version": "v1",
                "change_type": "publish",
                "target_env": "prod",
                "proposed_config": {},
                "submitted_by": "u_alice",
            },
        )
        rid = resp.json()["request_id"]
        await admin_client.post(f"/v1/change-requests/{rid}/approve")

        stub_cr["apply_raises"] = RuntimeError("db down")
        resp = await admin_client.post(f"/v1/change-requests/{rid}/apply")
        assert resp.status_code == 500


class TestCancel:
    async def test_cancel_by_submitter(self, admin_client, stub_cr):
        """提交方撤回。"""
        # admin_client 的 user_id 是 u_admin
        resp = await admin_client.post(
            "/v1/change-requests",
            json={
                "api_id": 1,
                "target_version": "v1",
                "change_type": "publish",
                "target_env": "prod",
                "proposed_config": {},
                "submitted_by": "u_admin",
            },
        )
        rid = resp.json()["request_id"]
        resp = await admin_client.post(f"/v1/change-requests/{rid}/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"


class TestGetAndList:
    async def test_get_not_found(self, admin_client, stub_cr):
        resp = await admin_client.get("/v1/change-requests/9999")
        assert resp.status_code == 404

    async def test_get_returns_request(self, admin_client, stub_cr):
        resp = await admin_client.post(
            "/v1/change-requests",
            json={
                "api_id": 5,
                "target_version": "v2",
                "change_type": "update",
                "target_env": "staging",
                "proposed_config": {"x": 1},
                "submitted_by": "u_alice",
            },
        )
        rid = resp.json()["request_id"]

        resp = await admin_client.get(f"/v1/change-requests/{rid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == rid
        assert data["change_type"] == "update"
        assert data["target_env"] == "staging"

    async def test_list(self, admin_client, stub_cr):
        # 先 submit 几个
        for i in range(3):
            await admin_client.post(
                "/v1/change-requests",
                json={
                    "api_id": i,
                    "target_version": "v1",
                    "change_type": "publish",
                    "target_env": "prod",
                    "proposed_config": {},
                    "submitted_by": "u_alice",
                },
            )
        resp = await admin_client.get("/v1/change-requests")
        assert resp.status_code == 200
        assert len(resp.json()) >= 3


class TestHealth:
    async def test_health(self, admin_client):
        resp = await admin_client.get("/v1/change-requests/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
