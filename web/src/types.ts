/** 与后端 Pydantic 响应对齐的前端类型。 */

/** 权限档位标识：与 governance/profiles.py 的 key 一一对应。 */
export type PermissionKey = 'readonly' | 'workspace' | 'execute' | 'unrestricted' | 'auto';

/** 越界兜底动作：直接拒 / 弹确认 / deny_rules 硬拦。 */
export type PermissionFallback = 'deny' | 'ask' | 'hard_deny';

/** 一个可选档位的展示信息（GET /api/permissions）。 */
export interface PermissionMeta {
  key: PermissionKey;
  label: string;
  note: string;
  fallback: PermissionFallback;
  /** 是否挂了 Write/Edit —— 界面据此显示「可写」标记。 */
  write: boolean;
}

export interface PermissionCatalog {
  default: PermissionKey;
  available: PermissionMeta[];
}

/** 一条等待确认的调用。input 是服务端给的展示文本，不是可回传的命令。 */
export interface PendingCall {
  tool_call_id: string;
  name: string;
  input: string;
  risk: 'read' | 'exec' | 'write' | 'danger';
}

/** 确认作用域：只这次 / 精确这条 / 这个命令前缀。 */
export type ConfirmScope = 'once' | 'exact' | 'prefix';

export interface ConfirmDecision {
  tool_call_id: string;
  confirmed: boolean;
  scope: ConfirmScope;
}

/** 挂起信息：GET /run/{id}/pending 的返回。 */
export interface PendingState {
  run_id: number;
  status: 'parked';
  expires_at: string | null;
  tool_calls: PendingCall[];
}

/** 前端内存里的挂起态：挂在助手消息上，刷新后仍能重建卡片。 */
export interface AwaitingState {
  runId: number | null;
  calls: PendingCall[];
  expiresAt: string | null;
}

export interface Run {
  id: number;
  session_id: string;
  domain: string;
  status: 'running' | 'success' | 'failed';
  input: string;
  output: string;
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
  model: string;
  total_tokens: number;
  health_score: number | null;
  input_tokens: number;
  output_tokens: number;
  /** 前缀缓存命中/写入量；老记录缺省为 0。 */
  cache_input_tokens?: number;
  cache_creation_input_tokens?: number;
  /** 本轮权限档位；老记录为空串。 */
  permission?: string;
  /** 实测（span 并集）与墙钟（起止差）两个耗时口径；老记录为 0。 */
  work_ms?: number;
  wall_ms?: number;
  /** 本轮实际下发的模型参数（JSON，只含非默认值）；空串 = 老记录未记录。 */
  model_params?: string;
}

export interface RunList {
  items: Run[];
  total: number;
  limit: number;
  offset: number;
}

export interface Span {
  id: number;
  run_id: number;
  type: 'model' | 'tool' | 'permission';
  name: string;
  start_offset_ms: number;
  duration_ms: number | null;
  input: string;
  output: string;
  tokens: number;
  status: 'ok' | 'error';
  error: string | null;
  /** 逐调用 token 分拆；工具 span 恒为 0，老记录缺省。 */
  input_tokens?: number;
  output_tokens?: number;
  cache_input_tokens?: number;
  cache_creation_input_tokens?: number;
  /** run 内单调序号与父 span：泳道与列表共用同一顺序。 */
  seq?: number;
  parent_id?: number | null;
}

/** 一条扣分项：回答「这 8 分扣在哪」。 */
export interface HealthItem {
  label: string;
  delta: number;
  detail: string;
}

/** 单个维度：value 为 null 表示证据不足，不参与加权。 */
export interface HealthDimension {
  key: string;
  label: string;
  value: number | null;
  weight: number;
  confidence: number;
  note: string;
  items: HealthItem[];
}

export interface RunHealth {
  run_id: number;
  score: number;
  grade: string;
  /** 有权重覆盖的比例：缺维度时 < 1，说明这个总分只代表部分信号。 */
  coverage: number;
  confidence: number;
  version: string;
  /** 按步数算出的耗时预算与实际耗时，前端画「花了多少 / 本该花多少」。 */
  budget_ms: number;
  actual_ms: number;
  model_calls: number;
  dimensions: Record<string, HealthDimension>;
}

