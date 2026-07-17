"""外部开发者身份业务 —— 注册 / 邮箱验证 / 登录 / 账号删除。

复用现有表：user_account(schema:32) + tenant_member(:47)。
邮箱验证 token 存 Redis（dev stub 不真发邮件）。
"""

import secrets
from datetime import datetime, timezone

import bcrypt
from apihub_core import db, redis
from apihub_core.errors import ApiError, ErrorCode
from apihub_core.logging import get_logger

log = get_logger(__name__)

EXTERNAL_PUBLIC_TENANT = "external-public"
VERIFY_TTL = 86400  # 24h


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


async def create_user(*, email: str, password: str, phone: str, name: str) -> dict:
    from apihub_core.pii import encrypt_pii  # noqa: PLC0415

    async with db.admin_db_session() as conn:
        exists = await conn.fetchval(
            "SELECT id FROM user_account WHERE email = $1", email
        )
        if exists:
            raise ApiError(ErrorCode.CONFLICT, "email already registered", http_status=409)
        user_id = f"u_{secrets.token_hex(8)}"
        await conn.execute(
            "INSERT INTO user_account (id, email, phone, password_hash, name,"
            " verification_level, status)"
            " VALUES ($1, $2, $3, $4, $5, 'email', 'pending')",
            user_id, email, encrypt_pii(phone), _hash_password(password), encrypt_pii(name),
        )
        # GDPR: 记录数据使用同意
        for purpose in ("account_management", "api_usage_tracking"):
            await conn.execute(
                "INSERT INTO user_consent (user_id, purpose, status) VALUES ($1, $2, 'granted')"
                " ON CONFLICT (user_id, purpose) DO NOTHING",
                user_id, purpose,
            )

    verify_token = secrets.token_urlsafe(32)
    await redis.t_set(f"t:verify:{verify_token}", user_id, ex=VERIFY_TTL)
    log.info("user_registered", user_id=user_id, email=email)
    return {"user_id": user_id, "status": "pending",
            "verification_level": "email", "verify_token": verify_token}


async def verify_email(token: str) -> dict:
    from apihub_core.pii import maybe_decrypt  # noqa: PLC0415

    user_id = await redis.t_get(f"t:verify:{token}")
    if not user_id:
        raise ApiError(ErrorCode.INVALID_INPUT, "invalid or expired token", http_status=400)
    async with db.admin_db_session() as conn:
        row = await conn.fetchrow(
            "UPDATE user_account SET status='active', last_login_at=NOW() "
            "WHERE id=$1 RETURNING id, name", user_id,
        )
        if not row:
            raise ApiError(ErrorCode.NOT_FOUND, "user not found", http_status=404)
        await conn.execute(
            "INSERT INTO tenant_member (id, tenant_id, user_id, role)"
            " VALUES ($1, $2, $3, 'developer') ON CONFLICT DO NOTHING",
            f"tm_{secrets.token_hex(8)}", EXTERNAL_PUBLIC_TENANT, user_id,
        )
    await redis.t_delete(f"t:verify:{token}")
    return {"user_id": user_id, "name": maybe_decrypt(row["name"]), "status": "active",
            "tenant_id": EXTERNAL_PUBLIC_TENANT}


async def login(*, email: str, password: str) -> dict:
    from apihub_core import jwt_utils
    from apihub_core.config import get_settings
    from apihub_core.pii import maybe_decrypt  # noqa: PLC0415

    async with db.admin_db_session() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, name, password_hash, status FROM user_account WHERE email=$1", email,
        )
    if not row or not row["password_hash"] or not _check_password(password, row["password_hash"]):
        raise ApiError(ErrorCode.UNAUTHORIZED, "invalid email or password", http_status=401)
    if row["status"] != "active":
        raise ApiError(ErrorCode.FORBIDDEN, "email not verified", http_status=403)
    s = get_settings()
    access = jwt_utils.issue_token(
        user_id=row["id"], tenant_id=EXTERNAL_PUBLIC_TENANT,
        secret=s.jwt_secret, ttl_seconds=s.jwt_ttl_seconds,
    )
    refresh = jwt_utils.issue_refresh_token(
        user_id=row["id"], tenant_id=EXTERNAL_PUBLIC_TENANT,
        secret=s.jwt_secret, ttl_seconds=s.jwt_refresh_ttl_seconds,
    )
    rt_payload = jwt_utils.decode_token(refresh, s.jwt_secret)
    await redis.t_set(f"t:refresh:{rt_payload['jti']}", row["id"],
                      ex=s.jwt_refresh_ttl_seconds)
    return {"access_token": access, "refresh_token": refresh,
            "expires_in": s.jwt_ttl_seconds,
            "user": {"id": row["id"], "name": maybe_decrypt(row["name"]), "tenant_id": EXTERNAL_PUBLIC_TENANT}}


