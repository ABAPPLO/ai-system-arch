"""CLI main 入口测试 —— validate / apply / dry-run。"""


import respx
from apihub_cli.main import main


class TestValidateCmd:
    def test_validate_single_file(self, sample_yaml_file, capsys):
        rc = main(["validate", str(sample_yaml_file)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "user-query" in out
        assert "1 definition(s)" in out

    def test_validate_dir(self, yaml_dir, capsys):
        rc = main(["validate", str(yaml_dir)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "user-query" in out
        assert "llm-chat" in out
        assert "2 definition(s)" in out

    def test_validate_invalid_yaml_returns_1(self, tmp_path, capsys):
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "api: { name: x }\n",  # 缺 category / base_path
            encoding="utf-8",
        )
        rc = main(["validate", str(bad)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "❌" in err


class TestApplyCmd:
    def test_apply_dry_run_no_http_calls(
        self, sample_yaml_file, capsys,
    ):
        """--dry-run：仅打印，不调 api-registry。"""
        rc = main([
            "apply", str(sample_yaml_file),
            "--env", "dev", "--dry-run",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "DRY-RUN" in out
        assert "user-query" in out

    def test_apply_calls_registry_end_to_end(
        self, sample_yaml_file, capsys,
    ):
        with respx.mock(base_url="http://localhost:8000") as mock:
            mock.get("/v1/apis").respond(200, json={"items": []})
            mock.post("/v1/apis").respond(200, json={"api_id": "api_001"})
            mock.post("/v1/api-versions").respond(
                200,
                json={
                    "id": "ver_001", "api_id": "api_001", "version": "v1",
                    "backend_type": "http", "backend_url": "x",
                    "status": "draft", "created_at": "2026-07-07T00:00:00Z",
                },
            )
            mock.post("/v1/change-requests").respond(
                201, json={"request_id": 42, "status": "approved"},
            )

            rc = main([
                "apply", str(sample_yaml_file),
                "--env", "dev",
                "--submitted-by", "u_tester",
            ])

        assert rc == 0
        out = capsys.readouterr().out
        assert "✨ 1 applied" in out
        assert "api_001" in out
        assert "cr#42" in out

    def test_apply_failure_returns_1(
        self, sample_yaml_file, capsys,
    ):
        with respx.mock(base_url="http://localhost:8000") as mock:
            mock.get("/v1/apis").respond(500, json={"message": "db down"})
            rc = main([
                "apply", str(sample_yaml_file), "--env", "dev",
            ])
        assert rc == 1
        err = capsys.readouterr().err
        assert "failed" in err


class TestApplyMultiEnv:
    """同一份 YAML，dev 自动 approved，prod 仅 pending。"""

    def test_dev_auto_approved(self, sample_yaml_file, capsys):
        with respx.mock(base_url="http://localhost:8000") as mock:
            mock.get("/v1/apis").respond(200, json={"items": []})
            mock.post("/v1/apis").respond(200, json={"api_id": "api_d"})
            mock.post("/v1/api-versions").respond(
                200,
                json={
                    "id": "ver", "api_id": "api_d", "version": "v1",
                    "backend_type": "http", "backend_url": "x",
                    "status": "draft", "created_at": "2026-07-07T00:00:00Z",
                },
            )
            mock.post("/v1/change-requests").respond(
                201, json={"request_id": 1, "status": "approved"},
            )
            rc = main([
                "apply", str(sample_yaml_file), "--env", "dev",
            ])
        assert rc == 0
        out = capsys.readouterr().out
        # ✅ 表示 approved 状态
        assert "✅ user-query" in out

    def test_prod_pending_marker(self, sample_yaml_file, capsys):
        with respx.mock(base_url="http://localhost:8000") as mock:
            mock.get("/v1/apis").respond(200, json={"items": []})
            mock.post("/v1/apis").respond(200, json={"api_id": "api_p"})
            mock.post("/v1/api-versions").respond(
                200,
                json={
                    "id": "ver", "api_id": "api_p", "version": "v1",
                    "backend_type": "http", "backend_url": "x",
                    "status": "draft", "created_at": "2026-07-07T00:00:00Z",
                },
            )
            mock.post("/v1/change-requests").respond(
                201, json={"request_id": 2, "status": "pending"},
            )
            rc = main([
                "apply", str(sample_yaml_file), "--env", "prod",
            ])
        assert rc == 0
        out = capsys.readouterr().out
        # ⏳ 表示 pending
        assert "⏳ user-query" in out
