-- GDPR 同意记录表
CREATE TABLE IF NOT EXISTS user_consent (
    user_id     VARCHAR(64) NOT NULL REFERENCES user_account(id),
    purpose     VARCHAR(64) NOT NULL,
    status      VARCHAR(20) NOT NULL DEFAULT 'granted',
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ip_address  VARCHAR(45),
    PRIMARY KEY (user_id, purpose)
);
CREATE INDEX IF NOT EXISTS idx_consent_user ON user_consent(user_id, status);
