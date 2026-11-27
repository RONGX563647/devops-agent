/** 记忆效果评测（SPEC-0014）的接口类型：与后端 summary_json 对齐。 */

export interface MemoryTrackRow {
  total: number;
  passed: number;
  tsr: number | null;
  ci: [number, number] | null;
  probe_acc: number | null;
  contradiction_rate: number | null;
  recall_at_5: number | null;
  precision_at_5: number | null;
  mrr: number | null;
  fact_retention: number | null;
  constraint_retention: number | null;
  artifact_retention: number | null;
  junk_rate: number | null;
  junk_injected: number;
  leak_count: number;
  redaction_violation: number;
}

export interface MemoryGain {
  n: number;
  full_tsr: number | null;
  off_tsr: number | null;
  gain_pp: number | null;
  ci: [number, number] | null;
  p: number | null;
}

export interface MemoryCause {
  code: string;
  message: string;
  actual?: number | null;
  threshold?: number | null;
}

export interface MemorySummary {
  plan: string;
  baseline: string;
  primary: string;
  verdict: string;
  suite_sha: string;
  plan_sha: string;
  kpi: {
    runs: number;
    cases: number;
    passed: number;
    pass_rate: number | null;
  };
  tracks: Record<string, MemoryTrackRow>;
  gains: Record<string, MemoryGain>;
  harm: { n: number; harm_rate: number | null };
  red_lines: {
    leak_count: number;
    redaction_violation: number;
    constraint_retention: number | null;
  };
  cost: {
    token_overhead_p50: number | null;
    token_overhead_p95: number | null;
    latency_p95_ms: number | null;
    memory_bytes_total: number;
    compression_ratio_avg: number | null;
  };
  causes: MemoryCause[];
}

export interface MemoryBatch {
  id: number;
  plan_name: string;
  verdict: string;
  started_at: string;
  finished_at: string;
  summary: MemorySummary;
}

export interface MemoryBatchList {
  items: MemoryBatch[];
  total: number;
  limit: number;
  offset: number;
}

export interface MemoryCaseRow {
  id: number;
  case_id: string;
  track: string;
  tier: string;
  variant: string;
  trial: number;
  passed: boolean;
  probe_total: number;
  probe_correct: number;
  latency_ms: number;
  tokens: number;
  metrics: Record<string, number>;
}

export interface MemoryCaseList {
  items: MemoryCaseRow[];
  total: number;
  limit: number;
  offset: number;
}
