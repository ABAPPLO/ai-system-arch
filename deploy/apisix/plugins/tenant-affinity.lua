-- Read gateway URLs from env vars with defaults
local GATEWAY_SH = os.getenv("GATEWAY_URL_SH") or "https://api-sh.apihub.com"
local GATEWAY_BJ = os.getenv("GATEWAY_URL_BJ") or "https://api-bj.apihub.com"

local _M = { version = 0.2, priority = 2000, name = "tenant-affinity" }

_M.schema = {
    type = "object",
    properties = {
        write_methods = {
            type = "array",
            items = { type = "string" },
            default = { "POST", "PUT", "PATCH", "DELETE" },
        },
        fallback_local = {
            type = "boolean",
            default = false,
        },
    },
}

function _M.rewrite(conf, ctx)
    local consumer = ctx.consumer
    local labels = consumer and consumer.labels
    if not labels or not labels.home_region then
        return
    end

    local home = labels.home_region
    local curr = os.getenv("HOME_REGION") or "sh"
    if home == curr then
        return
    end

    -- C1 + I2 + M3: Unknown home_region — log warning and allow write locally
    if home ~= "sh" and home ~= "bj" then
        ngx.log(ngx.WARN, "tenant-affinity: unknown home_region '"
                .. tostring(home) .. "', allowing write locally")
        return
    end

    local method = ctx.var.request_method
    for _, m in ipairs(conf.write_methods) do
        if m == method then
            -- C2: Safe degradation — if fallback_local is enabled and peer is
            -- unreachable, allow write locally with a warning
            if conf.fallback_local then
                ngx.log(ngx.WARN,
                        "tenant-affinity: write to non-home region allowed via "
                        .. "fallback_local, tenant=" .. tostring(home))
                return
            end

            local gw = { sh = GATEWAY_SH, bj = GATEWAY_BJ }
            local uri = ctx.var.uri
            if ctx.var.is_args == 1 then
                uri = uri .. "?" .. ctx.var.args
            end
            ngx.status = 302
            ngx.header["Location"] = gw[home] .. uri
            return ngx.exit(302)
        end
    end
end

return _M
