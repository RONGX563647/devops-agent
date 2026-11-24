/** 评测批次、题库与对比：与后端 Pydantic 响应对齐的前端类型。 */

export interface EvalTrends {
  count: number;
  averages: Record<string, number>;
  by_source: Record<string, number>;
}

/** 门禁三态：样本不足时是 insufficient，不能压成 false。 */
export interface EvalGate {
  key: string;
  value: number | null;
  min: number;
  min_samples: number;
  samples: number;
  state: 'pass' | 'fail' | 'insufficient';
  /** 三种「不能判」的成因；state=insufficient 时才有值。 */
  reason: EvalGateReason;
  /** 阈值本身的结论，与「样本够不够判」无关。 */
  threshold_state: 'pass' | 'fail' | null;
}

/** 为什么判不了：题库太小 / 裁判没跑 / 裁判样本不够。 */
export type EvalGateReason = 'n_below_gate' | 'no_samples' | 'samples_below_min' | null;

/**
 * 硬失败分类：与后端 `eval/kinds.py` 同源。
 *
 * 后端会产出 7 类，而这里曾只声明 3 类 —— `status` / `tool` / `text` / `grounding`
 * 四类失败在界面上完全不可见：一批 14/15 通过、结论 fail 的运行，硬失败表显示全 0。
 */
export const HARD_KIND_KEYS = [
  'safety',
  'forbidden_tool',
  'status',
  'tool',
  'text',
  'grounding',
  'format',
] as const;

export type HardKindKey = (typeof HARD_KIND_KEYS)[number];

/** 严重度决定配色与是否阻断合并：`blocking` 一票否决，`scoring` 只进通过率。 */
export const HARD_KIND_META: Record<
  HardKindKey,
  { label: string; severity: 'blocking' | 'scoring' }
> = {
  safety: { label: '安全越界', severity: 'blocking' },
  forbidden_tool: { label: '禁用工具', severity: 'blocking' },
  status: { label: '运行未完成', severity: 'blocking' },
  tool: { label: '工具轨迹', severity: 'scoring' },
  text: { label: '事实缺失', severity: 'scoring' },
  grounding: { label: '缺少定位', severity: 'scoring' },
  format: { label: '格式不符', severity: 'scoring' },
};

export type EvalHardFailures = Record<HardKindKey, number>;

/** 可比性：变了哪一类因子，决定 delta 能说多硬。 */
export interface EvalCompatibility {
  level: 'clean' | 'confounded' | 'blocking' | 'unknown';
  changed: string[];
  missing: string[];
  reasons: string[];
}

export type EvalVerdict =
  | 'pass'
  | 'warn'
  | 'fail'
  | 'insufficient'
  | 'running'
  | 'not_comparable';

/** 一次评测批次（GET /api/eval/batches）。 */
export interface EvalBatch {
  id: number;
  suite: string;
  suite_version: string;
  suite_sha: string;
  prompt_id: string;
  prompt_hash: string;
  model: string;
  profile: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  n: number;
  passed: number;
  pass_rate: number;
  hard_failures: EvalHardFailures;
  /** 阻断类失败数：>0 才拦合并。 */
  hard_blocking: number;
  /** 计分类失败数：已计入通过率，不单独否决。 */
  hard_scoring: number;
  /** {key: 中文标签}，由后端下发，前端不维护第二份映射。 */
  hard_labels: Record<string, string>;
  judge: Record<string, number> | null;
  trajectory_score: number | null;
  p95_ms: number | null;
  tokens_total: number;
  /** 前缀缓存命中量与写入量。 */
  cache_input: number;
  cache_creation: number;
  /** 命中率 = 命中 / 总输入；无数据时 null。 */
  cache_hit_rate: number | null;
  cost_total: number;
  baseline_id: number | null;
  verdict: EvalVerdict;
  gates: EvalGate[];
  min_cases_for_gate: number;
  mde: number | null;
  compatibility: EvalCompatibility | null;
  grader_version: string;
}

/** 一条用例的断言（GET /api/eval/suites）。 */
export interface EvalAssertion {
  kind: string;
  target: string;
}

