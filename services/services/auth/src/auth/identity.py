"""外部开发者身份业务 —— 注册 / 邮箱验证 / 登录。

复用现有表：user_account(schema:32) + tenant_member(:47)。
邮箱验证 token 存 Redis（dev stub 不真发邮件）。
"""

import secrets

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
    """注册：写 user_account(pending) + Redis 验证 token。重复邮箱 → 409。"""
    async with db.admin_db_session() as conn:
        exists = await conn.fetchval(
            "SELECT id FROM user_account WHERE email = $1", email
        )
        if exists:
            raise ApiError(ErrorCode.CONFLICT, "email already registered", http_status=409)

        user_id = f"u_{secrets.token_hex(8)}"
        await conn.execute(
            """
            INSERT INTO user_account (id, email, phone, password_hash, name,
                                       verification_level, status)
            VALUES ($1, $2, $3, $4, $5, 'email', 'pending')
            """,
            user_id, email, phone, _hash_password(password), name,
        )

    verify_token = secrets.token_urlsafe(32)
    await redis.t_set(f"t:verify:{verify_token}", user_id, ex=VERIFY_TTL)
    log.info("user_registered", user_id=user_id, email=email)
    return {"user_id": user_id, "status": "pending",
            "verification_level": "email", "verify_token": verify_token}


async def verify_email(token: str) -> dict:
    """验证邮箱：标 status=active + 加 tenant_member(external-public)。"""
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
            """
            INSERT INTO tenant_member (id, tenant_id, user_id, role)
            VALUES ($1, $2, $3, 'developer')
            ON CONFLICT (tenant_id, user_id) DO NOTHING
            """,
            f"tm_{secrets.token_hex(8)}", EXTERNAL_PUBLIC_TENANT, user_id,
        )
    await redis.t_delete(f"t:verify:{token}")
    log.info("email_verified", user_id=user_id)
    return {"user_id": user_id, "name": row["name"], "status": "active",
            "tenant_id": EXTERNAL_PUBLIC_TENANT}


async def login(*, email: str, password: str) -> dict:
    """登录：bcrypt 校验 + status=active 检查 → 签 JWT + refresh token。"""
    from apihub_core import jwt_utils
    from apihub_core.config import get_settings

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
            "user": {"id": row["id"], "name": row["name"], "tenant_id": EXTERNAL_PUBLIC_TENANT}}


async def refresh_access(refresh_token: str) -> dict:
    """用 refresh token 换新 access_token + refresh_token（rotation）。"""
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
