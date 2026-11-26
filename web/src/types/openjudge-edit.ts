/**
 * OpenJudge 可写面的类型：题库、评测配置、在线运行与趋势。
 *
 * 与 `openjudge`（批次与结论，只读）分开，是因为改题库/配置时不需要
 * 读完整的结论模型；两边混一个文件，500 行的上限也会先撞上。
 */

import type { OjGates } from './openjudge';

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

/** 评分契约（GET /scoring-config）：阈值、权重、量纲的唯一来源。 */
export interface OjJudgeSpec {
  key: string;
  grader: string;
  weight: number;
  scale: [number, number];
  /** 该裁判引用的 rubric，如 repo_qa_quality@v1；空串表示未引用。 */
  rubric?: string;
}

export interface OjTrackRule {
  pass_rate: number;
  min_n: number;
  judge?: number;
  max_violations?: number;
}

export interface OjScoringConfig {
  plan: { name: string; sha: string; judges: OjJudgeSpec[] };
  pass_judge: number;
  track_rules: Record<string, OjTrackRule>;
  /** 老服务端没有该字段，UI 降级为「门禁状态未知」。 */
  gates?: OjGates;
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
  /** 与 track 二选一：显式指定要复现的用例，仍受 max_limit 约束。 */
  case_ids?: string[];
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
  /** 每条用例重复次数上限；页面的 trials 选择器据此收边。 */
  max_trials?: number;
  tracks: string[];
  risks: string[];
  permissions: string[];
  /** 用例分层取值；老服务端没有该字段时页面退回内置两份。 */
  tiers?: string[];
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
  /** must_pass 一票否决，challenge 只进挑战分；老题库没标时后端补 must_pass。 */
  tier?: string;
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
