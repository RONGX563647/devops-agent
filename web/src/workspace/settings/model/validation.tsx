/** 模型参数的本地校验与提示文案：服务端仍是唯一裁判。 */

import { ApiError } from '../../../api/client';
import type { ModelBound, ModelPatch, ModelValues } from '../../../types';

/** 推理强度的可选项：与后端 BOUNDS 的 choices 一致，后端仍是唯一裁判。 */
export const EFFORTS = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh'];

/** 空字符串表示「用默认」：数字/枚举的清空语义统一走 null。 */
export function toNumber(raw: string, integer: boolean): number | null {
  const text = raw.trim();
  if (!text) return null;
  const value = Number(text);
  if (!Number.isFinite(value)) return null;
  return integer ? Math.round(value) : value;
}

/** 把结构化 detail 翻成一句人话；400 要指出字段，409 要指出版本冲突。 */
export function errorText(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return error instanceof Error ? error.message : '保存失败';
  }
  const detail = error.detail as { code?: string; field?: string; reason?: string } | null;
  if (detail?.code === 'invalid_model_config') {
    return `${detail.field}：${detail.reason ?? '取值非法'}`;
  }
  if (detail?.code === 'config_version_conflict') {
    return '配置已被别处改过，请关闭面板重开再改一次';
  }
  return error.message;
}

/** 危险改动提示：不是拦路，是让人知道自己在改什么。 */
export function dangersOf(draft: ModelPatch, base: ModelValues): string[] {
  const notes: string[] = [];
  if (typeof draft.model === 'string' && draft.model !== base.model) {
    notes.push('下一次模型调用即生效。若模型名无效，该次调用会失败且不会自动回滚到旧模型。');
  }
  if (draft.context_size !== undefined && draft.context_size !== base.context_size) {
    notes.push('压缩阈值会按当前触发比例重算，窗口变小可能立刻触发一次压缩。');
  }
  if (draft.max_iters !== undefined && (draft.max_iters ?? 0) > 50) {
    notes.push(
      `一次回复最多可能进行 ${draft.max_iters} 轮工具调用，耗时与 token 消耗相应放大。`,
    );
  }
  if (draft.model_max_retries !== undefined || draft.agent_max_retries !== undefined) {
    // 具体次数由后端的 derived 给，这里只说「会变」，不自己乘
    notes.push('重试次数变了：最坏情况下的模型请求次数会随之放大（保存后见推算值）。');
  }
  return notes;
}

/** 本地越界检查：只为提前禁用保存，服务端仍是唯一裁判。 */
export function boundError(
  values: ModelPatch,
  bounds: Record<string, ModelBound>,
): string {
  const table = values as unknown as Record<string, unknown>;
  for (const [key, bound] of Object.entries(bounds)) {
    const value = table[key];
    if (typeof value !== 'number') continue;
    if (bound.min !== undefined && value < bound.min) return `${key} 不能小于 ${bound.min}`;
    if (bound.max !== undefined && value > bound.max) return `${key} 不能大于 ${bound.max}`;
  }
  return '';
}
