-- Phase 4 AI 网关 —— ai_provider / ai_provider_key / ai_model_route

CREATE TABLE IF NOT EXISTS ai_provider (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text NOT NULL UNIQUE,
    provider_type   text NOT NULL CHECK (provider_type IN ('openai_compatible', 'anthropic')),
    base_url        text NOT NULL,
    default_model   text NOT NULL DEFAULT '',
    status          text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS set_updated_at ON ai_provider;
CREATE TRIGGER set_updated_at BEFORE UPDATE ON ai_provider
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

CREATE TABLE IF NOT EXISTS ai_provider_key (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id     uuid NOT NULL REFERENCES ai_provider(id) ON DELETE CASCADE,
    key_alias       text NOT NULL DEFAULT '',
    key_encrypted   text NOT NULL,
    key_prefix      text NOT NULL DEFAULT '',
    expires_at      timestamptz,
    status          text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'expired', 'revoked')),
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    updated_at      timestamptz NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS set_updated_at ON ai_provider_key;
CREATE TRIGGER set_updated_at BEFORE UPDATE ON ai_provider_key
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

CREATE TABLE IF NOT EXISTS ai_model_route (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    model_pattern       text NOT NULL,
    target_provider_id  uuid NOT NULL REFERENCES ai_provider(id),
    target_model        text NOT NULL,
    priority            int NOT NULL DEFAULT 0,
    status              text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at          timestamptz NOT NULL DEFAULT NOW(),
    updated_at          timestamptz NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS set_updated_at ON ai_model_route;
CREATE TRIGGER set_updated_at BEFORE UPDATE ON ai_model_route
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

GRANT SELECT, INSERT, UPDATE, DELETE ON ai_provider TO apihub_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ai_provider_key TO apihub_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ai_model_route TO apihub_app;
