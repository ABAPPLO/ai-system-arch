"""外部开发者身份业务 —— 注册 / 邮箱验证 / 登录 / 账号删除。

复用现有表：user_account(schema:32) + tenant_member(:47)。
邮箱验证 token 存 Redis（dev stub 不真发邮件）。
"""

import secrets
from datetime import UTC, datetime

import bcrypt
from apihub_core import db, redis
from apihub_core.errors import ApiError, ErrorCode
from apihub_core.logging import get_logger

log = get_logger(__name__)

EXTERNAL_PUBLIC_TENANT = "external-public"
PLATFORM_TENANT = (
    "platform"  # admin JWT tenant_id 标签（admin_db_session 旁路 RLS，无需 tenant 行）
)
VERIFY_TTL = 86400  # 24h


def _bootstrap_admin_unionids(settings: "object") -> set[str]:
    """解析 BOOTSTRAP_ADMIN_DINGTALK_UNIONIDS 逗号列表（容空白/空段）。"""
    raw = getattr(settings, "bootstrap_admin_dingtalk_unionids", "") or ""
    return {part.strip() for part in raw.split(",") if part.strip()}


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


async def create_user(*, email: str, password: str, phone: str, name: str) -> dict:
    from apihub_core.pii import encrypt_pii  # noqa: PLC0415

    async with db.admin_db_session() as conn:
        exists = await conn.fetchval("SELECT id FROM user_account WHERE email = $1", email)
        if exists:
            raise ApiError(ErrorCode.CONFLICT, "email already registered", http_status=409)
        user_id = f"u_{secrets.token_hex(8)}"
        await conn.execute(
            "INSERT INTO user_account (id, email, phone, password_hash, name,"
            " verification_level, status)"
            " VALUES ($1, $2, $3, $4, $5, 'email', 'pending')",
            user_id,
            email,
            encrypt_pii(phone),
            _hash_password(password),
            encrypt_pii(name),
        )
        # GDPR: 记录数据使用同意
        for purpose in ("account_management", "api_usage_tracking"):
            await conn.execute(
                "INSERT INTO user_consent (user_id, purpose, status) VALUES ($1, $2, 'granted')"
                " ON CONFLICT (user_id, purpose) DO NOTHING",
                user_id,
                purpose,
            )

    verify_token = secrets.token_urlsafe(32)
    await redis.t_set(f"t:verify:{verify_token}", user_id, ex=VERIFY_TTL)
    log.info("user_registered", user_id=user_id, email=email)
    return {
        "user_id": user_id,
        "status": "pending",
        "verification_level": "email",
        "verify_token": verify_token,
    }


async def verify_email(token: str) -> dict:
    from apihub_core.pii import maybe_decrypt  # noqa: PLC0415

    user_id = await redis.t_get(f"t:verify:{token}")
    if not user_id:
        raise ApiError(ErrorCode.INVALID_INPUT, "invalid or expired token", http_status=400)
    async with db.admin_db_session() as conn:
        row = await conn.fetchrow(
            "UPDATE user_account SET status='active', last_login_at=NOW() "
            "WHERE id=$1 RETURNING id, name",
            user_id,
        )
        if not row:
            raise ApiError(ErrorCode.NOT_FOUND, "user not found", http_status=404)
        await conn.execute(
            "INSERT INTO tenant_member (id, tenant_id, user_id, role)"
            " VALUES ($1, $2, $3, 'developer') ON CONFLICT DO NOTHING",
            f"tm_{secrets.token_hex(8)}",
            EXTERNAL_PUBLIC_TENANT,
            user_id,
        )
    await redis.t_delete(f"t:verify:{token}")
    return {
        "user_id": user_id,
        "name": maybe_decrypt(row["name"]),
        "status": "active",
        "tenant_id": EXTERNAL_PUBLIC_TENANT,
    }