async def refresh_access(refresh_token: str) -> dict:
    from apihub_core import jwt_utils
    from apihub_core.config import get_settings

    s = get_settings()
    try:
        payload = jwt_utils.decode_token(refresh_token, s.jwt_secret)
    except jwt_utils.JWTError as e:
        raise ApiError(ErrorCode.UNAUTHORIZED, "invalid refresh token", http_status=401) from e
    if payload.get("type") != "refresh":
        raise ApiError(ErrorCode.UNAUTHORIZED, "invalid token type", http_status=401)
    jti = payload.get("jti")
    stored = await redis.t_get(f"t:refresh:{jti}")
    if not stored:
        raise ApiError(ErrorCode.UNAUTHORIZED, "refresh token revoked or expired", http_status=401)
    await redis.t_delete(f"t:refresh:{jti}")
    new_access = jwt_utils.issue_token(
        user_id=payload["user_id"], tenant_id=payload["tenant_id"],
        secret=s.jwt_secret, ttl_seconds=s.jwt_ttl_seconds,
    )
    new_refresh = jwt_utils.issue_refresh_token(
        user_id=payload["user_id"], tenant_id=payload["tenant_id"],
        secret=s.jwt_secret, ttl_seconds=s.jwt_refresh_ttl_seconds,
    )
    new_rt = jwt_utils.decode_token(new_refresh, s.jwt_secret)
    await redis.t_set(f"t:refresh:{new_rt['jti']}", payload["user_id"],
                      ex=s.jwt_refresh_ttl_seconds)
    return {"access_token": new_access, "refresh_token": new_refresh,
            "expires_in": s.jwt_ttl_seconds}


async def anonymize_user(*, user_id: str) -> None:
    """匿名化用户账号（GDPR Right to erasure）。

    匿名化而非物理删除，保护外键完整性。
    操作：user_account 匿名化 → tenant_member 删除 → API key 吊销 → Redis 清理。
    """
    async with db.admin_db_session() as conn:
        row = await conn.fetchrow(
            "SELECT id, email FROM user_account WHERE id = $1", user_id,
        )
        if not row:
            raise ApiError(ErrorCode.NOT_FOUND, "user not found", http_status=404)
        old_email = row["email"]

        anonymized_email = f"deleted-{secrets.token_hex(8)}@anonymized"
        await conn.execute(
            "UPDATE user_account SET email=$1, phone='', name='Deleted User',"
            " password_hash='', status='deleted', updated_at=NOW() WHERE id=$2",
            anonymized_email, user_id,
        )
        await conn.execute("DELETE FROM tenant_member WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM user_consent WHERE user_id = $1", user_id)
        # GDPR erasure：清该用户邮箱作为收件人的投递日志（notification_log.recipient = email PII）
        if old_email:
            await conn.execute(
                "DELETE FROM notification_log WHERE recipient = $1", old_email,
            )

        apps = await conn.fetch(
            "SELECT id FROM app WHERE tenant_id = $1", EXTERNAL_PUBLIC_TENANT,
        )
        for app in apps:
            await conn.execute(
                "UPDATE api_key SET status='revoked', revoked_at=NOW()"
                " WHERE app_id=$1 AND status='active'",
                app["id"],
            )

    try:
        for pattern in ("t:refresh:*", "t:verify:*"):
            cursor: bytes | str = "0"
            while cursor:
                cursor, keys = await redis.raw_client().scan(
                    cursor, match=pattern, count=100,
                )
                if keys:
                    await redis.raw_client().delete(*keys)
    except Exception as e:
        log.warning("redis_cleanup_partial", error=str(e))

    log.info("user_anonymized", user_id=user_id)


