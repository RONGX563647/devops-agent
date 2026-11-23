/** 生成客户端 id：优先 crypto.randomUUID（安全上下文），否则用时间 + 随机数兜底。 */
export function newId(prefix: string): string {
  const cryptoApi = typeof crypto !== 'undefined' ? crypto : undefined;
  const uuid = cryptoApi && typeof cryptoApi.randomUUID === 'function'
    ? cryptoApi.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  return `${prefix}-${uuid}`;
}
