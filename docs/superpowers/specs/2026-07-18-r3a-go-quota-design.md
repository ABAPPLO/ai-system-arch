# R3a — Go quota 补齐替代 Python（design）

**日期**：2026-07-18
**分支**：`fix/r3a-go-quota`（base main = `7613f3d`，dispatcher-sse 合后）
**audit**：§3.8 R3a —— Go quota 补齐替代 Python（对齐响应字段/Usage 形状/Redis key/改回 Lua 原子/接鉴权+RLS/补 health-ready/Makefile+k8s 切镜像）。量=大，用户选一轮 7 点 + 鉴权信任入口（复用 R1d）。

## 背景

Go quota 代码已存（main `services/go/quota/`，重写 WIP plan Task 1-9 已落地），但**未对齐 Python 契约 + 非原子 Lua + 无鉴权 + Makefile/k8s 仍跑 Python**。R3a 补齐 7 点使 Go quota 真替代 Python（**dispatcher 零改动**——Go 响应必须与 Python 逐字段一致）。

WIP `stash@{0}`（feat/go-quota 分支）的 `limiter/redis.go` 145 行是「Lua 原子」一点的进行中实现，作 point 4 输入参考。

## 现状差距（探索确认）

| 点 | Python（基准） | Go（现状） | 差距 |
|---|---|---|---|
| 1 响应字段 | `QuotaCheckResponse{allowed,tier_blocked,limit,remaining,retry_after_seconds,rule_source}` | `{allowed,tier_blocked,current,limit,remaining,reset_ms,rule_source}` | Go 多 `current/reset_ms`，缺 `retry_after_seconds` |
| 2 Usage 形状 | `{tenant_id,app_id,api_id,second,minute,day}`（扁平，`UsagePoint{window_seconds,used,limit}`） | `{points:[]{tier,used,limit,remaining,reset_ms}}` | 形状完全不同 |
| 3 Redis key | `t:{tenant}:rate:{api}:{app}:{s\|m\|d}:{slot}` | `t:{region}:rate:{tenant}:{api}:{app}:{slot}` | Go 有 region 前缀、无 tier `s/m/d` |
| 4 Lua 原子 | `lua_scripts.CHECK_AND_INCR`（多 tier 原子，1 RTT） | `INCR`+`Expire` 逐 tier（非原子，跨 tier race + 2 RTT） | Go 非原子 |
| 5 鉴权/RLS | apihub_core 中间件 key-auth + RLS | 裸 HTTP；LoadRules 应用层 `WHERE tenant_id=$1` | Go 无鉴权中间件 |
| 6 health | `/health/live` + `/health/ready` | 仅 `/v1/quota/health` | 缺 `/health/live`+`/health/ready` |
| 7 部署 | Makefile `run-quota=uvicorn`（Python）；k8s `image=quota:0.1.0-dev`（Python） | Go `Dockerfile` 存在但 Makefile/k8s 未切 | 未切 Go |

## 设计（7 点）

### 1-2. 响应/Usage 对齐 Python（Go models + handler 改）
- `QuotaCheckResponse`：去 `current`/`reset_ms`，加 `retry_after_seconds`（被挡 tier 窗口剩余秒；allowed=True 时 0）。
- `UsageResponse`：改扁平 `{tenant_id,app_id,api_id,second,minute,day}`；`UsagePoint{window_seconds,used,limit}`（limit 可 None=不限流）。
- `handler.usage` 返回扁平结构；`check`/`checkStrict` 响应字段对齐。

### 3. Redis key 对齐 Python
- `rateKey`：`t:{tenant}:rate:{api}:{app}:{s|m|d}:{slot}`（tier 用 `s`/`m`/`d`，无 region 前缀）。
- `Limiter` 保留 `region`/`splitRatio` 字段（**R3b 多 Region 前置**，R3a 阶段 `splitRatio=1` 不启用分区，key 不带 region）。

### 4. Lua 原子（WIP redis.go 输入）
- `limiter` 改内嵌 Lua 脚本：把 Python `CHECK_AND_INCR`（多 tier 原子 check+INCR+EXPIRE，1 RTT）移植为 Go 常量，`EvalSha`/`Eval` 原子执行。refund 同理（Python `REFUND` 脚本）。
- 参照 `stash@{0}:services/go/quota/internal/limiter/redis.go`（145 行 WIP）。

### 5. 鉴权信任入口 + RLS（复用 R1d）
- Go 加中间件：验 `X-Ingress-Auth`（`INGRESS_SHARED_SECRET`，同 dispatcher R1d）；不匹配 → 401。
- APISIX key-auth 验调用方 key + 注入 `X-Ingress-Auth`；Go quota ClusterIP 内网（不对外）。
- **RLS**：LoadRules 应用层 `WHERE tenant_id=$1 AND app_id=$2 AND api_id=$3`（现状已等价 RLS；quota_rule 平台级规则按 tenant 查）。Go 不共享 Python RLS 中间件，应用层 WHERE 达等价租户隔离。

### 6. /health/live + /health/ready
- `/health/live`：always 200（liveness）。
- `/health/ready`：PG ping + Redis ping，皆 ok 才 200（readiness）。
- 保留 `/v1/quota/health`（兼容，= ready 别名或保留）。

### 7. Makefile + k8s 切 Go
- Makefile `run-quota`：`go run ./services/go/quota/cmd`（或 build binary + run），**去 uvicorn**。加 `build-quota`（Go build）。
- k8s `deploy/k8s/services/quota/deployment.yaml`：`image` → Go quota 镜像；`command` → Go binary（去 uvicorn）；containerPort 8004 保留。Python quota 部署由此切到 Go（镜像名可保留 `quota:0.1.0-dev` 但 Dockerfile 构建 Go）。
- 各 overlay `quota-envfrom` 补 Go 需 env（`INGRESS_SHARED_SECRET` 等）。

## 范围 / 非范围

**范围**：Go quota `models`/`limiter`/`handler`/`cmd/main`（中间件）+ Makefile + k8s deployment + Go 测试。
**非范围**：
- 多 Region splitRatio 真启用（R3b；R3a 仅保留字段）。
- Python quota 代码删除（保留作参考，仅部署切 Go）。
- dispatcher 改动（Go 逐字段对齐 Python，dispatcher 零改动）。

## 测试

- **Go 单测**：limiter Lua 原子（多 tier check+incr 一次 Eval）+ key 格式断言（`= t:tenant_a:rate:api_x:app_y:s:<slot>`）+ 响应字段对齐 Python models（逐字段）。
- **集成测试**：`tests/quota_test.go`（真 Redis + PG）。
- **kind e2e**：APISIX key-auth → Go quota，对照 Python 契约（响应/Usage/key/Lua 原子）；Go 镜像部署 + /health/ready 200。

## 风险

- `UsageResponse` 形状 breaking（points→扁平）——但 Go 现状未上线（Python 在跑），改 Go 无生产影响。
- Lua 移植：Python `CHECK_AND_INCR`/`REFUND` 逻辑要忠实移植 Go（`Eval`，KEYS/ARGV 顺序一致）。
- 鉴权信任入口：`INGRESS_SHARED_SECRET` 须与 dispatcher R1d 一致（同 env）。
- `region` 字段保留但 R3a 不用（splitRatio=1）——R3b 启用前置。
- Go 1.25.0（go.mod）——构建环境兼容。