async def login(*, email: str, password: str) -> dict:
    from apihub_core import jwt_utils
    from apihub_core.config import get_settings
    from apihub_core.pii import maybe_decrypt  # noqa: PLC0415

    async with db.admin_db_session() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, name, password_hash, status FROM user_account WHERE email=$1",
            email,
        )
    if not row or not row["password_hash"] or not _check_password(password, row["password_hash"]):
        raise ApiError(ErrorCode.UNAUTHORIZED, "invalid email or password", http_status=401)
    if row["status"] != "active":
        raise ApiError(ErrorCode.FORBIDDEN, "email not verified", http_status=403)
    s = get_settings()
    access = jwt_utils.issue_token(
        user_id=row["id"],
        tenant_id=EXTERNAL_PUBLIC_TENANT,
        secret=s.jwt_secret,
        ttl_seconds=s.jwt_ttl_seconds,
    )
    refresh = jwt_utils.issue_refresh_token(
        user_id=row["id"],
        tenant_id=EXTERNAL_PUBLIC_TENANT,
        secret=s.jwt_secret,
        ttl_seconds=s.jwt_refresh_ttl_seconds,
    )
    rt_payload = jwt_utils.decode_token(refresh, s.jwt_secret)
    await redis.t_set(f"t:refresh:{rt_payload['jti']}", row["id"], ex=s.jwt_refresh_ttl_seconds)
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": s.jwt_ttl_seconds,
        "user": {
            "id": row["id"],
            "name": maybe_decrypt(row["name"]),
            "tenant_id": EXTERNAL_PUBLIC_TENANT,
        },
    }


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
        user_id=payload["user_id"],
        tenant_id=payload["tenant_id"],
        secret=s.jwt_secret,
        ttl_seconds=s.jwt_ttl_seconds,
    )
    new_refresh = jwt_utils.issue_refresh_token(
        user_id=payload["user_id"],
        tenant_id=payload["tenant_id"],
        secret=s.jwt_secret,
        ttl_seconds=s.jwt_refresh_ttl_seconds,
    )
    new_rt = jwt_utils.decode_token(new_refresh, s.jwt_secret)
    await redis.t_set(
        f"t:refresh:{new_rt['jti']}", payload["user_id"], ex=s.jwt_refresh_ttl_seconds
    )
    return {
        "access_token": new_access,
        "refresh_token": new_refresh,
        "expires_in": s.jwt_ttl_seconds,
    }


async def upsert_sso_user(*, union_id: str, name: str, provider: str = "dingtalk") -> dict:
    """SSO 登录 upsert 用户身份（admin 钉钉登录用）。

    首次登录：建 user_account（合成 email 满足 UNIQUE NOT NULL；verification_level
    'enterprise'；status 'active'）。复用：按 (provider, union_id) 命中则更新 last_login + 名字。
    bootstrap 命中 → is_platform_admin=true（仅设不撤）；未命中保留原值（默认 false）。
    不落 tenant_member（admin 是平台级全局身份，admin_db_session 旁路 RLS）。
    """
    from apihub_core.config import get_settings  # noqa: PLC0415
    from apihub_core.pii import encrypt_pii  # noqa: PLC0415

    s = get_settings()
    is_admin = union_id in _bootstrap_admin_unionids(s)
    synth_email = f"{union_id}@{provider}.sso.local"

    async with db.admin_db_session() as conn:
        row = await conn.fetchrow(
            "SELECT id, is_platform_admin FROM user_account"
            " WHERE sso_provider=$1 AND sso_union_id=$2",
            provider,
            union_id,
        )
        if row:
            user_id = row["id"]
            admin_clause = ", is_platform_admin=true" if is_admin else ""
            await conn.execute(
                "UPDATE user_account SET last_login_at=NOW(), name=$2"  # noqa: S608
                + admin_clause
                + " WHERE id=$1",
                user_id,
                encrypt_pii(name),
            )
            cur_admin = is_admin or bool(row["is_platform_admin"])
        else:
            user_id = f"u_{secrets.token_hex(8)}"
            await conn.execute(
                "INSERT INTO user_account"
                " (id, email, name, verification_level, status, sso_provider, sso_union_id,"
                "  is_platform_admin, last_login_at)"
                " VALUES ($1, $2, $3, 'enterprise', 'active', $4, $5, $6, NOW())",
                user_id,
                synth_email,
                encrypt_pii(name),
                provider,
                union_id,
                is_admin,
            )
            cur_admin = is_admin

    log.info("sso_user_upserted", user_id=user_id, provider=provider, is_platform_admin=cur_admin)
    return {"user_id": user_id, "name": name, "is_platform_admin": cur_admin}


