import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// dev proxy：前端 /api/admin/* → admin-bff:8006
//          /api/registry/* → api-registry:8000
//          /api/retry/*    → retry-svc:8009
//          /api/trace/*    → trace-svc:8008
// prod：前端 nginx 反代同样的前缀
const targets = {
  admin: 'http://localhost:8006',
  registry: 'http://localhost:8000',
  retry: 'http://localhost:8009',
  trace: 'http://localhost:8008',
};

export default defineConfig({
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
