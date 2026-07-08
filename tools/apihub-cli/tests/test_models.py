"""models / loader 测试。"""

import pytest
from apihub_cli.models import (
    ApiDefinition,
    BackendType,
    Method,
    load_dir,
    load_yaml,
)


class TestLoader:
    def test_load_single_yaml(self, sample_yaml_file):
        d = load_yaml(sample_yaml_file)
        assert isinstance(d, ApiDefinition)
        assert d.api.name == "user-query"
        assert d.api.base_path == "/user-service"
        assert d.version.version == "v1"
        assert d.version.backend_type == BackendType.HTTP
        assert d.version.method == Method.GET

    def test_load_dir_collects_all(self, yaml_dir):
        defs = load_dir(yaml_dir)
        names = {d.api.name for d in defs}
        assert names == {"user-query", "llm-chat"}

    def test_ai_yaml_parsed_correctly(self, ai_yaml_file):
        d = load_yaml(ai_yaml_file)
        assert d.version.backend_type == BackendType.AI_MODEL
        assert d.version.ai_model == "qwen-max"
        assert d.version.ai_streaming is True

    def test_invalid_backend_url_rejected(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            """
api:
  name: bad-api
  category: x
  base_path: /x
version:
  version: v1
  backend_type: http
  backend_url: not-a-url
  method: GET
  path: /v1/x
""",
            encoding="utf-8",
        )
        with pytest.raises(Exception, match="backend_url must start with"):
            load_yaml(bad)

    def test_task_scheme_accepted(self, tmp_path):
        """task:// 是异步任务的合法 backend_url。"""
        p = tmp_path / "task.yaml"
        p.write_text(
            """
api:
  name: user-import
  category: x
  base_path: /x
version:
  version: v1
  backend_type: async_task
  backend_url: task://user-import
  method: POST
  path: /v1/import
""",
            encoding="utf-8",
        )
        d = load_yaml(p)
        assert d.version.backend_url == "task://user-import"

    def test_invalid_version_format_rejected(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            """
api:
  name: bad-api
  category: x
  base_path: /x
version:
  version: "1.0"
  backend_type: http
  backend_url: http://x/v1
  method: GET
  path: /v1/x
""",
            encoding="utf-8",
        )
        with pytest.raises(Exception, match="string_pattern_mismatch"):
            load_yaml(bad)

    def test_proposed_config_includes_full_spec(self, sample_yaml_file):
        d = load_yaml(sample_yaml_file)
        cfg = d.proposed_config()
        assert "api" in cfg
        assert "version" in cfg
        assert cfg["api"]["name"] == "user-query"
