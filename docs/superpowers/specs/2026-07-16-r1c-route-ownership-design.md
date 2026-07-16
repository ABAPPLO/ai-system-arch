# R1c spec — 路由归属：APISIX 动态路由 + dispatcher 退纯转发 + api-registry 下发

日期：2026-07-16 · 分支 `fix/r1c-route-ownership` · 依据：审计 `phase4-audit-findings.md` §3.1 + §9-A，fix-program §5 Wave 1 R1c 行。

## 问题

**§3.1（stub）**：`api-registry` 的 publish/retire 只改 PG 状态，**不下发 APISIX 数据面**——`apisix_client.py` 不存在，`routes.py:147-149`（publish）与 `:219`（retire）是注释掉的 TODO。结果：「发布即可调、下线即 410」闭环没接通；真正把流量导入 dispatcher 的是 phase2 手动 helm 装的静态 route，不是 api-registry 自动下发。

**§9-A（架构缺陷）**：路由真相分散三处——PG（api-registry 元数据）、Redis（dispatcher resolver 缓存）、APISIX（静态 route）。APISIX 做 `/dispatch/*` 路由 + key-auth，**dispatcher 又自己 `resolve_by_path` 从 PG re-resolve 一遍**——两层路由逻辑、职责不清。即使补上 stub，「APISIX 和 dispatcher 谁是真路由层」的重叠仍在。

**顺手发现（resolve_by_header 既有缺陷）**：`dispatcher/resolver.py:36` `resolve_by_header` 只匹配 `status='published'`，于是 **deprecated 版本（设计要求仍可调用）走 header 路径会 404**，retired 版本也没有 410 语义。R1c 修路由归属时一并修这条生命周期映射。

## 走法（已定：全量，贴 roadmap）

**APISIX 是 prod 唯一路由层；dispatcher 退纯转发**：
- api-registry publish → 通过 APISIX Admin API **upsert 一条路由**（uri=base_path+path、methods、upstream=dispatcher、`proxy-rewrite` 注入 `X-API-Version-Id` + 把 path 重写成 `/dispatch/...`）。
- 请求经 APISIX 匹配路由 → 注入 header + 改写 path → 转给 dispatcher → dispatcher `resolve_by_header(version_id)` 拿 snapshot → 转后端。
- dispatcher **删除 `resolve_by_path`**（§9-A 重叠清理），`/dispatch` 强制要求 `X-API-Version-Id`（APISIX 注入；dev 手动传）。
- **retire→410 由 dispatcher 兜底**（不依赖 APISIX 410 插件）：`resolve_by_header` 放开 `published`+`deprecated` 可路由，`retired` → `410 Gone`。retire 不删 APISIX 路由（留它在，dispatcher 返回 410）；避免启用 `serverless-pre-function` 等 APISIX 插件的 infra 改动。

为何不删 APISIX 路由换 410：APISIX 无内置「返回任意状态码」能力（需 serverless 插件，未在 `apisix-values.yaml` 启用，要 helm upgrade）；让已部署的 dispatcher 按版本状态返回 410，零 infra 改动且语义正确。

## 改动清单

### ① 新增 `api_registry/apisix_client.py`（APISIX Admin API 客户端）
- `publish_route(*, version_id, method, path, base_path) -> None`：
  - 归一化 path：`{var}` → APISIX `:var`（radixtree 段匹配）。
  - `PUT {APISIX_ADMIN_URL}/apisix/admin/routes/{version_id}`，header `X-API-KEY: <APISIX_ADMIN_KEY>`，body：
    ```json
    {
      "uri": "<base_path><path>",
      "methods": ["<METHOD>"],
      "upstream": {"type": "roundrobin", "nodes": {"<DISPATCHER_UPSTREAM>": 1}},
      "plugins": {
        "proxy-rewrite": {
          "regex_uri": ["^/(.*)$", "/dispatch/$1"],
          "headers": {"set": ["X-API-Version-Id: <version_id>"]}
        }
      }
    }
    ```
  - 非 2xx → `ApiError(ErrorCode.INTERNAL, "apisix admin publish failed", http_status=502)`。
- `retire_route(version_id) -> None`：**保留为可选/no-op**（本设计 retire 不删路由；留函数供后续清理 stale 路由用，R1c 内不被 retire 调用，避免误用）。
- `_admin_request(method, path, **kw)`：httpx 封装（timeout 3s，X-API-KEY header）。

### ② api-registry publish/retire 接线（`routes.py`）
- **publish**（`:135-164`）：在 `UPDATE status='published'` **之前**先 `await apisix_client.publish_route(version_id=..., method=row['method'], path=row['path'], base_path=<从 api 表查>)`。APISIX 下发成功才置 published（避免「DB 说 published 但 APISIX 没路由」的窗口）。需要 `base_path`：publish handler 内 join/查 `api.base_path`（meta 已有 `SELECT * FROM api_version`，补查 api 表或 LATERAL join）。
- **retire**（`:196-228`）：去掉 TODO 注释；**不调用** apisix_client（dispatcher 兜底 410）。加注释说明「retire 不摘 APISIX 路由，由 dispatcher 按 retired 状态返回 410」。
- **deprecate**：不动（路由留着的 deprecated 版本由 dispatcher 放行）。

### ③ dispatcher 路由归属清理（`resolver.py` + `routes.py`）
- `resolve_by_header`（`:23-47`）：
  - SQL 放开状态：`WHERE id=$1 AND status IN ('published','deprecated')`（修 deprecated 不可路由的既有缺陷）。
  - 查不到时区分：若版本存在但 `status='retired'` → `ApiError(ErrorCode.API_RETIRED, "...", http_status=410)`（新增/复用错误码，映射 410 Gone）；否则 `API_NOT_PUBLISHED`（404）。
