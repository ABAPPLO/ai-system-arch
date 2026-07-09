"""多语言调用示例生成 —— curl / Python(httpx) / JavaScript(fetch)。

复用 openapi_gen 的方法推断；基于 base_path + backend_type 给出最小可跑示例。
"""

from docs.models import ApiMeta, ExampleResponse


def build_examples(
    meta: ApiMeta, *, base_url: str = "https://api.apihub.example"
) -> ExampleResponse:
    """生成 curl / Python / JavaScript 调用示例。"""
    method = _infer_method(meta)
    full_url = f"{base_url.rstrip('/')}{meta.base_path}"
    notes: list[str] = []

    if meta.backend_type == "ai_model" and meta.ai_streaming:
        notes.append("AI 流式接口：响应是 SSE，需要按行解析 data: 块。")
    if meta.masking:
        notes.append("敏感字段已脱敏，调用方看到的响应与文档示例可能不同。")
    if meta.version_status == "deprecated":
        notes.append("此版本已废弃（deprecated），建议迁移到新版本。")
    if meta.api_status != "published":
        notes.append(f"接口当前状态为 {meta.api_status}，可能无法调用。")

    has_body = meta.request_schema is not None and method in ("post", "put", "patch")

    curl = _curl(method, full_url, has_body)
    python = _python(method, full_url, has_body, meta)
    javascript = _javascript(method, full_url, has_body, meta)

    return ExampleResponse(
        curl=curl,
        python=python,
        javascript=javascript,
        notes=notes,
    )


def _infer_method(meta: ApiMeta) -> str:
    if meta.backend_type in ("async_task", "workflow", "ai_model"):
        return "post"
    return "get"


def _curl(method: str, url: str, has_body: bool) -> str:
    parts = [
        "curl -X " + method.upper(),
        f'  "{url}"',
        '  -H "X-API-Key: $APIHUB_API_KEY"',
    ]
    if has_body:
        parts.append('  -H "Content-Type: application/json"')
        parts.append('  -d \'{"key": "value"}\'')
    return " \\\n".join(parts)


def _python(method: str, url: str, has_body: bool, meta: ApiMeta) -> str:
    if meta.backend_type == "ai_model" and meta.ai_streaming:
        # 流式用 stream 接收
        return f"""import httpx

API_KEY = "YOUR_API_KEY"

# AI 流式：逐行读 SSE
with httpx.stream(
    "{method.upper()}",
    "{url}",
    headers={{"X-API-Key": API_KEY}},
    json={{"prompt": "你好"}} if {has_body} else None,
    timeout=60.0,
) as resp:
    for line in resp.iter_lines():
        if line.startswith("data: "):
            print(line[6:])
"""

    body_arg = ""
    if has_body:
        body_arg = ',\n    json={"key": "value"}'
    return f"""import httpx

API_KEY = "YOUR_API_KEY"

resp = httpx.request(
    "{method.upper()}",
    "{url}",
    headers={{"X-API-Key": API_KEY}}{body_arg},
    timeout=30.0,
)
print(resp.status_code)
print(resp.json())
"""


def _javascript(method: str, url: str, has_body: bool, meta: ApiMeta) -> str:
    if meta.backend_type == "ai_model" and meta.ai_streaming:
        return f"""const API_KEY = "YOUR_API_KEY";

// AI 流式：用 ReadableStream 读 SSE，按 "data: " 前缀解析每行
const resp = await fetch("{url}", {{
  method: "{method.upper()}",
  headers: {{
    "X-API-Key": API_KEY,
    "Content-Type": "application/json",
  }},
  body: JSON.stringify({{ prompt: "你好" }}),
}});

const reader = resp.body.getReader();
const decoder = new TextDecoder();
let buffer = "";
while (true) {{
  const {{ done, value }} = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, {{ stream: true }});
  const lines = buffer.split("\\n");
  buffer = lines.pop();
  for (const line of lines) {{
    if (line.startsWith("data: ")) {{
      console.log(line.slice(6));
    }}
  }}
}}
"""

    body_lines = ""
    if has_body:
        body_lines = """,
  body: JSON.stringify({ key: "value" })"""
    return f"""const API_KEY = "YOUR_API_KEY";

const resp = await fetch("{url}", {{
  method: "{method.upper()}",
  headers: {{
    "X-API-Key": API_KEY,
    "Content-Type": "application/json",
  }}{body_lines}
}});

const data = await resp.json();
console.log(data);
"""
