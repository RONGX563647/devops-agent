/** 记忆效果评测接口：批次列表、批次内的用例。只读，触发批次仍走 CLI。 */

import type { MemoryBatch, MemoryBatchList, MemoryCaseList } from '../types';
import { getJson } from './http';

/** 记忆评测：批次列表（按时间倒序）。 */
export function fetchMemoryBatches(limit = 20, offset = 0): Promise<MemoryBatchList> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  return getJson<MemoryBatchList>(`/api/quality/memory/batches?${params.toString()}`);
}

/** 记忆评测：单批次详情（含 summary）。 */
export function fetchMemoryBatch(id: number): Promise<MemoryBatch> {
  return getJson<MemoryBatch>(`/api/quality/memory/batches/${id}`);
}

/** 记忆评测：批次内用例分页，可选按变体/赛道/是否通过过滤。 */
export function fetchMemoryCases(
  batchId: number,
  params: { variant?: string; track?: string; limit?: number; offset?: number } = {},
): Promise<MemoryCaseList> {
  const query = new URLSearchParams();
  if (params.variant) query.set('variant', params.variant);
  if (params.track) query.set('track', params.track);
  query.set('limit', String(params.limit ?? 100));
  query.set('offset', String(params.offset ?? 0));
  return getJson<MemoryCaseList>(
    `/api/quality/memory/batches/${batchId}/cases?${query.toString()}`,
  );
}
