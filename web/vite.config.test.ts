import { describe, expect, it } from 'vitest';
import { proxy } from './vite.config';

/**
 * 复刻 Vite 代理的键匹配规则：`^` 开头当正则，否则按前缀。
 * 规则本身来自 Vite 的 http-proxy 中间件，这里只验我们的键选对了没有。
 */
function proxied(url: string): boolean {
  return Object.keys(proxy).some((key) =>
    key.startsWith('^') ? new RegExp(key).test(url) : url.startsWith(key),
  );
}

describe('Vite 开发代理边界', () => {
  it('转发后端的 /run 接口', () => {
    // 主链路：漏了这一个，聊天区一开口就是「流式请求失败 404」。
    expect(proxied('/run/stream')).toBe(true);
    expect(proxied('/run/42/pending?session_id=abc')).toBe(true);
    expect(proxied('/run/42/confirm')).toBe(true);
    expect(proxied('/run')).toBe(true);
  });

  it('不转发前端自己的 /runs 路由', () => {
    // 前缀匹配 `/run` 会把这两个吃掉，页面白屏。
    expect(proxied('/runs')).toBe(false);
    expect(proxied('/runs/7')).toBe(false);
  });

  it('保留 /api 与 /health', () => {
    expect(proxied('/api/runs')).toBe(true);
    expect(proxied('/api/eval/batches')).toBe(true);
    expect(proxied('/health')).toBe(true);
  });

  it('精确到 ^/run$ 的写法会漏掉 /run/stream', () => {
    // 这是 #32 引入的回归：为了避开 /runs 而收得太紧，反而切断了主链路。
    expect(new RegExp('^/run$').test('/run/stream')).toBe(false);
    expect(new RegExp('^/run(/|$)').test('/run/stream')).toBe(true);
    expect(new RegExp('^/run(/|$)').test('/runs')).toBe(false);
  });
});
