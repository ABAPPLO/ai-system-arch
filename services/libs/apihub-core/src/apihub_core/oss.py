"""MinIO / S3-compatible 对象存储客户端。

提供 `put_object` 函数，使用 AWS Signature V4 签名通过 httpx PUT 对象。

用法：
    ok = await put_object("audit-archive", "2026/01/tenant-t1.jsonl.gz", data)
"""

import hashlib
import hmac
from datetime import datetime

from apihub_core.config import get_settings
from apihub_core.logging import get_logger

log = get_logger(__name__)

_SERVICE = "s3"
_REGION = "us-east-1"  # MinIO 忽略 region；阿里云 OSS 可覆盖
_ALGORITHM = "AWS4-HMAC-SHA256"


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _get_signature_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = _sign(secret.encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    k_signing = _sign(k_service, "aws4_request")
    return k_signing


async def put_object(bucket: str, key: str, data: bytes) -> bool:
    """用 AWS V4 签名 PUT 对象到 S3-compatible 存储。

    参数：
        bucket: bucket 名
        key:    对象路径（如 "2026/01/tenant-t1.jsonl.gz"）
        data:   原始字节内容

    返回：
        True 表示 HTTP 2xx，False 表示失败（best-effort，不抛）。
    """
    import httpx

    settings = get_settings()
    base_url = settings.oss_endpoint.rstrip("/")
    scheme = "https" if settings.oss_secure else "http"
    if not base_url.startswith("http://") and not base_url.startswith("https://"):
        base_url = f"{scheme}://{base_url}"
    url = f"{base_url}/{bucket}/{key}"

    now = datetime.utcnow()
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    content_hash = hashlib.sha256(data).hexdigest()

    headers: dict[str, str] = {
        "Host": url.split("/")[2],
        "X-Amz-Content-Sha256": content_hash,
        "X-Amz-Date": amz_date,
    }
    sorted_keys = sorted(headers.keys())
    signed_headers_str = ";".join(k.lower() for k in sorted_keys)

    # Canonical Request
    canonical_headers = "".join(f"{k.lower()}:{headers[k]}\n" for k in sorted_keys)
    canonical_request = (
        f"PUT\n/{bucket}/{key}\n\n{canonical_headers}{signed_headers_str}\n{content_hash}"
    )
    canonical_request_hash = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()

    # String to Sign
    credential_scope = f"{date_stamp}/{_REGION}/{_SERVICE}/aws4_request"
    string_to_sign = f"{_ALGORITHM}\n{amz_date}\n{credential_scope}\n{canonical_request_hash}"

    # Signing Key
    signing_key = _get_signature_key(
        settings.oss_secret_key,
        date_stamp,
        _REGION,
        _SERVICE,
    )
    signature = hmac.new(
        signing_key,
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    # Authorization Header
    headers["Authorization"] = (
        f"{_ALGORITHM} Credential={settings.oss_access_key}/{credential_scope},"
        f" SignedHeaders={signed_headers_str}, Signature={signature}"
    )

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.put(url, content=data, headers=headers)
        if resp.status_code < 300:
            log.info("oss_put_ok", bucket=bucket, key=key, size=len(data))
            return True
        log.warning(
            "oss_put_failed",
            bucket=bucket,
            key=key,
            status=resp.status_code,
            body=resp.text[:200],
        )
        return False
    except Exception as e:
        log.warning("oss_put_error", bucket=bucket, key=key, error=str(e))
        return False
