# 声明式接口定义

> 通过 YAML 文件 + GitOps 方式接入接口（[ADR-003 双轨接入](../docs/00-decisions.md#adr-003-接入方式)）。

## 流程

1. 业务方在本目录下按 `<service>/<api>.yaml` 命名添加接口定义
2. 提 PR，CI 自动用 schema 校验
3. 评审通过 → 合并 main
4. api-registry 监听 Git 仓库变更，自动同步到平台
5. 按分级审批流（[ADR-005](../docs/00-decisions.md#adr-005-审批流)）路由：dev 自服务 / staging 简单审 / prod 强审批

## 现有样例

| 文件 | backend_type | 演示什么 |
|------|--------------|---------|
| [user-service/user-query.yaml](user-service/user-query.yaml) | http | 同步接口 + 缓存 + 脱敏 |
| [user-service/user-create.yaml](user-service/user-create.yaml) | async_task | 异步任务 + Webhook 回调 |
| [ai-service/llm-chat.yaml](ai-service/llm-chat.yaml) | ai_model | AI 流式 + Token 计费 |

## 字段说明

详见 [04-data-model.md §1 api_version](../docs/04-data-model.md#1-api-元数据)。

### backend_type

| 值 | 用途 |
|----|------|
| `http` | 同步 HTTP 接口 |
| `async_task` | 异步任务（task_id + Webhook） |
| `workflow` | 长时工作流（Argo Workflow DAG） |
| `ai_model` | LLM/AI 模型（支持流式 + Token 计量） |

### masking

| action | 效果 |
|--------|------|
| `remove` | 完全不进日志 |
| `mask` | 脱敏（`138****1234`） |
| `hash` | SHA256 哈希 |
