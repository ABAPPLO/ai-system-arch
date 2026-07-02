"""dispatcher —— 统一调用入口。

职责：
1. 接收 APISIX 转发的 API 调用
2. 解析路由（按 X-API-Version-Id header 或 path）
3. 转发到后端（HTTP / 异步任务 / AI 流式）
4. 按接口元数据脱敏响应
5. 投递调用事件到 Kafka（api-call-events）

详见 docs/03-services.md §3.4 + docs/05-core-flows.md §2
"""
