/**
 * OpenJudge 评测接口：批次、用例、题库与在线运行。
 *
 * 与观测接口分开，是因为两者生命周期完全不同：观测只读，评测还带写端点
 * （发起任务、改配置、改题库）。混在一个文件里，改评测不需要读一遍观测代码。
 */

import type {
  OjBatch,
  OjBatchList,
  OjCalibrationStatus,
  OjCaseDetail,
  OjCaseDocument,
  OjCaseDocumentBody,
  OjCaseList,
  OjCompare,
  OjCoverage,
  OjHealth,
  OjLibrary,
  OjPatrol,
  OjRubric,
  OjRun,
  OjRunCreated,
  OjRunList,
  OjRunRequest,
  OjScoringConfig,
  OjSeries,
  OjSettings,
  OjSettingsValues,
  OjSuites,
} from '../types';
import { getJson, sendJson } from './http';

/** OpenJudge：最新批次健康度；无数据时 has_data=false。 */
export function fetchOpenJudgeHealth(): Promise<OjHealth> {
  return getJson<OjHealth>('/api/openjudge/health');
}

/**
 * OpenJudge：批次列表（按时间倒序）；verdict 非空时按结论过滤。
 * `total` 是库里的真实条数，分页控件据此算页数 —— 不要用 items.length 当总数。
 */
export function fetchOpenJudgeBatches(
  limit = 20,
  verdict?: string,
  offset = 0,
): Promise<OjBatchList> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (verdict) params.set('verdict', verdict);
  return getJson<OjBatchList>(`/api/openjudge/batches?${params.toString()}`);
}

/** OpenJudge：巡检视图的聚合数据（最近 N 批的赛道通过率与结论分布）。 */
export function fetchOpenJudgePatrol(limit = 10): Promise<OjPatrol> {
  return getJson<OjPatrol>(`/api/openjudge/patrol?limit=${limit}`);
}

/** OpenJudge：判分方案、门槛与赛道规则（单一来源）。 */
export function fetchOpenJudgeScoringConfig(): Promise<OjScoringConfig> {
  return getJson<OjScoringConfig>('/api/openjudge/scoring-config');
}

/** OpenJudge：当前裁判校准状态；门槛值一并由服务端下发。 */
export function fetchOpenJudgeCalibration(): Promise<OjCalibrationStatus> {
  return getJson<OjCalibrationStatus>('/api/openjudge/calibration');
}

/** OpenJudge：单批次详情（含 summary）。 */
export function fetchOpenJudgeBatch(id: number): Promise<OjBatch> {
  return getJson<OjBatch>(`/api/openjudge/batches/${id}`);
}

export interface OjCaseQuery {
  track?: string;
  status?: string;
  subject?: string;
  passed?: boolean;
  /** 每页条数；优先于 limit 映射到服务端的 limit。 */
  pageSize?: number;
  limit?: number;
  offset?: number;
}

/** OpenJudge：批次内用例分页。 */
export function fetchOpenJudgeCases(
  batchId: number,
  params: OjCaseQuery = {},
): Promise<OjCaseList> {
  const query = new URLSearchParams();
  if (params.track) query.set('track', params.track);
  if (params.status) query.set('status', params.status);
  if (params.subject) query.set('subject', params.subject);
  if (params.passed !== undefined) query.set('passed', String(params.passed));
  query.set('limit', String(params.pageSize ?? params.limit ?? 100));
  query.set('offset', String(params.offset ?? 0));
  return getJson<OjCaseList>(
    `/api/openjudge/batches/${batchId}/cases?${query.toString()}`,
  );
}

/** OpenJudge：单条用例完整明细（规则检查、裁判意见、轨迹）。 */
export function fetchOpenJudgeCase(
  batchId: number,
  rowId: number,
): Promise<OjCaseDetail> {
  return getJson<OjCaseDetail>(`/api/openjudge/batches/${batchId}/cases/${rowId}`);
}

/** OpenJudge：两批次对比。 */
export function fetchOpenJudgeCompare(base: number, head: number): Promise<OjCompare> {
  const params = new URLSearchParams({ base: String(base), head: String(head) });
  return getJson<OjCompare>(`/api/openjudge/compare?${params.toString()}`);
}

/** OpenJudge：题库覆盖与校验问题。 */
export function fetchOpenJudgeCoverage(): Promise<OjCoverage> {
  return getJson<OjCoverage>('/api/openjudge/coverage');
}

/** OpenJudge：rubric 工件清单。 */
export function fetchOpenJudgeRubrics(): Promise<{ items: OjRubric[]; total: number }> {
  return getJson<{ items: OjRubric[]; total: number }>('/api/openjudge/rubrics');
}

