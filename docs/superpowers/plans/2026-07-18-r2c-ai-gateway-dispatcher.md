# R2c — ai-gateway 接入 dispatcher 成唯一 AI 流式入口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 ai-gateway 接入 dispatcher，使 dispatcher 成唯一 AI 流式入口（统一 token 计费/限流/脱敏/审计），ai-gateway 聚焦多 Provider 路由。

**Architecture:** 拓扑 A：`client → APISIX(key-auth+limit-count) → dispatcher(ai_model SSE 转发+token 提取+Kafka 计费+脱敏) → ai-gateway(ClusterIP, /v1/chat/completions skip_auth, 多 Provider 路由+key 解密) → provider`。R2c 核心 = 把 AI api `backend_url` 从占位 `llm-gateway.internal` 改指向 ai-gateway 服务（打通拓扑），其余计费/限流/脱敏 dispatcher 已有能力，走通即生效。

**Tech Stack:** Python 3.11, FastAPI, asyncpg, APISIX, kind k8s e2e, mock-backend（Python http.server）。

**Spec:** `docs/superpowers/specs/2026-07-18-r2c-ai-gateway-dispatcher-design.md`

## Global Constraints

- **拓扑**：dispatcher 唯一 AI 入口；ai-gateway ClusterIP 内网（已 ClusterIP + `/v1/chat/completions` skip_auth，不改 Service/鉴权）。
- **不改**：dispatcher forwarder AI 主干（已具备 SSE+token 提取+Kafka 计费+脱敏）、ai-gateway 路由/解密逻辑（Phase 4 已做）、ai-gateway Service/鉴权。
- **e2e mock provider**：`ai_provider.base_url` 指向 mock-backend 的 `/v1/chat/completions`（OpenAI 风格 SSE）；不用 real key/外网。
- **e2e 走 SQL seed**（同 `scripts/smoke/k8s-workflow-argo.py` 模式，不经 apihub-apply CLI）。
- **python/pytest 走 repo-root `.venv/bin/python`**（NOT services/.venv）。kind e2e 用 `kubectl --context kind-apihub`。
- **GateGuard**：每文件首次 bash/edit 拦，报 facts retry。
- **每 task 一个 commit**；TDD where applicable（mock-backend/e2e 以 e2e 验证为主）。
- cluster: `kind-apihub`；namespace `apihub-system`；PG pod `apihub-pg-0`（按实际 `kubectl get pod | grep pg`）；APISIX NodePort `30080`；mock-backend Service `mock-backend.apihub-system:80`。

---

### Task 1: mock-backend 加 OpenAI SSE 端点 + schema backend_url 改指 ai-gateway

**Files:**
- Modify: `deploy/k8s/overlays/kind/mock-backend.yaml`（内联 Python 脚本加 `/v1/chat/completions` SSE）
- Modify: `schema/ai-service/llm-chat.yaml`（`backend_url` 占位 → ai-gateway）

**Interfaces:**
- Produces: mock-backend `/v1/chat/completions` 返 OpenAI 风格 SSE（chunks + 末尾 `usage` + `[DONE]`），供 ai-gateway `openai_compat` provider 消费（dispatcher 提取 `usage` 计费）。

- [ ] **Step 1: mock-backend.yaml 加 SSE 端点**

`do_POST` 开头加 path 分流（`/v1/chat/completions` → SSE；其余 → 现有 echo）。完整替换 `args` 内联脚本为：
```python
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n=int(self.headers.get('Content-Length',0)); b=self.rfile.read(n)
        if self.path == '/v1/chat/completions':
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream')
            self.send_header('X-Accel-Buffering','no')
            self.end_headers()
            cid='chatcmpl-mock'
            for content in ['Hello',' from',' mock']:
                chunk={'id':cid,'object':'chat.completion.chunk','choices':[{'index':0,'delta':{'content':content},'finish_reason':None}]}
                self.wfile.write(f'data: {json.dumps(chunk)}\n\n'.encode()); self.wfile.flush()
            final={'id':cid,'object':'chat.completion.chunk','choices':[{'index':0,'delta':{},'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':3,'total_tokens':13}}
            self.wfile.write(f'data: {json.dumps(final)}\n\n'.encode()); self.wfile.flush()
            self.wfile.write(b'data: [DONE]\n\n'); self.wfile.flush()
            return
        self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
        self.wfile.write(json.dumps({"ok":True,"echo":b.decode()[:200]}).encode())
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')
    def log_message(self,*a): pass
HTTPServer(('0.0.0.0',8080),H).serve_forever()
```
（保留现有 Deployment/Service 结构，只换 `args` 内联脚本。）

