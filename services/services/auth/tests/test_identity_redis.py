"""identity 的纯 Redis / 纯函数测试（无需 dev PG）。

_delete_user_tokens：注销只清本人 token —— 此前 blind delete t:refresh:* 会把
全站所有用户登出（任何一个用户行使 GDPR 删除权）。
_registration_response：verify_token 只在非 prod 返回（prod 返回 = 邮箱验证被架空）。
"""


class TestDeleteUserTokens:
    """注销只清本人 token（此前 blind delete 会把全站登出）。纯 Redis 逻辑，无需 PG。"""

    async def test_only_deletes_own_tokens(self, fake_redis):
        from auth import identity

        await fake_redis.set("t:refresh:jti_a", "userA")
        await fake_redis.set("t:refresh:jti_b", "userB")
        await fake_redis.set("t:refresh:jti_c", "userA")
        await fake_redis.set("t:verify:tok_x", "userA")
        await fake_redis.set("t:verify:tok_y", "userB")

        n = await identity._delete_user_tokens("userA")

        assert n == 3
        assert await fake_redis.get("t:refresh:jti_a") is None
        assert await fake_redis.get("t:refresh:jti_c") is None
        assert await fake_redis.get("t:verify:tok_x") is None
        # 其他用户的 token 必须原样保留（回归点：此前全站被清）
        assert await fake_redis.get("t:refresh:jti_b") == "userB"
        assert await fake_redis.get("t:verify:tok_y") == "userB"

    async def test_no_tokens_deletes_nothing(self, fake_redis):
        from auth import identity

        await fake_redis.set("t:refresh:jti_b", "userB")
        assert await identity._delete_user_tokens("nobody") == 0
        assert await fake_redis.get("t:refresh:jti_b") == "userB"


class TestRegistrationResponse:
    """verify_token 只在非 prod 随响应返回（prod 返回 = 邮箱验证被架空）。"""

    def test_dev_returns_token(self):
        from auth.identity import _registration_response

        resp = _registration_response("u1", "tok", "dev")
        assert resp["verify_token"] == "tok"

    def test_prod_hides_token(self):
        from auth.identity import _registration_response

        resp = _registration_response("u1", "tok", "prod")
        assert "verify_token" not in resp
        assert resp["status"] == "pending"


class TestSendVerificationEmail:
    """注册验证邮件 → notification 内部端点（X-Ingress-Auth 信任入口）。纯 mock，无需 PG。"""

    async def test_calls_notify_with_token_and_tenant(self, monkeypatch):
        import httpx
        from auth import identity

        captured = {}

        class _FakeResp:
            status_code = 200

            def json(self):
                return {"success": True}

            def raise_for_status(self):
                pass

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def post(self, url, json=None, headers=None):
                captured["url"] = url
                captured["json"] = json
                captured["headers"] = headers
                return _FakeResp()

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

        class _S:
            portal_public_url = "https://portal.example.com"
            notify_internal_url = "http://notification/v1/internal/notify/send"
            ingress_shared_secret = "sec-1"

        result = await identity._send_verification_email(_S, "u@example.com", "tok123")
        assert result == {"success": True}
        assert captured["url"] == "http://notification/v1/internal/notify/send"
        assert captured["headers"]["X-Ingress-Auth"] == "sec-1"
        body = captured["json"]
        assert body["template_code"] == "email_verification"
        assert body["channel_type"] == "email"
        assert body["recipient"] == "u@example.com"
        assert body["tenant_id"] == "external-public"
        assert (
            body["variables"]["verify_url"]
            == "https://portal.example.com/verify-email?token=tok123"
        )

    async def test_no_secret_sends_without_header(self, monkeypatch):
        import httpx
        from auth import identity

        captured = {}

        class _FakeResp:
            status_code = 200

            def json(self):
                return {}

            def raise_for_status(self):
                pass

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def post(self, url, json=None, headers=None):
                captured["headers"] = headers
                return _FakeResp()

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

        class _S:
            portal_public_url = "http://localhost:5174"
            notify_internal_url = "http://notification/v1/internal/notify/send"
            ingress_shared_secret = None

        await identity._send_verification_email(_S, "u@example.com", "t")
        assert "X-Ingress-Auth" not in (captured["headers"] or {})