async def anonymize_user(*, user_id: str) -> None:
    """匿名化用户账号（GDPR Right to erasure）。

    匿名化而非物理删除，保护外键完整性。
    操作：user_account 匿名化 → tenant_member 删除 → user_consent 删除 →
    notification_log 投递日志清理（按旧 recipient=email）→ Redis 清理。
    租户级 api_key 不动（external-public 共享租户，app/api_key 无 user 归属字段，
    erasure 仅清该用户本人 PII）。全程在同一 admin_db_session 事务内
    （任一失败回滚，不残留半擦除状态），并写一条 audit_log(reason=gdpr_erasure)。
    """
    async with db.admin_db_session(audit_reason="gdpr_erasure") as conn:
        row = await conn.fetchrow(
            "SELECT id, email FROM user_account WHERE id = $1",
            user_id,
        )
        if not row:
            raise ApiError(ErrorCode.NOT_FOUND, "user not found", http_status=404)
        old_email = row["email"]

        anonymized_email = f"deleted-{secrets.token_hex(8)}@anonymized"
        await conn.execute(
            "UPDATE user_account SET email=$1, phone='', name='Deleted User',"
            " password_hash='', status='deleted', updated_at=NOW() WHERE id=$2",
            anonymized_email,
            user_id,
        )
        await conn.execute("DELETE FROM tenant_member WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM user_consent WHERE user_id = $1", user_id)
        # GDPR erasure：清该用户邮箱作为收件人的投递日志（notification_log.recipient = email PII）
        if old_email:
            await conn.execute(
                "DELETE FROM notification_log WHERE recipient = $1",
                old_email,
            )

    try:
        for pattern in ("t:refresh:*", "t:verify:*"):
            cursor: bytes | str = "0"
            while cursor:
                cursor, keys = await redis.raw_client().scan(  # type: ignore
                    cursor,  # type: ignore
                    match=pattern,
                    count=100,
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
        result.append(
            {
                "purpose": r["purpose"],
                "description": CONSENT_PURPOSES.get(r["purpose"], ""),
                "status": r["status"],
                "granted_at": r["granted_at"].isoformat(),
                "updated_at": r["updated_at"].isoformat(),
            }
        )
    return result


async def withdraw_consent(*, user_id: str) -> None:
    """撤回所有同意 → 触发账号匿名化（GDPR right-to-erasure）。

    撤回即擦除（与 delete_account 等效 erasure，不同语义入口）。
    """
    await anonymize_user(user_id=user_id)
    log.info("consent_withdraw_triggered_erasure", user_id=user_id)


async def export_user_data(*, user_id: str) -> dict:
    """导出用户个人数据（GDPR Right to portability）。

    含：账户信息、租户关系。租户级共享数据（apps/api_keys/billing_records）
    不归属个人（external-public 共享租户、无 user 归属字段），不在导出范围。
    """
    async with db.admin_db_session() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, phone, name, verification_level, status,"
            " created_at FROM user_account WHERE id = $1",
            user_id,
        )
        if not row:
            raise ApiError(ErrorCode.NOT_FOUND, "user not found", http_status=404)

        members = await conn.fetch(
            "SELECT tenant_id, role FROM tenant_member WHERE user_id = $1",
            user_id,
        )

    from apihub_core.pii import maybe_decrypt  # noqa: PLC0415

    return {
        "user_id": row["id"],
        "exported_at": datetime.now(UTC).isoformat(),
        "account": {
            "email": row["email"],
            "phone": maybe_decrypt(row["phone"] or ""),
            "name": maybe_decrypt(row["name"] or ""),
            "verification_level": row["verification_level"],
            "status": row["status"],
            "created_at": row["created_at"].isoformat(),
        },
        "tenants": [{"tenant_id": m["tenant_id"], "role": m["role"]} for m in members],
    }
