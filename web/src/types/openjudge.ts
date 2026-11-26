/** OpenJudge 评测 API 的响应类型（与 devops_agent.openjudge.api 对齐）。 */

/**
 * 五种结论。`partial` 与 `unvalidated` 是拆出来的两个新态：
 * 前者说「只测了一部分」，后者说「全测了但质量没证实」——
 * 与 `insufficient`（样本不够）的修复动作完全不同，不能合并成一个词。
 */
export type OjVerdict =
  | 'pass'
  | 'fail'
  | 'partial'
  | 'unvalidated'
  | 'insufficient';

export interface OjKpi {
  /** 结果行数（含多次试验），不等于用例条数。 */
  cases: number;
  /** 去重后的用例条数；多次试验时 cases > case_count。 */
  case_count?: number;
  runs?: number;
  /** 每条用例重复次数；1 表示单次试验。 */
  trials?: number;
  passed: number;
  judge_n: number;
  violations: number;
  /** pass@k（至少过一次）与 pass^k（k 次全过）在 k=1 时相等。 */
  pass_at_k?: number;
  pass_hat_k?: number;
  /** Wilson 区间；样本为 0 时后端给 null。 */
  ci?: [number, number] | null;
  /** 规则全过但质量未证实的条数。 */
  unvalidated?: number;
  /** 终态分布与失败归因，由后端汇总；缺字段的老批次显示「—」。 */
  status_counts?: Record<string, number>;
  rule_failed_cases?: number;
  judge_failed_cases?: number;
  not_judged_cases?: number;
  pass_rate?: number;
  latency_p95_ms?: number;
  tokens?: Record<string, number>;
  /** 未配置单价时为 null，配合 cost_note 说明原因，绝不写 0 冒充免费。 */
  cost_usd?: number | null;
  cost_note?: string;
}

/** 严重度决定这条原因能否挡住发布：advisory 只提示，不否决。 */
export type OjSeverity = 'blocking' | 'advisory';

/** 赛道未达标原因：实际值与阈值都由后端给出，前端只展示不比较。 */
export interface OjTrackReason {
  code: string;
  actual: number | null;
  threshold: number | null;
  message: string;
  severity?: OjSeverity;
}

/** 批次级原因：赛道级依据摊平后带上赛道名，前端不再自己拼。 */
export interface OjCause {
  code: string;
  message: string;
  severity: OjSeverity;
  track: string;
}

export interface OjTrackStat {
  /** 结果行数；多次试验时会大于用例条数。 */
  n: number;
  total?: number;
  passed: number;
  pass_rate: number;
  /** 只按规则算的通过率：裁判没跑不该被打成规则失败。 */
  rule_pass_rate?: number;
  judge_avg: number | null;
  judge_failed?: number;
  unvalidated?: number;
  violations: number;
  ci?: [number, number] | null;
  verdict: OjVerdict;
  reasons?: OjTrackReason[];
  /** 挑战集在此赛道的成绩：失败只影响挑战分，不否决批次。 */
  challenge?: { total: number; passed: number };
}

export interface OjFailure {
  case_id: string;
  track: string;
  status: string;
  violations: number;
  failed_checks: string[];
  /** 归一化后低于门槛的裁判维度；与 failed_checks 分开，归因才可落地。 */
  judge_dims: string[];
  failure_type: string;
  repro: string;
}

/** 批次覆盖范围：决定这批结论能不能当发布门禁。 */
export interface OjScope {
  mode: 'single_track' | 'selected' | 'full';
  measured_tracks: string[];
  unmeasured_tracks: string[];
}

/** 证据强度：裁判档位与校准样本都摆出来，不藏在细则里。 */
export interface OjEvidence {
  judge_level: string;
  judge_model: string;
  calibration_checked: boolean;
  calibration_n: number | null;
  calibration_spearman: number | null;
  measured_tracks: number;
}

/** 失败列表被截断时显式说明，否则「没有更多失败」是假的。 */
export interface OjTruncated {
  failures: boolean;
  pareto: boolean;
  total_failures: number;
}

/** 过程指标：白皮书的「过程评测集」，来源是已有轨迹，不额外采样。 */
export interface OjStageStat {
  traces: number;
  tool_calls: number;
  tool_call_success_rate: number | null;
  tool_param_ok_rate: number | null;
  avg_steps: number;
  retry_rate: number;
  timeout_rate: number;
  confirm_rate: number;
}

