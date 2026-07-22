#!/usr/bin/env python3
"""R2e 回填：存量 webhook_subscription.secret(明文) → secret_encrypted(AESGCM) + scrub 明文。

幂等：二次跑无 secret IS NOT NULL 行即 no-op。
apply 顺序：先 apply 14-hmac-secret.sql（ADD secret_encrypted）→ 跑本脚本 →
secret 列保留 NULL 占位（不 DROP，见 spec §4.2 rationale：保留列避免历史 schema 漂移）。

用法：
    HMAC_SECRET_KEY=<32-byte-hex> python scripts/init-db/14-backfill-webhook-secret.py \\
        "postgresql://apihub:apihub_dev_pwd@localhost:5432/apihub"

HMAC_SECRET_KEY 必须与 auth/dispatcher/notification 服务所用一致（同一 envelope key）。
"""

import asyncio
import sys

import asyncpg
from apihub_core.crypto import encrypt_secret


async def main(pg_dsn: str) -> None:
    conn = await asyncpg.connect(pg_dsn)
    try:
        rows = await conn.fetch(
            "SELECT id, secret FROM webhook_subscription "
            "WHERE secret IS NOT NULL AND secret_encrypted IS NULL"
        )
        if not rows:
            print("backfill: no rows; no-op")
            return
        for r in rows:
            enc = encrypt_secret(r["secret"])
            await conn.execute(
                "UPDATE webhook_subscription SET secret_encrypted=$1, secret=NULL WHERE id=$2",
                enc,
                r["id"],
            )
        print(f"backfill: encrypted + scrubbed {len(rows)} webhook secrets")
    finally:
        await conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: 14-backfill-webhook-secret.py <pg_dsn>", file=sys.stderr)
        sys.exit(2)
    asyncio.run(main(sys.argv[1]))