CONSENT_PURPOSES: dict[str, str] = {
    "account_management": "存储账号信息以提供服务",
    "api_usage_tracking": "记录 API 调用用于计费和监控",
}


async def list_consents(*, user_id: str) -> list[dict]:
    """查询用户的所有同意记录。"""
    async with db.admin_db_session() as conn:
        rows = await conn.fetch(
            "SELECT purpose, status, granted_at, updated_at"
            " FROM user_consent WHERE user_id = $1 ORDER BY purpose",
            user_id,
        )
    result = []
    for r in rows:
        result.append({
            "purpose": r["purpose"],
            "description": CONSENT_PURPOSES.get(r["purpose"], ""),
            "status": r["status"],
            "granted_at": r["granted_at"].isoformat(),
            "updated_at": r["updated_at"].isoformat(),
        })
    return result


async def withdraw_consent(*, user_id: str) -> None:
    """撤回所有同意 → 触发账号匿名化（GDPR right-to-erasure）。

    撤回即擦除（与 delete_account 等效 erasure，不同语义入口）。
    """
    await anonymize_user(user_id=user_id)
    log.info("consent_withdraw_triggered_erasure", user_id=user_id)


async def export_user_data(*, user_id: str) -> dict:
    """导出用户个人数据（GDPR Right to portability）。

    含：账户信息、租户关系、应用、API Key、计费记录。
    """
    async with db.admin_db_session() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, phone, name, verification_level, status,"
            " created_at FROM user_account WHERE id = $1", user_id,
        )
        if not row:
            raise ApiError(ErrorCode.NOT_FOUND, "user not found", http_status=404)

        members = await conn.fetch(
            "SELECT tenant_id, role FROM tenant_member WHERE user_id = $1", user_id,
        )

        apps = await conn.fetch(
            "SELECT id, name, type, status, created_at FROM app"
            " WHERE tenant_id = $1 ORDER BY created_at DESC",
            EXTERNAL_PUBLIC_TENANT,
        )

        api_keys = []
        for app in apps:
            keys = await conn.fetch(
                "SELECT id, name, scopes, status, created_at, expires_at FROM api_key"
                " WHERE app_id = $1 ORDER BY created_at DESC",
                app["id"],
            )
            for k in keys:
                api_keys.append({
                    "id": k["id"], "app_id": app["id"], "app_name": app["name"],
                    "name": k["name"], "scopes": list(k["scopes"] or []),
                    "status": k["status"], "created_at": k["created_at"].isoformat(),
                    "expires_at": k["expires_at"].isoformat() if k["expires_at"] else None,
                })

        billing_records = await conn.fetch(
            "SELECT period, plan_name, total_calls, total_tokens,"
            " base_cents, overage_cents, status, created_at"
            " FROM billing_record WHERE tenant_id = $1"
            " ORDER BY period DESC LIMIT 12",
            EXTERNAL_PUBLIC_TENANT,
        )

    from apihub_core.pii import maybe_decrypt  # noqa: PLC0415

    return {
        "user_id": row["id"],
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "account": {
            "email": row["email"],
            "phone": maybe_decrypt(row["phone"] or ""),
            "name": maybe_decrypt(row["name"] or ""),
            "verification_level": row["verification_level"],
            "status": row["status"],
            "created_at": row["created_at"].isoformat(),
        },
        "tenants": [{"tenant_id": m["tenant_id"], "role": m["role"]} for m in members],
        "apps": [dict(a) for a in apps],
        "api_keys": api_keys,
        "billing_records": [
            {
                "period": r["period"],
                "plan_name": r["plan_name"],
                "total_calls": r["total_calls"],
                "total_tokens": r["total_tokens"],
                "base_cents": r["base_cents"],
                "overage_cents": r["overage_cents"],
                "status": r["status"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in billing_records
        ],
    }
