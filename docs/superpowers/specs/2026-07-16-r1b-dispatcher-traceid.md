# R1b dispatcher trace_id → OTel（spec + plan 合一）

日期：2026-07-16 · 分支 `fix/r1b-dispatcher-traceid` · 依据：审计 `phase4-audit-findings.md` §3.9。

## 问题
`dispatcher/forwarder.py` 三个 `_emit_*`（L284/309/347）传 `trace_id=request.headers.get("X-Trace-Id","")`（非标准 header，通常空）→ `event.py:55` fallback 到随机 `trc_xxx` → ClickHouse 调用日志的 trace_id 与 Jaeger 对不上，trace-svc"单次调用详情含 span"（设计 §3.8）失效。

## 修法（~5 行）
1. **`dispatcher/event.py`**：`trace_id` 默认源从随机 `_gen_trace_id` 改成**OTel 当前 span 的 trace_id**（`f"{ctx.trace_id:032x}"`，无有效 span 才回落 `_gen_trace_id`）。逻辑复用 `routes.py:38-44` 现成的 `_trace_id()`。
   - 加 `from opentelemetry import trace`。
   - 加 `_otel_trace_id()` helper。
   - L55 `"trace_id": trace_id or _gen_trace_id()` → `"trace_id": trace_id or _otel_trace_id()`。
2. **`dispatcher/forwarder.py`**：删掉三处 `trace_id=request.headers.get("X-Trace-Id",""),`（让 `build_call_event` 走 OTel 默认）。

## 效果
调用事件 trace_id = 真实 OTel trace_id（= Jaeger 同一条 trace）→ CH↔Jaeger join 通。

## 测试（TDD，inline）
- 新增：`build_call_event` 在有活跃 OTel span 时，返回的 trace_id == 该 span 的 `trace_id:032x`；无 span 时回落 `trc_` 前缀。
- 回归：dispatcher `test_event.py` / `test_jobs.py` 现有断言不破（trace_id 仍非空；若有断言 `trc_` 前缀的，无 span 路径仍满足）。

## 不做
- 不改 `request_id`（仍由 forwarder 传，逻辑独立）。
- 不删 `_gen_trace_id`（OTel 无 span 时的回落仍用它）。
- 不动 Kafka header 里的 traceparent（emit 已注入，与本改动无关）。

## 步骤
1. 写失败测试（build_call_event 用 OTel span 的 trace_id）。
2. 改 event.py（_otel_trace_id + L55）+ forwarder.py（删 3 处 trace_id=）。
3. 跑 dispatcher 测试绿 + ruff。
4. commit → PR。
