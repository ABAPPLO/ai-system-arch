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
