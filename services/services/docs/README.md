# docs-svc

> 文档生成服务 —— 把 api-registry 的接口元数据 + JSON Schema 转成 OpenAPI 3.0 spec + 多语言调用示例。
> 详见 [docs/03-services.md §3.9](../../../docs/03-services.md)。

## 架构

```
api-registry (写) → api / api_version 表 (PG)
                              ↓
                        docs-svc (读)
                              ↓
       ┌──────────────────────┼───────────────────────┐
       ↓                      ↓                       ↓
   OpenAPI 3.0 spec      多语言示例              版本列表
   (JSON / YAML)         (curl/Python/JS)        （切版本）
       ↓
   portal/admin UI / 第三方工具（Swagger UI / Postman / openapi-generator）
```

## Phase 1 范围（基础）

| 功能 | 状态 |
|------|------|
| OpenAPI 3.0 spec（JSON / YAML） | ✅ |
| 多语言调用示例（curl / Python / JavaScript） | ✅ |
| AI 流式（SSE）示例 | ✅ |
| 版本列表（前端切版本用） | ✅ |
| 在线调试（POST /try） | ⏳ Phase 3 |
| 国际化 | ⏳ Phase 3 |
| HTML 渲染（前端做） | ⏳ portal-bff |

## 关键设计

### 1. 读走 db_session（RLS 自动过滤）

docs-svc 只读 api / api_version 表，不写。所有查询通过 `db_session()`，自动按当前租户上下文过滤 —— 调用方只能看到自己租户的接口文档。

### 2. OpenAPI spec 结构

```yaml
openapi: 3.0.3
info:
  title: <api.name>
  version: <api_version.version>
  description: <api.description>
servers:
  - url: https://api.apihub.example/v1
paths:
  <api.base_path>:
    <get|post>:
      summary: ...
      security: [{ApiKeyAuth: []}]
      requestBody: ...      # 仅 POST / async / ai_model
      responses:
        '200': ...
        '400' / '401' / '403' / '404' / '429' / '500': 标准错误
components:
  securitySchemes:
    ApiKeyAuth: { type: apiKey, in: header, name: X-API-Key }
x-apihub:                  # 平台扩展字段
  api_id: ...
  version_id: ...
  backend_type: ...
  ai_model: ...
```

### 3. method 推断

Phase 1 简化策略：
- `http` backend → `GET`
- `async_task` / `workflow` / `ai_model` → `POST`

Phase 2 起让 api-registry 在 `api_version` 表存 method，docs 直接读。

### 4. 多语言示例

| backend_type | Python | JavaScript |
|--------------|--------|------------|
| http | `httpx.request(...)` | `fetch(...)` |
| async_task / workflow | 同上 + body | 同上 + body |
| ai_model + streaming | `httpx.stream(...)` 按 `data: ` 解析 | `getReader()` + 行解析 |

`notes` 数组告诉调用方注意事项（脱敏 / 流式 / 废弃版本等）。

## 接口

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| GET  | `/v1/docs/apis/{api_id}/openapi.json` | 同租户 | OpenAPI spec JSON |
| GET  | `/v1/docs/apis/{api_id}/openapi.yaml` | 同租户 | OpenAPI spec YAML |
| GET  | `/v1/docs/apis/{api_id}/examples` | 同租户 | curl/Python/JS 示例 |
| GET  | `/v1/docs/apis/{api_id}/versions` | 同租户 | 该 API 全部版本 |
| GET  | `/v1/docs/health` | 无 | k8s probe |

`?version=v2` 查询参数可指定版本（默认取最新 published）。

## 本地开发

```bash
make install
make dev-up              # 起 PG/Redis/Kafka
make run-docs            # uvicorn docs.main:app --port 8007
make run-registry        # 顺便起 api-registry 让接口能注册（或者 mock 一条 api/api_version 行）
```

手动测一下：
```bash
# OpenAPI spec（JSON）
curl -s localhost:8007/v1/docs/apis/api_xxx/openapi.json -H 'X-API-Key: ak_test' | jq

# YAML 形式（喂给 Swagger UI / openapi-generator）
curl -s localhost:8007/v1/docs/apis/api_xxx/openapi.yaml -H 'X-API-Key: ak_test'

# 多语言示例
curl -s localhost:8007/v1/docs/apis/api_xxx/examples -H 'X-API-Key: ak_test' | jq
```

## 测试

```bash
cd services/services/docs
pytest tests/ -v
# 33 tests, all pass
```

覆盖：
- `test_openapi_gen.py`（17）—— spec 结构 / x-apihub 扩展 / 标准错误响应 / requestBody 条件触发 / `_infer_method` 4 个 backend_type / `_example_from_schema` 各类型
- `test_examples.py`（10）—— curl/Python/JS 基础形态 / AI 流式变体 / notes 触发条件（masking/deprecated/draft/streaming）
- `test_routes.py`（6）—— JSON/YAML/examples/versions 端点 + 404 + health

mock 策略：
- repository 层全 monkeypatch（不连真 PG）
- spec / examples 生成器纯函数单测（输入 ApiMeta，输出 dict / str）

## 性能预算（prod）

- 3 副本（无状态、只读、可水平扩展）
- 单副本 0.5 CPU / 512Mi（OpenAPI 生成是纯计算，CPU 偶尔飙）
- HPA 基于 CPU 70%

## 关联

- 上游：portal-bff（接口详情页）/ admin-bff（后台接口预览）/ 第三方 OpenAPI 工具
- 数据源：api + api_version 表（PG，docs 只读）
- 下游：无