/** 健康分直方图的一桶。 */
export interface HealthBucket {
  bucket: string;
  from: number;
  count: number;
}

/** 看板用的健康分聚合（最近 N 次运行按当前口径重算）。 */
export interface HealthMetrics {
  version: string;
  runs: number;
  scored: number;
  avg_score: number | null;
  p50: number | null;
  p90: number | null;
  distribution: HealthBucket[];
  grades: Record<string, number>;
  dimensions: Record<string, { label: string; avg: number | null; samples: number }>;
  deficits: { key: string; label: string; avg: number; loss: number }[];
  weights: Record<string, number>;
  coverage_avg: number | null;
  confidence_avg: number | null;
}

/** 全局指标拆解：工具/权限/缓存/失败聚类。 */
export interface Breakdown {
  version: string;
  outcome: { total: number; success: number; failed: number; running: number };
  tool: { calls: number; failed: number; failure_rate: number | null };
  permission: { decisions: number; denied: number; deny_rate: number | null };
  model: { calls: number; tokens: number; avg_tokens_per_call: number | null };
  tokens: {
    input: number;
    output: number;
    cache_hit: number;
    cache_hit_rate: number | null;
    cache_recorded: boolean;
  };
  failures: { reason: string; count: number }[];
}

export interface Summary {
  total_runs: number;
  success_count: number;
  failed_count: number;
  success_rate: number | null;
  avg_health_score: number | null;
  p95_duration_ms: number | null;
  total_input_tokens: number;
  total_output_tokens: number;
  token_proxy: number;
  tool_call_count: number;
  tool_failed_count: number;
}

export interface TimePoint {
  date: string;
  total: number;
  success: number;
  failed: number;
}

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

/** SSE 流式事件（与后端 event_to_dict + RUN_DONE/RUN_ERROR 对齐）。 */
export interface StreamEvent {
  type: string;
  created_at?: string;
  session_id?: string;
  reply_id?: string;
  block_id?: string;
  delta?: string;
  name?: string;
  model_name?: string;
  tool_call_name?: string;
  tool_call_id?: string;
  state?: string;
  hint?: string;
  source?: string;
  input_tokens?: number;
  output_tokens?: number;
  /** 命中前缀缓存的输入 token 数（模型不支持时缺省）。 */
  cache_input_tokens?: number;
  cache_creation_tokens?: number;
  finished_reason?: string;
  error?: string;
  run_id?: number;
  text?: string;
  /** 输入解析事件字段 */
  stage?: string;
  stage_label?: string;
  summary?: string;
  before?: number;
  after?: number;
  items?: number;
  ms?: number;
  changed?: boolean;
  failed?: number;
  detail?: string;
  degraded?: number;
  /** 待确认调用（REQUIRE_USER_CONFIRM）/ 挂起帧携带 */
  tool_calls?: PendingCall[];
  /** 挂起有效期（RUN_PARKED）；后端未给时为空 */
  expires_at?: string | null;
  /** 上下文预算：触发压缩的 token 阈值（CONTEXT_BUDGET） */
  threshold?: number;
  /** 模型窗口大小（CONTEXT_BUDGET） */
  context_size?: number;
  /** 触发压缩的比例（CONTEXT_BUDGET），阈值 = ratio × 窗口 */
  trigger_ratio?: number;
  /** 压缩检查前后的 token 数（CONTEXT_COMPRESSION_START / END） */
  before_tokens?: number;
  after_tokens?: number;
  /** 这次检查是否真的压了：没到阈值时为 false，前端不画无效记录 */
  compressed?: boolean;
  /** 压缩后摘要的字符数 */
  summary_chars?: number;
}

/** 过程步骤的类别——每类在 UI 上有不同视觉标注。 */
export type ProcessKind =
  | 'lifecycle'
  | 'model'
  | 'text'
  | 'thinking'
  | 'tool'
  | 'control'
  | 'error'
  | 'parse';