/** OpenJudge：题库清单（赛道、用例数、阈值、最近一批结论）。 */
export function fetchOpenJudgeSuites(): Promise<OjSuites> {
  return getJson<OjSuites>('/api/openjudge/suites');
}

/** OpenJudge：最近 N 天的批次趋势。 */
export function fetchOpenJudgeSeries(days = 30): Promise<OjSeries> {
  return getJson<OjSeries>(`/api/openjudge/series?days=${days}`);
}

/**
 * 发起一次在线评测。
 *
 * 409 时不弹错：`ApiError.detail` 里带着正在跑的任务 `run_id`，
 * 调用方据此挂到那个任务上继续轮询。
 */
export function startOpenJudgeRun(body: OjRunRequest): Promise<OjRunCreated> {
  return sendJson<OjRunCreated>('/api/openjudge/runs', 'POST', body);
}

/** OpenJudge：单个在线任务的进度。 */
export function fetchOpenJudgeRun(id: number): Promise<OjRun> {
  return getJson<OjRun>(`/api/openjudge/runs/${id}`);
}

/** OpenJudge：最近的任务列表。 */
export function fetchOpenJudgeRuns(limit = 10): Promise<OjRunList> {
  return getJson<OjRunList>(`/api/openjudge/runs?limit=${limit}`);
}

/** OpenJudge：取消一个未完成的任务。 */
export function cancelOpenJudgeRun(id: number): Promise<{ id: number; status: string }> {
  return sendJson<{ id: number; status: string }>(
    `/api/openjudge/runs/${id}/cancel`,
    'POST',
  );
}

/** OpenJudge：评测配置（被测模型 / 裁判模型 / 默认开关）。 */
export function fetchOpenJudgeSettings(): Promise<OjSettings> {
  return getJson<OjSettings>('/api/openjudge/settings');
}

/** OpenJudge：局部更新配置；只传要改的字段。 */
export function patchOpenJudgeSettings(
  patch: Partial<OjSettingsValues>,
): Promise<OjSettings> {
  return sendJson<OjSettings>('/api/openjudge/settings', 'PATCH', patch);
}

/** OpenJudge：题库列表（仓库 + 本地覆盖层）。 */
export function fetchOpenJudgeLibrary(params: {
  track?: string;
  source?: string;
} = {}): Promise<OjLibrary> {
  const query = new URLSearchParams();
  if (params.track) query.set('track', params.track);
  if (params.source) query.set('source', params.source);
  const suffix = query.toString();
  return getJson<OjLibrary>(`/api/openjudge/cases${suffix ? `?${suffix}` : ''}`);
}

/** OpenJudge：单条用例的原始 JSON（编辑器直接改这份）。 */
export function fetchOpenJudgeCaseDocument(id: string): Promise<OjCaseDocument> {
  return getJson<OjCaseDocument>(`/api/openjudge/cases/${encodeURIComponent(id)}`);
}

/** OpenJudge：把批次里的用例回流成题库草稿（派生 `-b<batch>` 后缀，不覆盖原题）。 */
export function createCaseFromBatch(
  batchId: number,
  caseRowId: number,
  body: { id?: string; reviewer?: string; tier?: string } = {},
): Promise<OjCaseDocument> {
  return sendJson<OjCaseDocument>(
    `/api/openjudge/cases/from-batch/${batchId}/${caseRowId}`,
    'POST',
    body,
  );
}

/** OpenJudge：新增或修改一条用例（同 id 覆盖，写入本地覆盖层）。 */
export function saveOpenJudgeCase(
  id: string,
  raw: OjCaseDocumentBody,
): Promise<OjCaseDocument> {
  return sendJson<OjCaseDocument>(`/api/openjudge/cases/${encodeURIComponent(id)}`, 'PUT', raw);
}

/** OpenJudge：从题库隐藏一条（仓库文件不动，只记隐藏标记）。 */
export function deleteOpenJudgeCase(
  id: string,
): Promise<{ id: string; suite: OjLibrary['suite'] }> {
  return sendJson<{ id: string; suite: OjLibrary['suite'] }>(
    `/api/openjudge/cases/${encodeURIComponent(id)}`,
    'DELETE',
  );
}

/** OpenJudge：丢弃本地改动，恢复仓库版本。 */
export function restoreOpenJudgeCase(id: string): Promise<OjCaseDocument> {
  return sendJson<OjCaseDocument>(
    `/api/openjudge/cases/${encodeURIComponent(id)}/restore`,
    'POST',
  );
}
