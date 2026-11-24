/** 与后端 Pydantic 响应对齐的前端类型。 */

/** 权限档位标识：与 governance/profiles.py 的预设名一一对应。 */
export type PermissionKey = 'readonly' | 'workspace' | 'cautious' | 'unattended' | 'container';

/** 能力范围：决定工具集与规则生效下限。 */
export type PermissionScope = 'readonly' | 'workspace' | 'container';

/** 打扰程度：只决定兜底动作（框架 mode），不改工具集。 */
export type PermissionApprovals = 'always' | 'on-risk' | 'never';

/** 一条已沉淀的规则（config/permissions.local.yaml）。 */
export interface PermissionRule {
  tool: string;
  pattern: string | null;
  behavior: 'allow' | 'ask' | 'deny';
  source: string;
  scope: PermissionScope;
  /** 生成它的命令原文，撤销时知道当初批准了什么。 */
  origin: string;
  created_at: string;
}

/** 一个可选档位的展示信息（GET /api/permissions）。 */
export interface PermissionMeta {
  key: PermissionKey;
  label: string;
  note: string;
  scope: PermissionScope;
  approvals: PermissionApprovals;
  /** `scope+approvals` 组合串，与后端重建判据一致。 */
  combo: string;
  tools: string[];
  /** 是否挂了 Write/Edit —— 界面据此显示「可写」标记。 */
  write: boolean;
  /** 以下三行由后端拿真实引擎跑取样命令得出，不是手写文案。 */
  auto_allow: string[];
  will_ask: string[];
  will_deny: string[];
}

/** 同一前缀被反复询问的计数：前端据此提示「以后都这样」。 */
export interface PermissionFatigue {
  tool: string;
  prefix: string;
  count: number;
}

export interface PermissionTrust {
  mode: 'permissive' | 'strict';
  workdirs: string[];
}

export interface PermissionCatalog {
  default: PermissionKey;
  available: PermissionMeta[];
  scopes: PermissionScope[];
  approvals: PermissionApprovals[];
  rules: PermissionRule[];
  rules_file: string;
  trust: PermissionTrust;
  fatigue: PermissionFatigue[];
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

/**
 * 确认卡片的三个按钮：这一次 / 本会话 / 以后都这样。
 *
 * 前两者写会话规则，第三者额外落盘（`config/permissions.local.yaml`）。
 * 与 `ConfirmScope` 分开：作用域管「规则多宽」，按钮管「记多久」。
 */
export type ConfirmChoice = 'once' | 'session' | 'forever' | 'reject';

export interface ConfirmDecision {
  tool_call_id: string;
  confirmed: boolean;
  scope: ConfirmScope;
  /** 「以后都这样」为真：写进部署级规则文件，跨会话保留。 */
  persist?: boolean;
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

export interface RunHealth {
  run_id: number;
  score: number;
  dimensions: {
    execution: number;
    efficiency: number;
    quality: number | null;
    safety: number;
  };
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
  hard_failures: { safety: number; forbidden_tool: number; format: number };
  judge: Record<string, number> | null;
  trajectory_score: number | null;
  p95_ms: number | null;
  tokens_total: number;
  cost_total: number;
  baseline_id: number | null;
  verdict: 'pass' | 'warn' | 'fail' | 'insufficient' | 'running';
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
}

export interface EvalBatchDetail {
  batch: EvalBatch;
  tags: Record<string, { n: number; passed: number }>;
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
  /** 这一轮用的权限档位（仅 user 消息），事后回看才知道当时放到了哪。 */
  permission?: PermissionKey;
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