/** 输入解析的一个阶段：与后端 INPUT_PARSE_STAGE 事件字段一一对应。 */
export interface ParseStage {
  stage: string;
  label: string;
  status: 'ok' | 'skip' | 'error';
  summary: string;
  before: number;
  after: number;
  items: number;
  ms: number;
  detail: string;
  /** 该阶段是否真的改动了输入——界面只展示这些。 */
  changed: boolean;
  /** 阶段内部失败条数（部分降级）。 */
  failed: number;
}

/** 一次上下文压缩记录：START/END 配对后落到这里。
 *
 * state 三态：running 表示框架正在压；done 表示压完了；skipped 表示
 * 检查过但没到阈值（框架直接 return）。界面只把 done 画进时间线。
 */
export interface CompressionRecord {
  id: string;
  /** 压缩前上下文 token 数（框架按自己的口径估算，不是本轮累计用量） */
  before: number;
  /** 压缩后 token 数；未结束时为 null */
  after: number | null;
  /** 触发阈值，用于显示「103053 / 102400」 */
  threshold: number;
  state: 'running' | 'done' | 'skipped';
  /** 压缩后摘要字符数 */
  summaryChars: number;
  at: string;
  endedAt: string | null;
}

/** 一条过程步骤：由若干同类事件流聚合成，携带展示所需的标注与状态。 */
export interface ProcessEntry {
  id: string;
  kind: ProcessKind;
  label: string;
  detail: string;
  state: 'running' | 'success' | 'error';
  toolCallId?: string;
  /** 首次出现的事件时间（ISO），用于轨迹里估算步骤耗时。 */
  at?: string;
  /** 结束事件时间（ISO），缺省表示仍在执行。 */
  endedAt?: string | null;
  /** 工具入参增量（与返回分开展示，避免正文/参数混在一起）。 */
  args?: string;
  /** 工具返回增量。 */
  result?: string;
  /** 工具原始标识，排查时展示。 */
  toolName?: string;
  /** 该次模型调用的 token 用量（kind === 'model' 时有值）。 */
  tokensIn?: number;
  tokensOut?: number;
  /** 其中命中前缀缓存的输入 token。 */
  cacheTokens?: number;
  /** 模型调用所属回复：同一轮里多次调用靠它分组，避免步骤 id 互相覆盖。 */
  replyId?: string;
  /** 同一回复内的模型调用序号（从 1 开始），用来区分首枪与后续枪。 */
  callIndex?: number;
  /** 输入解析阶段的明细（kind === 'parse' 时有值）。 */
  parseStages?: ParseStage[];
}

/** 一份上传附件：服务端返回元信息，正文留在服务端由解析管线取用。 */
export interface Attachment {
  id: string;
  name: string;
  size: number;
  suffix: string;
  chars: number;
  truncated: boolean;
  /** uploading 是本地乐观态，服务端只会返回 ok / error。 */
  status: 'ok' | 'error' | 'uploading';
  problem?: string;
  /** 前若干字符的预览，够确认「传对了文件」。 */
  preview?: string;
}

/** 输入框里的一份待发送附件：带上本地主键，避免同名文件互相覆盖。 */
export interface PendingAttachment extends Attachment {
  /** 本地唯一键（服务端返回前用临时 id，返回后仍是它）。 */
  key: string;
}

/** 一条会话消息：用户消息或一轮助手回答（助手消息携带该轮过程步骤）。 */
export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  /** 消息时间（ISO）。 */
  at: string;
  steps: ProcessEntry[];
  /** 该条用户消息附带的文件（仅 user 消息）。 */
  attachments?: Attachment[];
  /** 等待用户确认的挂起态（仅 assistant 消息）。 */
  awaiting?: AwaitingState;
  error?: string;
  pending?: boolean;
  /** 用户主动停止且没有任何产出时置位，界面显示「已停止」。 */
  stopped?: boolean;
  modelName?: string;
  inputTokens?: number;
  outputTokens?: number;
  cacheInputTokens?: number;
  runId?: number | null;
  endedAt?: string | null;
}

/** 工具调用的 diff：Edit / Write 的旧串与新串按行拆开。 */
export interface TraceDiff {
  oldLines: string[];
  newLines: string[];
}

/** 步骤计时：三个视图共用这一份，禁止在组件里另算时长。
 *
 * - measured：起止事件都在，最可信；
 * - derived：由阶段自报耗时或落库 span 反推，可信但粒度粗；
 * - estimated：相邻步骤插值，只能看趋势，不参与「最慢 Top N」。
 */
