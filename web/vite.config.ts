import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 开发期把 /api、/run、/health 代理到后端，前端同源访问，避免 CORS 配置。
// /run 必须精确匹配：前缀匹配会连前端路由 /runs、/runs/:id 一起转发给后端而 404。
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '^/run$': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
    },
  },
});
