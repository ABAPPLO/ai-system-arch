-- 15-sso-user-account.sql — admin 钉钉 SSO：user_account 加 SSO 身份列 + 平台超管列。
-- 幂等：全部 IF NOT EXISTS。apply 须 as owner apihub（同 13-/14- 约束）。
-- email 保持 NOT NULL：SSO 用户由 upsert_sso_user 合成 "<union_id>@<provider>.sso.local"。

ALTER TABLE user_account ADD COLUMN IF NOT EXISTS sso_provider text;
ALTER TABLE user_account ADD COLUMN IF NOT EXISTS sso_union_id text;
ALTER TABLE user_account
    ADD COLUMN IF NOT EXISTS is_platform_admin boolean NOT NULL DEFAULT false;

-- SSO 身份唯一（仅 SSO 用户；密码用户两列 NULL，部分索引排除）。
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_sso
    ON user_account (sso_provider, sso_union_id)
    WHERE sso_provider IS NOT NULL;
