local _M = { version = 0.1, priority = 2000, name = "tenant-affinity" }

_M.schema = {
    type = "object",
    properties = {
        write_methods = {
            type = "array",
            items = { type = "string" },
            default = { "POST", "PUT", "PATCH", "DELETE" },
        },
    },
}

function _M.rewrite(conf, ctx)
    local consumer = ctx.consumer
    if not consumer or not consumer.home_region then
        return
    end

    local home = consumer.home_region
    local curr = os.getenv("HOME_REGION") or "sh"
    if home == curr then
        return
    end

    local method = ctx.var.request_method
    for _, m in ipairs(conf.write_methods) do
        if m == method then
            local gw = {
                sh = "https://api-sh.apihub.com",
                bj = "https://api-bj.apihub.com",
            }
            local uri = ctx.var.uri
            if ctx.var.is_args == 1 then
                uri = uri .. "?" .. ctx.var.args
            end
            ngx.status = 302
            ngx.header["Location"] = (gw[home] or "") .. uri
            return ngx.exit(302)
        end
    end
end

return _M