- [ ] **Step 2: schema/ai-service/llm-chat.yaml backend_url 改**

`backend_url: http://llm-gateway.internal/v1/chat/completions` → `backend_url: http://ai-gateway.apihub-system/v1/chat/completions`。

- [ ] **Step 3: commit**

```bash
git add deploy/k8s/overlays/kind/mock-backend.yaml schema/ai-service/llm-chat.yaml
git commit -m "feat(r2c): mock-backend 加 OpenAI SSE 端点 + llm-chat backend_url 改指 ai-gateway"
```

---

### Task 2: kind e2e 全链（seed → APISIX → dispatcher → ai-gateway → mock，断言 SSE/计费/限流/路由）

**Files:**
- Create: `scripts/smoke/k8s-ai-gateway-dispatcher.py`（e2e 脚本，参照 `k8s-workflow-argo.py` / `k8s-links.py` 模式）

**Interfaces:**
- Consumes: Task 1 的 mock-backend SSE + ai-gateway `/v1/chat/completions`（skip_auth）+ dispatcher ai_model 转发 + APISIX key-auth/limit-count（R1d）。
- Produces: e2e 证明 R2c 拓扑打通 + 统一计费 + 限流 + 多 Provider 路由。

- [ ] **Step 1: 写 e2e 脚本**

参照 `scripts/smoke/k8s-workflow-argo.py` 的 `http()` / `sh()` / psql-seed 模式。骨架：
```python
#!/usr/bin/env python3
"""R2c e2e: client→APISIX→dispatcher→ai-gateway→mock-backend SSE。
断言：SSE 流式透传、token 计费 emit、多 Provider 路由（model 不匹配→400）、限流（超限→429）。
退出码：0 OK / 1 assert fail / 2 env unavailable。"""
import json, sys, time, urllib.request, urllib.error
APISIX_URL="http://127.0.0.1:30080"
DEMO_KEY="ak_test_a_demo001"   # 复用 R1d seed consumer key（按实际调整）
TENANT_ID="tenant_a"
AI_API_ID="smoke-llm-chat"
MODEL="gpt-4o-mini"
MOCK_PROVIDER_ID="11111111-1111-1111-1111-111111111111"

def http(method, url, headers=None, data=None, timeout=30):
    req=urllib.request.Request(url, method=method, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r: return r.status, r.read()
    except urllib.error.HTTPError as e: return e.code, e.read()

def sh(cmd):
    import subprocess
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout

def seed():
    sql=f"""
    INSERT INTO api (id,tenant_id,name,description,category,base_path,tags,status,visibility)
    VALUES ('{AI_API_ID}','{TENANT_ID}','smoke llm chat','r2c e2e','smoke','/smoke-llm-chat',ARRAY['smoke'],'published','tenant')
    ON CONFLICT (id) DO NOTHING;
    INSERT INTO api_version (api_id,version,backend_type,backend_url,ai_model,ai_streaming,status)
    VALUES ('{AI_API_ID}','v1','ai_model','http://ai-gateway.apihub-system/v1/chat/completions','{MODEL}',true,'published')
    ON CONFLICT (api_id,version) DO UPDATE SET backend_url=EXCLUDED.backend_url, ai_streaming=true, ai_model=EXCLUDED.ai_model;
    INSERT INTO ai_provider (id,name,provider_type,base_url,default_model,status)
    VALUES ('{MOCK_PROVIDER_ID}','mock-openai','openai_compatible','http://mock-backend.apihub-system:80/v1/chat/completions','{MODEL}','active')
    ON CONFLICT (name) DO NOTHING;
    INSERT INTO ai_provider_key (provider_id,key_alias,key_encrypted,key_prefix,status)
    VALUES ('{MOCK_PROVIDER_ID}','mock','<ENCRYPTED_MOCK_KEY>','mk_','active')
    ON CONFLICT DO NOTHING;
    INSERT INTO ai_model_route (model_pattern,target_provider_id,target_model,priority,status)
    VALUES ('gpt-4o%','{MOCK_PROVIDER_ID}','{MODEL}',0,'active')
    ON CONFLICT DO NOTHING;
    """
    open('/tmp/_r2c_seed.sql','w').write(sql)
    sh("docker exec -i apihub-pg psql -U apihub -d apihub -v ON_ERROR_STOP=1 < /tmp/_r2c_seed.sql")
```
**`<ENCRYPTED_MOCK_KEY>`**：ai-gateway `decrypt()` 用 `pii_encryption_key` 解密 `ai_provider_key.key_encrypted`。执行时按 `ai_gateway/crypto.py` 契约，用 `pii_encryption_key` 加密一个 mock key（如 `"mock-key"`）后填入（参考 ai-gateway crypto 单测的加密方式）。openai_compat provider 会把解密后的 key 作 `Authorization: Bearer <key>` 发给 mock-backend（mock 不校验 key，故任意值可）。

