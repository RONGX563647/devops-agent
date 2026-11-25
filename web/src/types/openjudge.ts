/** OpenJudge 评测 API 的响应类型（与 devops_agent.openjudge.api 对齐）。 */

export interface OjKpi {
  cases: number;
  passed: number;
  judge_n: number;
  violations: number;
}

export interface OjTrackStat {
  n: number;
  passed: number;
  pass_rate: number;
  judge_avg: number | null;
  violations: number;
  verdict: 'pass' | 'fail' | 'insufficient';
}

export interface OjFailure {
  case_id: string;
  track: string;
  status: string;
  violations: number;
  failed_checks: string[];
  repro: string;
}

export interface OjBatchSummary {
  verdict: 'pass' | 'fail' | 'insufficient';
  kpi: OjKpi;
  tracks: Record<string, OjTrackStat>;
  subjects: Record<string, number>;
  failures: OjFailure[];
  pareto: { check: string; count: number }[];
  meta?: Record<string, string>;
}

export interface OjBatch {
  id: number;
  verdict: string;
  model: string;
  suite_sha: string;
  plan_sha: string;
  started_at: string;
  finished_at: string;
  summary: OjBatchSummary;
}

export interface OjCaseRow {
  id: number;
  case_id: string;
  track: string;
  subject: string;
  risk: string;
  status: string;
  passed: boolean;
  latency_ms: number;
  tokens: Record<string, number>;
}

export interface OjCaseDetail extends OjCaseRow {
  checks: { name: string; kind: string; passed: boolean; detail?: string }[];
  judge: Record<string, { score?: number; normalized?: number; weight?: number; reason?: string }>;
  events: string[];
  input_stages: unknown[];
  workspace: Record<string, unknown>;
  trace: Record<string, unknown>;
}

export interface OjCaseList {
  items: OjCaseRow[];
  total: number;
  limit: number;
  offset: number;
}

export interface OjCompare {
  base: number;
  head: number;
  comparable: boolean;
  suite_sha: [string, string];
  tracks: Record<string, { base: number; head: number; delta: number }>;
  flips: { case_id: string; base: boolean; head: boolean }[];
}

export interface OjCoverage {
  cases: number;
  suite_sha: string;
  coverage: {
    tracks?: Record<string, number>;
    risks?: Record<string, number>;
    subjects?: Record<string, number>;
    permissions?: Record<string, number>;
    tools?: Record<string, number>;
  };
  problems: string[];
}

export interface OjRubric {
  name: string;
  version: string;
  sha: string;
  status: string;
  reviewer?: string;
  created_at?: string;
}

export interface OjHealth {
  has_data: boolean;
  latest_batch: OjBatch | null;
  calibration: Record<string, number> | null;
  anchors: Record<string, number> | null;
}

// --- 题库 / 趋势 / 在线运行 -------------------------------------------

/** 某赛道最近一批的结论；从未跑过时为 null。 */
export interface OjSuiteLatest {
  batch_id: number;
  pass_rate: number;
  verdict: string;
  finished_at: string;
}

/** 一个赛道的题库规模、阈值与最近结论。 */
export interface OjSuiteTrack {
  track: string;
  cases: number;
  /** 阈值来自服务端 TRACK_RULES，如 {pass_rate: 0.6, judge: 0.6, min_n: 10}。 */
  rules: Record<string, number>;
  latest: OjSuiteLatest | null;
}

export interface OjSuites {
  total: number;
  suite_sha: string;
  tracks: OjSuiteTrack[];
}

/** 趋势图上的一个批次点。 */
export interface OjSeriesPoint {
  batch_id: number;
  finished_at: string;
  verdict: string;
  pass_rate: number;
  cases: number;
  /** 各赛道通过率；没跑到的赛道不出现。 */
  tracks: Record<string, number>;
}

export interface OjSeries {
  points: OjSeriesPoint[];
}

export type OjRunStatus = 'pending' | 'running' | 'done' | 'failed' | 'cancelled';

export interface OjRun {
  id: number;
  status: OjRunStatus;
  /** 空串表示全部赛道。 */
  track: string;
  total: number;
  done: number;
  judge: boolean;
  batch_id: number | null;
  error: string;
  elapsed_ms: number;
  current_case: string;
}

export interface OjRunRequest {
  track?: string;
  limit?: number;
  judge?: boolean;
  concurrency?: number;
}

export interface OjRunCreated {
  run_id: number;
  total: number;
}

export interface OjRunList {
  items: OjRun[];
  total: number;
}

/** POST /runs 的 409 detail：直接挂到这个 run 上，而不是报错。 */
export interface OjRunConflictDetail {
  message: string;
  run_id: number;
}

/** 可写接口下发的边界值：前端不自己编一份「并发上限是几」。 */
export interface OjEditLimits {
  max_concurrency: number;
  min_timeout: number;
  max_timeout: number;
  max_limit: number;
  tracks: string[];
  risks: string[];
  permissions: string[];
}

/** 评测配置：`''` 表示「不覆盖，跟随默认」。 */
export interface OjSettingsValues {
  target_model: string;
  judge_model: string;
  judge: boolean;
  concurrency: number;
  timeout: number;
}

export interface OjSettings {
  values: OjSettingsValues;
  /** 实际生效的两个模型名（空值已解析到具体模型）。 */
  effective: { target_model: string; judge_model: string };
  limits: OjEditLimits;
}

/** 题库现状：条数 + 指纹 + 本地覆盖层情况。 */
export interface OjSuiteView {
  cases: number;
  suite_sha: string;
  overlay: number;
  deleted: number;
  dir: string;
}

export interface OjLibraryCase {
  id: string;
  track: string;
  risk: string;
  permission: string;
  subject: string;
  fixture: string;
  offline: boolean;
  /** repo = 仓库自带；overlay = 本地改过或新增。 */
  source: 'repo' | 'overlay';
  /** 真 = 覆盖层里有一条同 id 的修改（能还原）。 */
  edited: boolean;
}

export interface OjLibrary {
  items: OjLibraryCase[];
  total: number;
  suite: OjSuiteView;
  thresholds: Record<string, Record<string, number>>;
  limits: OjEditLimits;
}

/** 单条用例的原始 JSON；下划线字段是服务端附加的来源信息。 */
export type OjCaseDocumentBody = Record<string, unknown> & {
  id?: string;
  _source?: 'repo' | 'overlay';
  _edited?: boolean;
  _deleted_from_repo?: boolean;
};

export interface OjCaseDocument {
  case: OjCaseDocumentBody;
  limits?: OjEditLimits;
  suite?: OjSuiteView;
}
