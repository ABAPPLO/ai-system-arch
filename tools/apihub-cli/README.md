# apihub-cli

> 把声明式 YAML 推到 [api-registry](../../services/services/api-registry/) 的 CLI 工具。
> 配合 CI 模板实现「Git PR → 自动同步到 api-registry」的接入流（ADR-003）。

## 安装

```bash
# 仓库根目录
pip install -e tools/apihub-cli/
```

提供命令 `apihub-apply`（也可用 `python -m apihub_cli`）。

## 子命令

### `validate <path>`

仅解析校验，不调远端。`<path>` 可以是单个 yaml 文件或目录。

```bash
apihub-apply validate schema/user-service/
# ✅ 2 definition(s) parsed:
#   - user-query @ /user-service (http GET /v1/users/{user_id})
#   - user-create @ /user-service (async_task POST /v1/users)
```

### `apply <path> --env <env>`

把 YAML 推到 api-registry：

```bash
apihub-apply schema/ \
  --base-url http://api-registry:8000 \
  --api-key ak_xxx \
  --env dev \
  --submitted-by "alice"
```

参数：

| 参数 | 默认 | 说明 |
|------|------|------|
| `--base-url` | `http://localhost:8000` | api-registry 地址 |
| `--api-key` | `dev_local` | X-API-Key |
| `--env` | `dev` | `dev` / `staging` / `prod` |
| `--submitted-by` | `ci@apihub` | 工单提交人，写入审计 |
| `--dry-run` | off | 仅打印，不发请求 |

## 行为

按 YAML 文件顺序对每份定义执行：

1. **找/建 api**：调 `GET /v1/apis` 按 `name` 去重查；查不到调 `POST /v1/apis`。
2. **建 version**：调 `POST /v1/api-versions`（每次都新增一行）。
3. **提工单**：调 `POST /v1/change-requests`。
   - `target_env=dev` → api-registry 自动 `approved` 并 apply。
   - `target_env=staging|prod` → 工单 `pending`，等平台运维审批。

详细流程见 [docs/05-core-flows.md §1](../../docs/05-core-flows.md)。

## CI 集成

仓库提供两套模板（业务方 drop-in）：

- GitLab CI: [`templates/ci/gitlab-ci.apihub.yml`](../../templates/ci/gitlab-ci.apihub.yml)
- GitHub Actions: [`templates/ci/github-action.apihub.yml`](../../templates/ci/github-action.apihub.yml)

业务仓库的 `.gitlab-ci.yml` 添加：

```yaml
include:
  - project: 'platform/apihub-templates'
    file: '/templates/ci/gitlab-ci.apihub.yml'

variables:
  APIHUB_API_KEY: $CI_APIHUB_API_KEY  # GitLab Variable
```

合并 main → CI 自动 apply 到 dev。打 tag `staging-x.y.z` / `vX.Y.Z` → 手动触发对应环境 apply。

## 开发

```bash
# 测试
python -m pytest tools/apihub-cli/tests/ -v

# 添加新字段：models.py 加 → 加测试 → 提 PR
```

依赖：`pydantic` `pyyaml` `httpx`（dev: `respx` `pytest`）。