export interface OjBatchSummary {
  verdict: OjVerdict;
  scope?: OjScope;
  causes?: OjCause[];
  evidence?: OjEvidence;
  stages?: Record<string, OjStageStat>;
  truncated?: OjTruncated;
  /** 挑战集通过率；没有挑战集用例时为 null。 */
  challenge_score?: number | null;
  case_total?: number;
  kpi: OjKpi;
  tracks: Record<string, OjTrackStat>;
  subjects: Record<string, number>;
  failures: OjFailure[];
  /** 归因分成规则与裁判两栏：混在一张表里会让「哪种问题多」不可判。 */
  pareto: { rule: { check: string; count: number }[]; judge: { check: string; count: number }[] };
  meta?: Record<string, string>;
  /** 批次执行时的校准状态；老批次没有该字段，展示为「未知」。 */
  calibration?: OjCalibrationStatus;
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

/** 批次分页：`total` 是库里的真实条数，不是本页条数。 */
export interface OjBatchList {
  items: OjBatch[];
  total: number;
  limit: number;
  offset: number;
}

/** 巡检契约：最近 N 批的赛道通过率与结论分布。 */
export interface OjPatrolTrack {
  batches: number;
  latest_pass_rate: number | null;
  avg_pass_rate: number | null;
  latest_verdict: string;
}

export interface OjPatrol {
  batches: number;
  tracks: Record<string, OjPatrolTrack>;
  verdicts: Record<string, number>;
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
  /** must_pass 一票否决，challenge 只进挑战分；老批次没有该列。 */
  tier?: string;
  /** 第几次试验（从 0 起）；单次试验时恒为 0。 */
  trial?: number;
}

/** 单条规则断言。security 断言违例单列，不并入语义质量均分。 */
export interface OjCheck {
  name: string;
  kind: string;
  passed: boolean;
  detail?: string;
  security?: boolean;
}

/** 裁判项：scored/skipped 是后端写入的新字段，旧字段保持可选以兼容历史批次。 */
export interface JudgeEntry {
  status?: 'scored' | 'skipped';
  skip_reason?: string;
  score?: number;
  normalized?: number;
  weight?: number;
  reason?: string;
}

export type OjFailureType =
  | ''
  | 'runtime'
  | 'security'
  | 'rule'
  | 'judge_skipped'
  | 'judge_score';

export interface OjJudgeFailure {
  key: string;
  normalized: number;
  weight: number;
  contribution: number;
}

/** 单条用例的判定依据：分数、失败类型、公式全部来自后端。 */
export interface OjScoring {
  rule_passed: boolean;
  violations: number;
  judge_norm: number | null;
  pass_judge: number;
  passed: boolean;
  formula: string;
  failure_type: OjFailureType;
  rule_failed_checks: string[];
  judge_failed: OjJudgeFailure[];
}

export interface OjToolCall {
  name: string;
  args: Record<string, unknown>;
  state: string;
  result_excerpt: string;
}

/** 执行轨迹快照；长文本字段只读展示，前端不重算。 */
export interface OjTrace {
  case_id?: string;
  run_id?: string;
  model?: string;
  status?: string;
  error?: string;
  turns: Record<string, string>[];
  tool_calls: OjToolCall[];
  events: string[];
  input_stages: Record<string, unknown>[];
  workspace: {
    states?: Record<string, unknown>;
    diff?: string;
    hashes?: Record<string, string>;
  } & Record<string, unknown>;
  judge_input: Record<string, unknown>;
  final_text: string;
}

export interface OjCaseDetail extends OjCaseRow {
  checks: OjCheck[];
  judge: Record<string, JudgeEntry>;
  scoring: OjScoring;
  events: string[];
  input_stages: unknown[];
  workspace: Record<string, unknown>;
  trace: OjTrace;
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

/** 三道可信度门禁：状态全部由服务端判定，前端只做展示与下钻入口。 */
export interface OjReviewGate {
  total: number;
  approved: number;
  pending: number;
  /** 历史批量导入的条数，用于提示「待逐条复核」。 */
  legacy: number;
  pending_ids: string[];
}

export interface OjRubricGate {
  ref: string;
  name: string;
  version: string;
  sha: string;
  /** none / missing / draft / frozen。 */
  status: string;
  reviewer: string;
  ready: boolean;
}

/**
 * 裁判校准状态。`level` 是三档唯一来源：
 * off（无记录，裁判分只展示）/ soft（n≥30 且 ρ≥0.60，可降为 unvalidated）/ hard（可否决）。
 */
export interface OjCalibrationStatus {
  checked: boolean;
  /** 兼容旧调用：等价于 level === 'hard'。 */
  valid: boolean;
  level?: 'off' | 'soft' | 'hard';
  missing: string[];
  required_n: number;
  required_spearman: number;
  /** 当前实际达成的样本量与相关性；null 表示连校准记录都没有。 */
  n?: number | null;
  spearman?: number | null;
  soft_n?: number;
  soft_spearman?: number;
  /** 升到下一档还差什么；`next` 为空串表示已达 hard。 */
  progress?: {
    n: number | null;
    spearman: number | null;
    next: string;
    soft: { n: number; spearman: number };
    hard: { n: number; spearman: number };
  } | null;
  calibration_ids?: number[];
}

export interface OjGates {
  review: OjReviewGate;
  rubric: OjRubricGate;
}

export interface OjHealth {
  has_data: boolean;
  latest_batch: OjBatch | null;
  calibration: Record<string, number> | null;
  anchors: Record<string, number> | null;
}

// 题库 / 配置 / 在线运行的类型在 `openjudge-edit`：那一侧是可写面，
// 改新增用例或改配置时不必读完整的批次结论模型。
