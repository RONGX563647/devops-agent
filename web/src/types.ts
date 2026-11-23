/** 与后端 Pydantic 响应对齐的前端类型。 */

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
}

/** 过程步骤的类别——每类在 UI 上有不同视觉标注。 */
export type ProcessKind =
  | 'lifecycle'
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
  /** 输入解析阶段的明细（kind === 'parse' 时有值）。 */
  parseStages?: ParseStage[];
}

/** 一条会话消息：用户消息或一轮助手回答（助手消息携带该轮过程步骤）。 */
export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  /** 消息时间（ISO）。 */
  at: string;
  steps: ProcessEntry[];
  error?: string;
  pending?: boolean;
  /** 用户主动停止且没有任何产出时置位，界面显示「已停止」。 */
  stopped?: boolean;
  modelName?: string;
  inputTokens?: number;
  outputTokens?: number;
  runId?: number | null;
  endedAt?: string | null;
}

/** 轨迹步骤：由消息与过程步骤派生的只读视图。 */
export interface TraceStep {
  id: string;
  kind: 'text' | 'tool' | 'error' | 'parse';
  title: string;
  text: string;
  /** 毫秒时间戳。 */
  at: number;
  durationMs: number;
  status?: ProcessEntry['state'];
  toolCallId?: string;
  toolName?: string;
  args?: string;
  result?: string;
  /** 解析步骤的阶段明细。 */
  stages?: ParseStage[];
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
  /** 该轮输入解析的计数摘要（无解析时缺省）。 */
  parse?: { refs: number; redactions: number; degraded: number };
}
