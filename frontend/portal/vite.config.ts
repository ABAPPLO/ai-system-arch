import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// dev proxy：前端 /v1/portal/* → portal-bff:8011
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5174,
    proxy: {
      '/v1/portal': 'http://localhost:8011',
      '/v1/trace': 'http://localhost:8008',
      '/v1/docs': 'http://localhost:8007',
    },
  },
});