export interface EvalSuiteCase {
  id: string;
  tags: string[];
  permission: string;
  subject: string;
  require_executed: boolean;
  assertions: EvalAssertion[];
  /** 按题面要求动作时会跑的代表性命令；没有则 null。 */
  command_hint: string | null;
  /** 档位可达性三态：见 EvalCaseDecision。 */
  decision: EvalCaseDecision;
  /** decision 非 ok 时的说明。 */
  problem: string | null;
}

/**
 * 用例在它声明的档位下能否真的执行要求的命令。
 *
 * - `ok`：一致；
 * - `mismatch`：档位与期望不符，用例结构上不可能通过；
 * - `defense_in_grader`：用例能跑，但防线不在权限层（例如 explore 档的只读命令
 *   自动放行，`cat /etc/shadow` 也会被判 allow）。
 *
 * 第三态必须能看见，否则「权限层挡住了」会被当成既有事实。
 */
export type EvalCaseDecision = 'ok' | 'mismatch' | 'defense_in_grader';

/** 评测集目录：题库构成与断言分布。 */
export interface EvalSuite {
  suite: string;
  version: string;
  sha256: string;
  counts: Record<string, number>;
  min_cases_for_gate: number;
  grader_version: string;
  thresholds: Record<string, { min: number; min_samples: number }>;
  assertions: Record<string, number>;
  /** 各 subject 的用例数：结论能归因到哪个被测因子。 */
  subjects: Record<string, number>;
  /** 配了裁判的用例数：回答「裁判样本够不够」。 */
  judged_cases: number;
  cases: EvalSuiteCase[];
}

export interface EvalSuiteList {
  items: EvalSuite[];
}

/** 批次时间序列的一个点。 */
export interface EvalSeriesPoint {
  id: number;
  started_at: string;
  n: number;
  pass_rate: number;
  judge_correctness: number | null;
  trajectory_score: number | null;
  p95_ms: number | null;
  hard_total: number;
  /** 阻断类与计分类分开：「阻断归零但计分上升」是完全不同的故事。 */
  hard_blocking: number;
  hard_scoring: number;
  /** 前缀缓存命中率；提示词布局改坏时它会先掉。 */
  cache_hit_rate: number | null;
  verdict: EvalVerdict;
  prompt_hash: string;
}

export interface EvalSeries {
  items: EvalSeriesPoint[];
}

/** 用例 × 批次通过矩阵；cells 为 null 表示该批没跑到这条。 */
export interface EvalMatrix {
  batches: { id: number; started_at: string }[];
  cases: { case_id: string; cells: (boolean | null)[] }[];
}

export interface EvalBatchList {
  items: EvalBatch[];
  total: number;
}

/** 用例里的一条规则检查结果。 */
export interface EvalCaseCheck {
  name: string;
  ok: boolean;
  detail: string;
}

export interface EvalCaseItem {
  case_id: string;
  status: string;
  passed: boolean;
  tags: string[];
  trajectory_score: number | null;
  judge: Record<string, number>;
  latency_ms: number;
  tokens: number;
  checks: EvalCaseCheck[];
  run_id: number | null;
  /** 发过调用（含被拒）。 */
  tool_names: string[];
  /** 真的执行了。 */
  executed_tools: string[];
  /** 被权限拒绝。 */
  denied_tools: string[];
  /** 命中的硬失败分类。 */
  hard_failures: HardKindKey[];
  error: string | null;
}

export interface EvalBatchDetail {
  batch: EvalBatch;
  tags: Record<string, { n: number; passed: number }>;
  /** 按 subject 归因：这次变化能算到哪个被测因子。 */
  subjects: Record<string, { n: number; passed: number }>;
  cases: EvalCaseItem[];
}

export interface EvalCompareMetric {
  key: string;
  base: number | null;
  head: number | null;
  delta: number | null;
  better: 'higher' | 'lower';
}

export interface EvalCompare {
  verdict: string;
  metrics: EvalCompareMetric[];
  cases: { case_id: string; base_passed: boolean; head_passed: boolean }[];
  n: number;
  base_n: number;
  /** 精度上限：小于该值的 delta 不超过一个用例的抖动。 */
  mde: number;
  compatibility: EvalCompatibility;
}

export interface EvalCaseList {
  items: EvalCaseItem[];
  total: number;
}
