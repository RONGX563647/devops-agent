/** 运行记录、SSE 事件、过程步骤与轨迹视图：与后端 Pydantic 响应对齐的前端类型。 */

import type { AwaitingState, PendingCall } from './permission';

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
  /** 本轮网络获取决策（JSON：net / prefetch / fallback）；空串 = 老记录未记录。 */
  web_access?: string;
  /** 失败原因原文；空串 = 成功或老记录。 */
  error?: string;
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
  type: 'model' | 'tool' | 'memory' | 'permission';
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
  /** 首 token 等待（毫秒）；0 表示没采到，不能按 0ms 计入均值。 */
  ttft_ms?: number;
  /** run 内单调序号与父 span：泳道与列表共用同一顺序。 */
  seq?: number;
  parent_id?: number | null;
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
  /** 记忆事件（SPEC-0013）：层、状态与结构化明细 */
  layer?: string;
  reason?: string;
  memory?: Record<string, unknown>;
  hits?: number;
  ids?: number[];
  scores?: number[];
  injected_chars?: number;
  dropped_tool_results?: number;
  dropped_turns?: number;
  deduped_reads?: number;
  reclaimed_chars?: number;
  version?: number;
  stored?: boolean;
  deduped?: boolean;
  budget?: number;
  latency_ms?: number;
  zones?: Record<string, unknown>;
  estimated?: boolean;
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
  | 'parse'
  | 'memory';

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

/** 一轮的网络获取决策：`net` 是否授予检索，`fallback` 是缺口说明。 */
export interface WebAccessInfo {
  net: boolean | null;
  fallback: string;
  prefetch: string;
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
  /** 该轮的网络获取决策（历史回放时有；实时流没有 run 行，故缺省）。 */
  webAccess?: WebAccessInfo;
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
  /** 会话内唯一（已带消息 id 前缀）；原始 step.id 只在单条消息内唯一。 */
  id: string;
  kind: 'text' | 'tool' | 'error' | 'parse' | 'model' | 'memory';
  /** 该步骤占用的资源：模型推理 / 工具执行 / 输入解析 / 记忆动作。 */
  layer: 'model' | 'tool' | 'input' | 'control' | 'memory';
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
  /** 会话内唯一，直接用作 React key；buildLanes 保证同泳道不重复。 */
  id: string;
  lane: 'model' | 'tool' | 'memory';
  start: number;
  end: number;
  status: 'ok' | 'error' | 'running';
  label: string;
  /** 指向 TraceStep.id（同一份会话内唯一 id），点选与高亮靠它对上。 */
  stepId: string;
}

/** 三条泳道的占用区间。只画真实占用的时间，空隙就留白，不造「等待」块。 */
export interface TraceLanes {
  model: TraceSpan[];
  tool: TraceSpan[];
  memory: TraceSpan[];
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
  /** 本轮网络获取决策；未记录时缺省（不能当成「没联网」）。 */
  webAccess?: WebAccessInfo;
}
