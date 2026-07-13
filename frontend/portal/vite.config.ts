import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// dev proxy：前端 /v1/portal/* → portal-bff:8011
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      '/v1/portal': 'http://localhost:8011',
    },
  },
});