export interface TraceTiming {
  start: number;
  end: number;
  durationMs: number;
  source: 'measured' | 'derived' | 'estimated';
}

/** 轨迹步骤：由消息与过程步骤派生的只读视图。 */
export interface TraceStep {
  id: string;
  kind: 'text' | 'tool' | 'error' | 'parse' | 'model';
  /** 该步骤占用的资源：模型推理 / 工具执行 / 输入解析。 */
  layer: 'model' | 'tool' | 'input' | 'control';
  /** 轮内序号，从 1 开始；Inspector 标题用。 */
  index: number;
  title: string;
  text: string;
  /** 毫秒时间戳。 */
  at: number;
  /** 唯一计时来源；durationMs / measured 是它投影出来的旧字段。 */
  timing: TraceTiming;
  durationMs: number;
  /** 耗时是否不是插值（measured 或 derived）；false 表示按相邻步骤估算。 */
  measured: boolean;
  /** 占全会话实测耗时的比例 0~1；估算值与模型正文恒为 0。 */
  share: number;
  /** 模型的最终正文行：读答案用，不是耗时段，不进时长榜与占比分母。 */
  synthetic?: boolean;
  status?: ProcessEntry['state'];
  toolCallId?: string;
  toolName?: string;
  args?: string;
  result?: string;
  /** 行内展示用的入参摘要（只取关键字段）。 */
  argsSummary?: string;
  /** 行内展示用的返回摘要（首行 + 行数）。 */
  resultSummary?: string;
  diff?: TraceDiff;
  stages?: ParseStage[];
  tokensIn?: number;
  tokensOut?: number;
  cacheTokens?: number;
}

/** 泳道段：某条资源泳道上的一段占用。 */
export interface TraceSpan {
  id: string;
  lane: 'model' | 'tool';
  start: number;
  end: number;
  status: 'ok' | 'error' | 'running';
  label: string;
  stepId: string;
}

/** 两条泳道的占用区间。只画真实占用的时间，空隙就留白，不造「等待」块。 */
export interface TraceLanes {
  model: TraceSpan[];
  tool: TraceSpan[];
}

/** 一组聚合指标：顶部状态栏与空态提示共用。 */
export interface TraceStats {
  /** 会话实测耗时（span 并集）；「占全会话」的分母。 */
  totalMs: number;
  /** 实测耗时：所有步骤真实占用区间的并集，等价于 totalMs。 */
  measuredMs: number;
  /** 墙钟耗时：首轮问题到末轮结束，含用户读消息/走开的空闲。 */
  wallMs: number;
  turns: number;
  steps: number;
  tools: number;
  errors: number;
  /** 模型调用次数：与时长榜里的模型行同源，不再靠猜。 */
  modelCalls: number;
  tokensIn: number;
  tokensOut: number;
  cacheTokens: number;
  /** 输出 token / 模型总耗时（秒）；没有模型耗时为 null。 */
  tokPerSec: number | null;
  /** 缓存命中率 0~1；模型不回传缓存字段时为 null。 */
  cacheRate: number | null;
  /** 每轮第一次模型调用的命中率 0~1：这一枪必然冷读，单独标出才不冤枉缓存。 */
  firstCallRate: number | null;
}

/** 一次运行落库的解析阶段（GET /api/runs/{id}/input-stages）。 */
export interface InputStage {
  seq: number;
  stage: string;
  label: string;
  status: 'ok' | 'skip' | 'error';
  changed: boolean;
  summary: string;
  before: number;
  after: number;
  items: number;
  duration_ms: number;
  detail: string;
  failed: number;
}

