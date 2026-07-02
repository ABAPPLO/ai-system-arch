# 07 · 开发者门户与文档自动化

## 1. 门户定位

两套前端：

| 前端 | 用户 | 核心 | 域名示例 |
|------|------|------|---------|
| **管理后台 Admin** | 平台运维、内部接口提供方、内部调用方 | 接口管理、应用管理、监控、失败重试、配额、**租户管理** | admin.apihub.internal |
| **开发者门户 Portal** | 外部开发者 | 自助注册、申请 Key、查文档、在线调试、看用量 | open.apihub.com |

两者底层共享核心服务（api-registry、auth、trace、docs），但 BFF 独立，安全边界清晰。

## 1.1 多租户下的 Portal 体验（[ADR-009](00-decisions.md#adr-009-多租户策略)）

- 个人开发者注册 → 自动加入 `external-public` 租户
- 企业开发者（enterprise 认证后）→ 创建 / 加入独立租户
- 一个邮箱可关联多个租户（员工跳槽 / 多公司顾问场景）
- 登录后默认进入"最近活跃"租户，顶部可切换
- 切换租户审计

**Admin 后台**：
- 内部用户加入一个或多个内部租户
- 顶部下拉切换"当前租户"
- 超管可切换任意租户（审计 + 告警）

## 2. 管理后台（Admin）

### 2.1 核心模块

```
┌──────────────────────────────────────────────────────────┐
│  管理后台                                                  │
├──────────────────────────────────────────────────────────┤
│  仪表盘         │ 平台总览、QPS、错误率、热门 API          │
│  接口管理       │ 接口列表、详情、新建、编辑、发布、下线    │
│  版本管理       │ 版本历史、diff、回滚                     │
│  评审工单       │ 变更评审、审批                            │
│  应用管理       │ 内部 / 外部应用、Key 管理                  │
│  授权管理       │ 调用方与 API 的授权关系                    │
│  调用监控       │ 实时大盘、按 API / 调用方下钻             │
│  调用日志       │ 按 trace_id 查询、详情、关联 Jaeger        │
│  失败重试       │ 失败列表、手动重试、死信队列              │
│  配额与限流     │ 配额规则、实时用量、限流配置              │
│  灰度发布       │ 灰度策略、流量比例、回滚                  │
│  审计日志       │ 所有变更操作                              │
│  系统设置       │ 团队、权限、通知、集成                    │
└──────────────────────────────────────────────────────────┘
```

### 2.2 接口列表页

```
┌────────────────────────────────────────────────────────────────────┐
│  接口管理                                                            │
│                                                                     │
│  [搜索: 名字/路径]  [业务线 ▾]  [状态 ▾]  [我负责的 ☑]   [新建接口]   │
├────────────────────────────────────────────────────────────────────┤
│  名称          路径           方法  版本  状态       调用量(今日)     │
│  user.create  /v1/users      POST  v2   published  125,432          │
│  user.query   /v1/users/:id  GET   v1   published  332,108          │
│  order.refund /v1/orders/refund POST v1 deprecated 8,234            │
│  job.export   /v1/exports    POST  v1   published  12              │
└────────────────────────────────────────────────────────────────────┘
```

### 2.3 接口详情页

左侧目录：

```
概述
├─ 基本信息
├─ 接入说明
├─ 参数说明
├─ 响应说明
├─ 错误码
├─ 调用示例 (curl/Python/JS/Java)
├─ 在线调试
├─ 版本历史
├─ 调用方列表
├─ 监控
├─ 失败重试
└─ 配置 (鉴权/限流/超时)
```

## 3. 开发者门户（Portal）

### 3.1 核心模块

```
┌──────────────────────────────────────────────────────────┐
│  开发者门户                                                │
├──────────────────────────────────────────────────────────┤
│  首页           │ 产品介绍、快速开始                       │
│  API 目录       │ 浏览所有公开 API，按业务/标签筛选         │
│  API 详情       │ 文档、参数、示例、在线调试                │
│  我的应用       │ 创建应用、管理 API Key                    │
│  我的授权       │ 已授权 API 列表、申请新授权               │
│  用量统计       │ 调用次数、错误率、延迟（按应用 / API）    │
│  账单           │ 计费明细（如开通计费）                    │
│  沙箱           │ 在线测试代码（无需本地环境）              │
│  SDK 下载       │ 多语言 SDK 包                            │
│  帮助           │ 常见问题、状态码、最佳实践                │
│  通知中心       │ 接口变更、配额告警                        │
└──────────────────────────────────────────────────────────┘
```

### 3.2 注册与实名（[ADR-011 邮箱 + 手机号](00-decisions.md#adr-011-实名认证)）

```
注册邮箱 → 邮箱验证 → 手机号 → 短信验证 → 创建用户账号
                                              ↓
                                        自动加入 external-public 租户
                                              ↓
                                          创建首个应用
```

**默认认证级别**：`basic`（邮箱 + 手机号验证即可）

**升级到 `enterprise`**（按需，调用企业 API 时要求）：
- 上传营业执照
- OCR + 人工审核（24h 内）
- 审核通过 → 用户 `verification_level = enterprise`
- 创建 / 加入企业租户（独立 tenant_id）

