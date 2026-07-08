# APIHub Admin（管理后台前端）

Phase 2 MVP 后台，覆盖：

- 登录（X-API-Key，dev 期简化；Phase 3 替换为 SSO / OIDC）
- Dashboard（审计事件聚合）
- 失败重试（retry-svc 任务列表 + 详情 + 手动重试 / 忽略）
- 评审工单（api-registry 变更工单列表 + 详情 + 批准 / 驳回 / Apply）

## 技术栈

- **Vite 5** + **React 18** + **TypeScript 5**
- **Ant Design 5** + **@ant-design/pro-components**（ProLayout / ProTable）
- **react-router-dom v6**
- **SWR**（数据获取 + 缓存）
- **dayjs**

## 目录结构

```
src/
├── api/
│   ├── client.ts     # fetch wrapper + X-API-Key 注入 + 401 跳登录
│   └── types.ts      # 后端 Pydantic 模型的 TS 镜像
├── components/
│   └── Layout.tsx    # ProLayout 外壳（侧边栏 + 顶栏 + 租户 Tag）
├── pages/
│   ├── Login.tsx           # X-API-Key 登录
│   ├── Dashboard.tsx       # 平台概览（统计卡 + 最近审计事件）
│   ├── Retry.tsx           # 失败重试（ProTable + Drawer）
│   └── ChangeRequests.tsx  # 评审工单（ProTable + Drawer + Modal）
├── App.tsx           # 路由 + RequireAuth
└── main.tsx          # ConfigProvider（zhCN）
```

## 快速启动

```bash
# 1. 安装依赖（首次或 package-lock.json 变更后）
make admin-frontend-install
# 等价于： cd frontend/admin && npm install

# 2. 启动 dev server（端口 5173）
make run-admin-frontend
# 等价于： cd frontend/admin && npm run dev -- --host
```

打开 http://localhost:5173 ，用本地 dev 的 `ak_xxx` 登录（用户名随意填，超管勾选由后端返回，Phase 2 简化前端写死）。

## Dev 代理架构

`vite.config.ts` 把以下前缀代理到本地服务：

| 前缀             | 转发到                           | 说明              |
| ---------------- | -------------------------------- | ----------------- |
| `/api/admin`     | `http://localhost:8006`          | admin-bff         |
| `/api/registry`  | `http://localhost:8000`          | api-registry      |
| `/api/retry`     | `http://localhost:8009`          | retry-svc         |
| `/api/trace`     | `http://localhost:8008`          | trace-svc         |

需要本地启动对应服务（参考根目录 `make run-admin run-registry run-retry`），或修改 `vite.config.ts` 指向远程开发环境。

## 类型同步

后端权威模型在 `services/services/{admin,retry,api-registry,trace}/src/.../models.py`。
前端 `src/api/types.ts` 是手动镜像 —— 后端字段变更时需要同步修改（CI 还没接自动生成）。

Phase 3 计划：

- 后端 OpenAPI → `openapi-typescript` 自动生成
- 或用 `pydantic2ts` 直接从 Pydantic 模型产出 `.d.ts`

## 生产部署

### 构建

```bash
make admin-frontend-build
# 产出： frontend/admin/dist/
```

### nginx 配置

参考 `deploy/nginx/admin.conf`（同仓库）：

- 静态资源 root 指向 dist
- `/api/*` 反代到集群内对应服务（K8s ClusterIP / 内网 SLB）
- SPA fallback：`try_files $uri /index.html`

### K8s 部署形态（Phase 3）

- 单 Pod：nginx + 静态 dist
- Service：ClusterIP
- Ingress：HTTPS 证书 + auth-proxy（OAuth2 proxy / 阿里 IDaaS）

## 已知限制（Phase 2）

- ❌ 没有 SSR / 路由级权限校验（仅前端 `is_platform_admin` 控制 UI）
- ❌ 没有分页总数（list 接口没返回 total，前端用 `length < pageSize` 启发式判断）
- ❌ 登录未做表单校验（除必填）
- ❌ 没有国际化（全中文 hardcode）
- ❌ 没有 e2e 测试（手动验证）

Phase 3 会逐项补齐，详见 `docs/12-roadmap.md`。
