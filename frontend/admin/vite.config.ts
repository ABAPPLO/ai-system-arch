import path from 'node:path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// dev proxy：前端 /api/admin/* → admin-bff:8006
//          /api/registry/* → api-registry:8000
//          /api/retry/*    → retry-svc:8009
//          /api/trace/*    → trace-svc:8008
// prod：前端 nginx 反代同样的前缀
// 目标端口可用环境变量覆盖（本机端口冲突时免改代码）：
//   APIHUB_REGISTRY_PORT=18000 npm run dev
const targets = {
  admin: `http://localhost:${process.env.APIHUB_ADMIN_PORT || 8006}`,
  registry: `http://localhost:${process.env.APIHUB_REGISTRY_PORT || 8000}`,
  retry: `http://localhost:${process.env.APIHUB_RETRY_PORT || 8009}`,
  trace: `http://localhost:${process.env.APIHUB_TRACE_PORT || 8008}`,
  tenant: `http://localhost:${process.env.APIHUB_TENANT_PORT || 8005}`,
  docs: `http://localhost:${process.env.APIHUB_DOCS_PORT || 8007}`,
  billing: `http://localhost:${process.env.APIHUB_BILLING_PORT || 8014}`,
  auth: `http://localhost:${process.env.APIHUB_AUTH_PORT || 8002}`,
};

export default defineConfig({
  resolve: {
    alias: {
      // 共享包（client/format）以源码形式被两 app 引用，无需发布
      '@apihub/shared': path.resolve(__dirname, '../shared/src'),
    },
  },
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api/admin': {
        target: targets.admin,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api\/admin/, ''),
      },
      '/api/registry': {
        target: targets.registry,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api\/registry/, ''),
      },
      '/api/retry': {
        target: targets.retry,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api\/retry/, ''),
      },
      '/api/trace': {
        target: targets.trace,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api\/trace/, ''),
      },
      '/api/tenant': {
        target: targets.tenant,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api\/tenant/, ''),
      },
      '/api/docs': {
        target: targets.docs,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api\/docs/, ''),
      },
      '/api/billing': {
        target: targets.billing,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api\/billing/, ''),
      },
      '/api/auth': {
        target: targets.auth,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api\/auth/, ''),
      },
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          'react-vendor': ['react', 'react-dom', 'react-router-dom'],
          'antd-vendor': ['antd', '@ant-design/icons'],
          'pro-vendor': ['@ant-design/pro-components'],
          'swr-vendor': ['swr'],
        },
      },
    },
  },
});