/** 轨迹轮次：一条用户输入到其后的助手产出。 */
export interface TraceTurn {
  id: string;
  index: number;
  question: string;
  userText: string;
  at: number;
  startedAt: number;
  endedAt: number;
  steps: number;
  tools: number;
  failed: boolean;
  items: TraceStep[];
  /** 本轮实测耗时（span 并集）。 */
  measuredMs: number;
  /** 本轮墙钟耗时（起止差，含用户空闲）。 */
  wallMs: number;
  /** 该轮输入解析的计数摘要（无解析时缺省）。 */
  parse?: { refs: number; redactions: number; degraded: number; attachments: number };
  /** 该轮用户消息附带的文件数（无附件时缺省）。 */
  attachments?: number;
  /** 模型/工具的真实占用区间，供泳道图使用；正文与思考不在其中。 */
  spans: TraceSpan[];
  tokens: { in: number; out: number; cache: number };
  /** 该轮失败步骤数（异常行 + 工具报错）。 */
  errorCount: number;
  /** 同题的上一轮序号：用来对比「同一个问题这次是变快还是变慢」。 */
  repeatOf?: number;
  /** 本轮第一次模型调用的输入与缓存命中：冷读是必然成本，单独记账。 */
  firstCall?: { in: number; cache?: number };
}

/** 保险箱里的一条凭据（GET /api/secrets）：只给元信息，没有值。 */
export interface SecretMeta {
  name: string;
  hint: string;
  source: string;
  created_at: string;
  last_used_at: string | null;
  uses: number;
}

/** 一次受控明文读回的结果；found=false 表示解不开或不存在。 */
export interface SecretValue {
  name: string;
  value: string;
  found: boolean;
}

/** 服务端运行时的自述（GET /health）。echo = 没配模型凭据，回复只是回显。 */
export interface RuntimeStatus {
  kind: 'echo' | 'agentscope';
  /** 真实运行时用的模型名；echo 时为空串。 */
  model: string;
  /** 需要用户注意的说明，echo 时非空。 */
  notice: string;
}

/** 存活探针的响应：runtime 与 tools 都是「能跑但不能干活」的降级信号。 */
export interface Health {
  status: string;
  runtime?: RuntimeStatus;
  tools?: Record<string, { available: boolean; binary: string }>;
}

// --- 模型参数（SPEC-0001）-----------------------------------------------

/** 一个参数值是从哪一层来的：会话覆盖 > 部署默认 > yaml > 环境变量 > 框架默认。 */
export type ModelSource = 'session' | 'stored' | 'yaml' | 'env' | 'framework-default';

/** 生效参数集：键与后端 `effective` 一一对应。 */
export interface ModelValues {
  model: string;
  context_size: number;
  temperature: number | null;
  max_tokens: number | null;
  top_p: number | null;
  reasoning_effort: string | null;
  thinking_enable: boolean | null;
  parallel_tool_calls: boolean;
  max_iters: number;
  model_max_retries: number;
  agent_max_retries: number;
  trigger_ratio: number;
  reserve_ratio: number;
  context_buffer_ratio: number;
}

/** 一个字段的取值区间：滑杆上下限与越界提示都来自它，前端不自己写死数字。 */
export interface ModelBound {
  kind: 'int' | 'float' | 'str' | 'bool' | 'enum';
  nullable: boolean;
  min?: number;
  max?: number;
  step?: number;
  choices?: string[];
}

/** 后端算好的推算值：前端不再乘一遍，口径只能有一处。 */
export interface ModelDerived {
  compression_threshold: number;
  worst_case_attempts: number;
  /** 上下文窗口还没被部署者确认过：面板要显示醒目提示。 */
  context_uncalibrated: boolean;
}

/** GET|PATCH /api/model-config 的响应。 */
export interface ModelConfig {
  effective: ModelValues;
  sources: Record<string, ModelSource>;
  bounds: Record<string, ModelBound>;
  editable: string[];
  /** 只读项 → 「在哪改」的说明；面板里禁用但可见。 */
  readonly: Record<string, string>;
  derived: ModelDerived;
  warnings: string[];
  version: number;
  /** 该会话的覆盖项（GET 带 session 时返回）。 */
  overrides?: Partial<ModelValues>;
  /** 改部署默认后的传播结果：多少会话已生效、多少等当前轮结束。 */
  affected?: { applied: number; deferred: number };
}

/** 会话级改动的响应：applied=false 表示没有活跃实例，下一轮才装配。 */
export interface SessionModelConfig extends ModelConfig {
  overrides: Partial<ModelValues>;
  applied?: boolean;
  effective_from?: string;
}
