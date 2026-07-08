"""共享 fixtures。"""

import os

# 最小 env（避免 pydantic-settings 在测试时要求 PG/Redis）
os.environ.setdefault("PG_HOST", "localhost")
os.environ.setdefault("PG_USER", "apihub")
os.environ.setdefault("PG_PASSWORD", "test")
os.environ.setdefault("REDIS_HOST", "localhost")

import pytest  # noqa: E402

SAMPLE_YAML = """\
api:
  name: user-query
  category: user-service
  description: 根据用户 ID 查询用户信息
  base_path: /user-service
  tags: [user, query]
  owner: user-service-team

version:
  version: v1
  backend_type: http
  backend_url: http://user-service.internal/v1/users/{user_id}
  method: GET
  path: /v1/users/{user_id}
  request_schema:
    type: object
    properties:
      user_id: { type: string }
    required: [user_id]
  response_schema:
    type: object
    properties:
      user_id: { type: string }
      name: { type: string }
"""


AI_YAML = """\
api:
  name: llm-chat
  category: ai-service
  description: LLM 流式对话
  base_path: /ai-service
  tags: [ai, llm]

version:
  version: v1
  backend_type: ai_model
  backend_url: https://dashscope.aliyuncs.com/v1/chat
  method: POST
  path: /v1/chat
  ai_model: qwen-max
  ai_streaming: true
"""


@pytest.fixture
def sample_yaml_file(tmp_path):
    p = tmp_path / "user-query.yaml"
    p.write_text(SAMPLE_YAML, encoding="utf-8")
    return p


@pytest.fixture
def ai_yaml_file(tmp_path):
    p = tmp_path / "llm-chat.yaml"
    p.write_text(AI_YAML, encoding="utf-8")
    return p


@pytest.fixture
def yaml_dir(tmp_path, sample_yaml_file, ai_yaml_file):
    # 把两个样例 yaml 放进同一目录
    target = tmp_path / "schemas"
    target.mkdir()
    (target / sample_yaml_file.name).write_text(sample_yaml_file.read_text(), encoding="utf-8")
    (target / ai_yaml_file.name).write_text(ai_yaml_file.read_text(), encoding="utf-8")
    return target