- [ ] **Step 2: publish llm-chat 到 APISIX + 验 SSE 透传 + 计费 + 路由 + 限流**

```python
def main():
    seed()
    # 触发 api-registry publish_route（或等 R1c 自动 publish；按 R1c 机制）
    # client POST APISIX（路由形态按 R1c：base_path 或 /v1/chat/completions）
    body=json.dumps({"model":MODEL,"messages":[{"role":"user","content":"hi"}],"stream":True}).encode()
    st,raw=http("POST", f"{APISIX_URL}/v1/chat/completions",
                headers={"X-API-Key":DEMO_KEY,"Content-Type":"application/json"}, data=body, timeout=30)
    print(f"POST chat -> HTTP {st} {raw[:200]!r}")
    assert st==200, f"SSE HTTP {st}: {raw}"
    assert b'data: ' in raw and b'[DONE]' in raw, f"SSE 未透传: {raw!r}"
    print("R2C-A SSE 透传 OK")

    # 计费：dispatcher _emit_stream_complete 发 Kafka api-call-events(tokens_prompt/completion)→CH
    time.sleep(5)
    # 查 CH（trace-svc 写）最近该 api 的 call_log token 非零（按 CH 实际 schema/列名 + trace-svc 查询路径）
    # …查 CH/Kafka 断言 token_prompt>0 or token_completion>0…

    # 多 Provider 路由：未知 model → ai-gateway 400
    bad=json.dumps({"model":"unknown-model","messages":[{"role":"user","content":"x"}],"stream":True}).encode()
    stb,rawb=http("POST",APISIX_URL+"/v1/chat/completions",headers={"X-API-Key":DEMO_KEY,"Content-Type":"application/json"},data=bad)
    assert stb==400, f"未路由拒绝: {stb} {rawb}"
    print("R2C-B 多 Provider 路由 OK")

    # 限流：limit-count 超限 → 429（快速连发超 consumer 配额；阈值按 R1d，过高则标 skip）
    print("ALL OK —— R2c e2e green"); sys.exit(0)

if __name__=="__main__":
    try: main()
    except AssertionError as e: print(f"SMOKE FAIL: {e}"); sys.exit(1)
    except (OSError, RuntimeError) as e: print(f"SMOKE ENV-UNAVAILABLE: {e}"); sys.exit(2)
```

- [ ] **Step 3: 部署 mock-backend + 跑 e2e + 修暴露问题 + commit**

```bash
unset ALL_PROXY HTTPS_PROXY HTTP_PROXY; NO_PROXY=127.0.0.1
kubectl --context kind-apihub apply -f deploy/k8s/overlays/kind/mock-backend.yaml
kubectl --context kind-apihub -n apihub-system rollout restart deploy/mock-backend
kubectl --context kind-apihub -n apihub-system rollout status deploy/mock-backend
python3 scripts/smoke/k8s-ai-gateway-dispatcher.py
```
e2e 暴露的问题（ai-gateway decrypt mock key / dispatcher→ai-gateway header / CH 计费写入路径 / APISIX 路由形态）→ 修 + commit（同 R2e T4/T5 模式）。
```bash
git add scripts/smoke/k8s-ai-gateway-dispatcher.py <其他修复>
git commit -m "feat(r2c): kind e2e 全链（APISIX→dispatcher→ai-gateway→mock）+ SSE/计费/路由断言"
```

---

## 风险 / 注意（执行时）

- **ai-gateway `decrypt()` mock key**：`ai_provider_key.key_encrypted` 须用 `pii_encryption_key` 真加密一个 mock key（参考 `ai_gateway/crypto.py` + 其单测）。mock-backend 不校验 key，故解密后的值任意可用，但 decrypt 本身不能失败。
- **APISIX 路由 llm-chat**：R1c publish 形态（base_path 路由 vs `/v1/chat/completions` 直连）——e2e 请求 URL 按 R1c 实际；若需手动 publish，调 api-registry publish_route。
- **CH 计费写入**：dispatcher emit Kafka `api-call-events`（含 `tokens_prompt/completion`），trace-svc 消费写 CH。e2e 断言 token 非零要等 Kafka→CH 落库（sleep/轮询）；按 trace-svc CH schema 实际列名断言。若 CH 查询路径复杂，可改断言 Kafka topic 最近消息含 token。
- **kind 环境**：集群/mock-backend/APISIX 未起 → e2e 退出码 2（env-unavailable）→ 控制器确保 dev 栈 + kind 起。
