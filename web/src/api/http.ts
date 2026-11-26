/** 观测 API 的传输层：fetch + JSON 解析 + 错误形状，不含任何业务语义。 */

/** 统一读请求：非 2xx 直接抛错，调用方不需要自己判 status。 */
export async function getJson<T>(path: string): Promise<T> {
  const resp = await fetch(path);
  if (!resp.ok) {
    throw new Error(`请求失败 ${resp.status}: ${path}`);
  }
  return (await resp.json()) as T;
}

/** 带状态码与结构化 detail 的错误：400 要能定位到具体字段，409 要能带回当前版本。 */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(message: string, status: number, detail: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

/** 统一写请求：非 2xx 时把 FastAPI 的 detail 原样抛出来，交给调用方解读。 */
export async function sendJson<T>(
  path: string,
  method: string,
  body?: unknown,
): Promise<T> {
  const resp = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!resp.ok) {
    const payload = (await resp.json().catch(() => null)) as { detail?: unknown } | null;
    throw new ApiError(`请求失败 ${resp.status}`, resp.status, payload?.detail ?? null);
  }
  return (await resp.json()) as T;
}