**API 敏感度分级**：
- API 元数据可标 `min_verification_level`
- 调用 API 时平台检查 `app.owner.verification_level >= api.min_verification_level`
- 不满足 → 返回 `403 + 提示升级认证`

### 3.3 API 目录

```
┌────────────────────────────────────────────────────────────────────┐
│  API 目录                                                            │
│                                                                     │
│  [分类 ▾]  [标签 ▾]  [搜索]                                          │
├────────────────────────────────────────────────────────────────────┤
│  📦 用户服务                                                          │
│    user.create    创建用户    ★★★★☆  1280 调用方                     │
│    user.query     查询用户    ★★★★★  2104 调用方                     │
│                                                                     │
│  💳 支付服务                                                          │
│    pay.create     发起支付    ★★★★☆  856 调用方                      │
│    pay.refund     退款       ★★★☆☆  420 调用方                       │
└────────────────────────────────────────────────────────────────────┘
```

每个 API 卡片：
- 名称 / 简介
- 评分（调用方反馈）
- 调用方数量
- 文档直达
- 申请授权入口

### 3.4 应用与 API Key 管理

```
我的应用 → 创建应用 → 填写名称、用途、回调 URL → 生成 app_id + app_secret
                                                            ↓
                                                  生成 API Key（可选多个）
                                                  [ak_xxxxx] [权限范围]
                                                  [下载 SDK]
```

**Key 安全**：
- 创建时显示明文一次，之后只显示前 8 位
- 支持吊销、轮换
- 多 Key 用于不同环境

## 4. 文档自动化（核心）

### 4.1 文档生成的输入

- 接口元数据（路径、方法、鉴权、限流）
- JSON Schema（请求 + 响应 + 各错误码）
- 示例（请求 + 响应对）
- 业务描述（负责人手填一段文字）

### 4.2 文档结构

每个 API 详情页结构：

```
1. 概述
   - 接口用途
   - 业务场景
   - 调用频率建议

2. 接入信息
   - 请求路径（含 dev / staging / prod 域名）
   - 请求方法
   - 鉴权方式（详细说明）
   - Content-Type

3. 参数说明
   - 请求头
   - Query 参数
   - Body 参数（基于 JSON Schema 渲染表格）
     | 字段名 | 类型 | 必填 | 默认值 | 说明 | 枚举 |
     | name  | str | ✓  | -     | 用户名 | -   |
     | age   | int | ✗  | -     | 年龄  | 0-150|

4. 响应说明
   - 成功响应（200）
     - Body 结构（同上表格）
     - 示例 JSON
   - 错误响应
     - 400 参数错误
     - 401 鉴权失败
     - 403 未授权
     - 429 限流
     - 500 服务异常

5. 调用示例
   - curl
   - Python (requests)
   - JavaScript (fetch)
   - Java (OkHttp)
   - Go (net/http)
   - PHP (curl)
   - C# (HttpClient)

6. 在线调试
   - 表单填参数
   - 一键发起调用（dev 环境）
   - 查看响应

7. 错误码对照表
   | code | message | 说明 |

8. 变更历史
   - 版本 diff
   - 不兼容变更高亮

9. 监控与统计
   - 我的调用统计（如已授权）
```

### 4.3 curl 示例自动生成

```bash
curl -X POST 'https://api.apihub.com/v1/users' \
  -H 'Authorization: Bearer <your_api_key>' \
  -H 'Content-Type: application/json' \
  -H 'X-Trace-Id: <optional>' \
  -d '{
    "name": "alice",
    "age": 18
  }'
```

**生成规则**：
- 路径用生产域名（可切换 dev/staging）
- `<your_api_key>` 占位符，登录后自动替换为用户实际 Key（点击"填充我的 Key"）
- Header 按鉴权方式动态生成
- Body 从示例取

### 4.4 Python 示例自动生成

```python
# 方式 1：使用平台官方 SDK（推荐）
from apihub import ApiHubClient

client = ApiHubClient(api_key="<your_api_key>")
resp = client.user.create(name="alice", age=18)
print(resp.data.id)

# 方式 2：使用 requests
import requests

resp = requests.post(
    "https://api.apihub.com/v1/users",
    headers={
        "Authorization": "Bearer <your_api_key>",
        "Content-Type": "application/json",
    },
    json={"name": "alice", "age": 18},
    timeout=10,
)
print(resp.json())
```

### 4.5 JS / Java 等其他语言

类似规则，基于模板 + JSON Schema 自动填充。

## 5. 在线调试（Try it out）

### 5.1 体验目标

- 类似 Swagger UI / Postman，但更友好
- 调用走 dev 环境（不影响生产）
- 自动带上用户 Key
- 完整响应展示（含 header、body、耗时、trace_id）
- 一键复制为代码

### 5.2 实现

```
用户填表单 → 点击"发送" → Portal BFF → docs-svc /try
                                              ↓
                                       dev 环境调用接口
                                              ↓
                                       返回完整响应
                                              ↓
                                       记录 trace_id
```

### 5.3 限制

