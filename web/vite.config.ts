import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const BACKEND = 'http://127.0.0.1:8000';

/**
 * 开发期代理表：让前端同源访问后端，避免每次都配 CORS。
 *
 * 路径选择是这里唯一的坑。后端的 run 相关接口有一个 `/run` 前缀：
 *   POST /run/stream              流式对话（主链路）
 *   GET  /run/{id}/pending        挂起态的待确认调用
 *   POST /run/{id}/confirm        提交确认决策
 * 而前端有同名前缀的路由：
 *   /runs、/runs/:id              运行列表与详情（由 react-router 渲染）
 *
 * 所以既不能用 `/run` 做前缀匹配（会把 /runs 也转发给后端，页面白屏 404），
 * 也不能精确到 `^/run$`（只匹配 /run 本身，把 /run/stream 漏掉 —— #32 的回归，
 * 表现为侧边栏一切正常、一开口就「流式请求失败 404」）。
 *
 * 正确的边界是「/run 后面紧跟 / 或字符串结束」：命中三个后端接口，避开 /runs。
 */
export const proxy: Record<string, string> = {
  '/api': BACKEND,
  '^/run(/|$)': BACKEND,
  '/health': BACKEND,
};

export default defineConfig({
  plugins: [react()],
  server: { proxy },
});
