import path from 'node:path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// dev proxy：前端 /v1/portal/* → portal-bff:8011
export default defineConfig({
  resolve: {
    alias: {
      // 共享包（client/format）以源码形式被两 app 引用，无需发布
      '@apihub/shared': path.resolve(__dirname, '../shared/src'),
    },
  },
  plugins: [react(), tailwindcss()],
  server: {
    port: 5174,
    proxy: {
      '/v1/portal': 'http://localhost:8011',
      '/v1/docs': 'http://localhost:8007',
    },
  },
});