- 每个 Key 在线调试每天限 100 次（防滥用）
- 仅 dev 环境
- 请求体大小限制 1MB
- 不支持长时任务（用沙箱替代）

## 6. SDK 自动生成

### 6.1 工具

基于 [openapi-generator](https://openapi-generator.tech/)，加上平台自研 wrapper：

- 自动加上鉴权逻辑（API Key / 签名）
- 自动重试（幂等接口）
- 自动日志（trace_id 注入）
- 自动错误处理（统一响应格式解析）
- 类型定义（强类型语言）

### 6.2 支持语言

| 语言 | 优先级 | 包管理 |
|------|--------|--------|
| Python | P0 | PyPI（内部） |
| Java | P0 | Maven（内部） |
| Go | P1 | Go module proxy（内部） |
| JavaScript | P1 | npm（内部） |
| TypeScript | P1 | npm（内部） |
| PHP | P2 | Composer |
| C# | P2 | NuGet |
| Ruby | P3 | gem |

### 6.3 发布流程

```
接口发布 → api-registry 触发 → sdk-gen 服务
                                      ↓
                              生成 OpenAPI spec
                                      ↓
                              openapi-generator 多语言
                                      ↓
                              加上 platform wrapper
                                      ↓
                              打包上传到内部 Nexus
                                      ↓
                              通知调用方（Webhook）
```

### 6.4 SDK 使用体验（Python 例子）

```python
from apihub import ApiHubClient

# 初始化
client = ApiHubClient(
    api_key="ak_xxxxx",
    env="prod",                  # dev / staging / prod
    timeout=10,
    max_retries=3,
)

# 同步调用
resp = client.user.create(name="alice", age=18)
print(resp.trace_id)
print(resp.data.id)

# 异步任务
task = client.report.generate(start="2026-06-01", end="2026-06-30")
result = task.wait(timeout=300)         # 阻塞等待
# 或 task.wait_async(callback)          # 异步回调

# 错误处理
from apihub import ApiHubError, RateLimitError
try:
    client.user.create(name="")
except RateLimitError as e:
    print(f"限流，建议 {e.retry_after}s 后重试")
except ApiHubError as e:
    print(f"业务错误 {e.code}: {e.message}")
```

### 6.5 SDK 版本管理

- 接口版本 → SDK 主版本（v1 → 1.x.x）
- 不兼容变更 → 升主版本（v1 → v2，SDK 1.x → 2.x.x）
- 兼容变更 → 升 minor / patch
- 老版本 SDK 维护期：12 个月

## 7. 沙箱环境

### 7.1 目的

外部开发者无需本地环境即可测试。

### 7.2 形式

- **Web IDE**（基于 Monaco Editor / VSCode Server）
- 预装各语言 SDK
- 直接调用 dev 环境
- 支持 Jupyter Notebook（数据分析场景）

### 7.3 实现

- K8s 起独立 Pod（按用户隔离）
- 资源限额 1c2g
- 空闲 30 分钟回收

## 8. 通知与变更订阅

### 8.1 通知场景

| 事件 | 接收方 | 渠道 |
|------|--------|------|
| 接口发布 | 已授权调用方 | 邮件 + 站内信 |
| 接口废弃 | 仍在调用的调用方 | 邮件 + 短信 + 站内信 |
| 接口下线 | 仍在调用的调用方 | 多次提醒（30/7/1 天） |
| 配额预警 | 调用方 | 邮件 + 短信 |
| 错误率突增 | 接口负责人 + 调用方 | 钉钉 / 飞书 |
| 失败重试耗尽 | 调用方 | 站内信 |

### 8.2 订阅管理

调用方可在门户配置订阅：
- 哪些 API 的变更通知
- 哪些渠道（邮件 / 短信 / Webhook）
- 哪些事件类型

## 9. 用量统计与计费

### 9.1 用量展示

门户内每个应用可看：
- 调用次数（按天 / 周 / 月）
- 成功率
- 平均延迟
- 按 API 维度的分布
- 实时调用流（最近 100 条）

### 9.2 计费（可选，未来）

支持：
- 免费配额（每月 1w 次）
- 计次付费
- 包月套餐
- 阶梯定价

数据来源：ClickHouse `api_call_log`，按月聚合。

## 10. 安全考虑

| 风险 | 对策 |
|------|------|
| 暴力枚举 API Key | 频率限制 + IP 黑名单 |
| 文档泄露敏感信息 | 公开 API 才显示在门户；内部 API 仅内部用户可见 |
| 沙箱恶意调用 | 资源限额 + 调用频率限制 |
| 在线调试被滥用 | 每日次数限制 + 仅 dev 环境 |
| Webhook URL 被滥用 | 验证回调签名 |
| 抓包 Key | 强制 HTTPS + Key 前缀提示定期轮换 |

## 11. 移动端适配

Portal 关键页面（API 文档、用量、错误码）需移动端友好，方便开发者随时查阅。Admin 不做移动端。

## 12. 国际化

Portal 支持：
- 中文（默认）
- 英文（如有海外调用方）

接口文档支持多语言字段（接口提供方填写中英双语）。