- **删除 `resolve_by_path`**（`:50-74`）+ `_match_path`（如仅它用）。codegraph 确认 `resolve_by_path` 无覆盖测试、仅 `routes.py:74` 一处调用。
- `routes.py` `dispatch`（`:64-107`）：去掉 `version_id = request.headers.get(...); if version_id: ... else: resolve_by_path` 分支，改为**强制要求 `X-API-Version-Id`**：无该 header → `ApiError(ErrorCode.BAD_REQUEST, "missing X-API-Version-Id (must enter via APISIX)", http_status=400)`；有则 `resolve_by_header`。
- `ErrorCode` 缺 `API_RETIRED` 则补（`apihub_core/errors.py`），映射 http 410。

### ④ 配置（api-registry）
- `apihub_core/config.Settings`（或 api-registry 本地 settings）加：`apisix_admin_url`（已有 configmap `APISIX_ADMIN_URL`）、`apisix_admin_key`（新，从 Secret 读 `APISIX_ADMIN_KEY`）、`dispatcher_upstream`（新，默认 `dispatcher.apihub-system:8001`）。
- `deploy/k8s/services/api-registry/configmap.yaml`：加 `DISPATCHER_UPSTREAM`；Secret 占位加 `APISIX_ADMIN_KEY`（真值走 Sealed Secret，不入 git）。
- `.env.dev`：加 `APISIX_ADMIN_KEY`（dev/kind 值，从 kind 实际部署取）+ `DISPATCHER_UPSTREAM`。

### ⑤ 文档
- `docs/aggregate-ownership.md`（R0c 建的）：追加/明确「**路由归属 = APISIX**（动态路由 + 注入 X-API-Version-Id）；dispatcher 是纯转发，不做 path 解析；api-registry 通过 Admin API 下发」。
- `docs/03-services.md §3.1/§3.2`：注明 publish→APISIX 下发、dispatcher 依赖 header 的真实数据面路径（修正「手动静态 route」的过时描述）。

## 验证（禁 smoke 脚本绕生产者；走真实入口）

- **单元**：
  - `apisix_client`：stub httpx，断言 `publish_route` 发出 `PUT /apisix/admin/routes/{id}` + 正确 body（uri/method/upstream/proxy-rewrite header + regex_uri）+ `X-API-KEY`；非 2xx → ApiError 502。path 归一化 `{x}`→`:x`。
  - api-registry publish：stub apisix_client，断言 publish 先调 `publish_route` 再 UPDATE；APISIX 失败则不置 published。
  - dispatcher `resolve_by_header`：published/deprecated 可路由；retired → 410；`/dispatch` 无 header → 400；`resolve_by_path` 已移除（import 失败即证）。
- **kind e2e（APISIX 已在 kind-apihub Running）**：
  1. 取 APISIX admin key（kind 部署的实际值）。
  2. publish 一个测试 api_version → `curl APISIX admin /routes/{id}` 确认路由存在且含 proxy-rewrite。
  3. 经 APISIX gateway（带有效 API Key）调该路径 → 200（APISIX→dispatcher→后端）。
  4. deprecate → 仍 200；retire → 410。
- **回归**：api-registry / dispatcher 既有测试全绿；`make lint` 0 新增。确认 `scripts/smoke/` 里直连 dispatcher 的脚本改为经 APISIX 或带 `X-API-Version-Id`（否则会 400）。

## 不做（R1c 边界）
- 不启用 APISIX `serverless-pre-function` / 不做 helm upgrade（retire 410 由 dispatcher 兜底）。
- 不做 retire 删 APISIX 路由的 stale 清理（`retire_route` 留 no-op 占位，follow-up）。
- 不补 §3.2 缺失的 `PUT/versions/rollback` 端点（单独项）。
- 不动 APISIX consumer/key-auth（已工作）。

## 风险
- **删 resolve_by_path 破 dev 直连**：`/dispatch` 强制 header 后，绕过 APISIX 直打 dispatcher 的脚本/测试会 400。缓解：e2e + smoke 脚本走 APISIX 或带 header；dispatcher 单测改用 header。
- **APISIX uri 段匹配语法**：`{var}`→`:var` 转换需对齐 kind 实际 APISIX radixtree 行为，e2e 验证。
- **APISIX admin key 在 kind 的获取**：configmap/Secret 是占位，e2e 需从 kind 实际部署取真值（`kubectl -n apihub-ingress get secret` 或 helm values）。
- **base_path 获取**：publish handler 现仅 `SELECT * FROM api_version`，需补 api.base_path（join api 表）。
- **proxy-rewrite regex_uri 与 key-auth 顺序**：APISIX 插件执行序，key-auth 在 access 早于 proxy-rewrite rewrite，应无冲突；e2e 验证带 key 的请求被正确改写 + 注入 header。

## 步骤（粗，细化交 writing-plans）
1. api-registry：`apisix_client.py` + settings（APISIX_ADMIN_KEY/DISPATCHER_UPSTREAM）+ 单测（stub Admin）。
2. api-registry：publish 接线 publish_route（含 base_path 查询）+ retire 注释 + 单测。
3. dispatcher：resolve_by_header 放开 published+deprecated / retired→410 + 删 resolve_by_path + /dispatch 强制 header + ErrorCode.API_RETIRED + 单测。
4. k8s config（configmap DISPATCHER_UPSTREAM + Secret 占位 APISIX_ADMIN_KEY）+ .env.dev + 文档（aggregate-ownership / 03-services）。
5. kind e2e（publish→200 / deprecate→200 / retire→410）+ 回归 + lint。
6. commit → 一个 squash-PR（push/merge 仅在用户要求时）。
