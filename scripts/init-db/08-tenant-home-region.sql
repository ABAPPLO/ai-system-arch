-- 08-tenant-home-region.sql
-- Multi-region active/active: tenant table gets home_region
ALTER TABLE tenant
  ADD COLUMN IF NOT EXISTS home_region VARCHAR(20) NOT NULL DEFAULT 'sh';

-- Migrate ~1/3 active tenants to bj for geographic distribution
UPDATE tenant SET home_region = 'bj'
WHERE id % 3 = 0 AND status = 'active';

CREATE INDEX IF NOT EXISTS idx_tenant_home_region ON tenant(home_region);
COMMENT ON COLUMN tenant.home_region IS 'Home region (sh=cn-shanghai, bj=cn-beijing)';
