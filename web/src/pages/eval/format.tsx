/** 评测批次的展示文案与数字格式化。 */

import type { EvalBatch, EvalCompareMetric } from '../../types';
import { HARD_KIND_KEYS, HARD_KIND_META } from '../../types';

export const PAGE_SIZE = 50;
export const CLI_HINT =
  'python -m devops_agent.eval run --suite eval/suites/devops-core.v2.jsonl --out workspace/evals/<batch>';

export const VERDICT_LABEL: Record<string, string> = {
  pass: '通过',
  warn: '警告',
  fail: '失败',
  insufficient: '样本不足',
  running: '运行中',
  not_comparable: '不可比',
};

/** 门禁三态：`insufficient` 不是「未达」，两者必须分开显示。 */
export const GATE_STATE_LABEL: Record<string, string> = {
  pass: '通过',
  fail: '未达',
  insufficient: '样本不足',
};

export const JUDGE_LABEL: Record<string, string> = {
  correctness: '正确性',
  coherence: '连贯性',
  relevance: '相关性',
  objectivity: '客观性',
  clarity: '清晰度',
  conciseness: '简洁性',
};

/** 门禁「不能判」的成因：三种样本不足的处置完全不同，不能只写一句「样本不足」。 */
export const GATE_REASON_LABEL: Record<string, string> = {
  n_below_gate: '题库样本不足',
  no_samples: '裁判未运行',
  samples_below_min: '裁判样本不足',
};

/** 硬失败总数：7 类全算。只加 3 类会让其余四类失败在界面上消失。 */
export function hardTotal(batch: EvalBatch): number {
  return Object.values(batch.hard_failures ?? {}).reduce((sum, value) => sum + (value ?? 0), 0);
}

/** 按严重度列出有计数的分类，用于横幅文案。 */
export function describeKinds(batch: EvalBatch, severity: 'blocking' | 'scoring'): string {
  const parts = HARD_KIND_KEYS.filter(
    (key) => HARD_KIND_META[key].severity === severity && (batch.hard_failures?.[key] ?? 0) > 0,
  ).map((key) => `${HARD_KIND_META[key].label} ${batch.hard_failures[key]}`);
  return parts.length > 0 ? parts.join(' / ') : '—';
}

export function formatPercent(value: number | null): string | null {
  return value === null ? null : `${(value * 100).toFixed(1)}%`;
}

export function formatScore(value: number | null): string | null {
  return value === null ? null : value.toFixed(2);
}

export function formatNumber(value: number | null): string {
  if (value === null) return '—';
  if (Number.isInteger(value)) return String(value);
  return value.toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
}

export function formatDelta(value: number | null): string {
  if (value === null) return '—';
  const text = formatNumber(value);
  return value > 0 ? `+${text}` : text;
}

/** better=lower 时 delta 为负才是正向，颜色方向据此反转。 */
export function deltaTone(metric: EvalCompareMetric): 'good' | 'bad' | 'flat' {
  if (metric.delta === null || metric.delta === 0) return 'flat';
  const improved = metric.better === 'higher' ? metric.delta > 0 : metric.delta < 0;
  return improved ? 'good' : 'bad';
}
