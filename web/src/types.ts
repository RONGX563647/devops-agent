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
}

/** 过程步骤的类别——每类在 UI 上有不同视觉标注。 */
export type ProcessKind =
  | 'lifecycle'
  | 'text'
  | 'thinking'
  | 'tool'
  | 'control'
  | 'error';

/** 一条过程步骤：由若干同类事件流聚合成，携带展示所需的标注与状态。 */
export interface ProcessEntry {
  id: string;
  kind: ProcessKind;
  label: string;
  detail: string;
  state: 'running' | 'success' | 'error';
  toolCallId?: string;
}
