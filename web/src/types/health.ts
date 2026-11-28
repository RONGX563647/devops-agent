import type { ConfigHealth } from './config';

/** 健康分、全局指标与运行时自述：与后端 Pydantic 响应对齐的前端类型。 */

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
  // kind：run = 整轮失败；tool/model/permission = 逐次调用的失败
  failures: { reason: string; count: number; kind: string; name: string }[];
}

/** 一组耗时样本的均值 / 分位；samples 是真正有记录的条数。 */
export interface TimingBlock {
  avg: number | null;
  p95: number | null;
  samples: number;
}

/** 一个模型的用量、耗时与成本（cost 为 null = 没配单价，不计入总额）。 */
export interface ModelUsage {
  name: string;
  calls: number;
  errors: number;
  input_tokens: number;
  output_tokens: number;
  cache_input_tokens: number;
  avg_ms: number | null;
  ttft_samples: number;
  avg_ttft_ms: number | null;
  cost: number | null;
  priced: boolean;
}

/** 成本 / 性能看板（GET /api/metrics/cost）。 */
export interface CostMetrics {
  currency: string;
  unit_tokens: number;
  priced_models: number;
  unpriced_models: number;
  total_cost: number | null;
  models: ModelUsage[];
  ttft: TimingBlock;
  model_ms: TimingBlock;
  tool_ms: TimingBlock;
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
  /** 配置库读写状态（读降级、写拒绝）；`mode` 可能是 invalid（环境变量拼错）。 */
  config?: ConfigHealth;
}

/**
 * 联网检索工具的就绪状态（GET /api/tools/search）。
 *
 * 按钮能不能点必须由服务端说：前端猜不出「有没有配搜索后端」，
 * 而给一个点了没反应的开关是最难排查的一类缺陷。
 */
export interface SearchToolStatus {
  configured: boolean;
  /** tool = 工具化（默认）；provider = 旧请求体模式；off = 禁网 */
  mode: 'tool' | 'provider' | 'off';
  /** http / provider；未配置时为空串 */
  backend: string;
  max_calls: number;
  timeout_s: number;
  /** 不可用原因，直接展示给用户 */
  reason?: string;
}
