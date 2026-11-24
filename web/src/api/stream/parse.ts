/** SSE 帧解析、上下文提示与输入解析阶段摘要。 */

import type { ParseStage, StreamEvent } from '../../types';

// 手写 SSE 帧解析而非引入 fetch-event-source：本应用只需 POST + data 帧 + abort，
// 按 SSE 规范兼容 CRLF 与 \n 两种换行，并支持多行 data 拼接；注释行（心跳）自动忽略。
export function parseFrame(frame: string): StreamEvent | null {
  const normalized = frame.replace(/\r\n/g, '\n');
  const dataLines = normalized
    .split('\n')
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).trimStart());
  if (dataLines.length === 0) return null;
  try {
    return JSON.parse(dataLines.join('\n')) as StreamEvent;
  } catch {
    return null;
  }
}

/**
 * agentscope 在接近阈值时会注入一句上下文长度提示（包在 <system-reminder> 里）：
 * 「Your current context contains 103053 tokens. When reaching 102400 tokens,
 * your context will be compressed.」
 *
 * 这是首次模型调用返回之前唯一能点亮圆环的精确数字，所以先抓数、再决定要不要展示。
 */
const CONTEXT_HINT = /contains (\d+) tokens[\s\S]*?reaching (\d+) tokens/i;

export function parseContextHint(hint: string): { tokens: number; threshold: number } | null {
  const matched = CONTEXT_HINT.exec(hint);
  if (!matched) return null;
  return { tokens: Number(matched[1]), threshold: Number(matched[2]) };
}

/** 输入解析按 block_id 归并成一条父步骤，各阶段挂在 parseStages 上。 */
export function parseStepId(event: StreamEvent): string {
  return `parse:${event.block_id ?? 'input'}`;
}

/** 事件里的 state 只有 error / skip 需要区分，其余一律按成功处理。 */
const PARSE_STATUS: Record<string, ParseStage['status']> = { error: 'error', skip: 'skip' };
/** 阶段里的计数字段，缺省一律 0——统一处理免得逐字段写 ??。 */
const STAGE_NUMBERS = ['before', 'after', 'items', 'ms', 'failed'] as const;

export function toParseStage(event: StreamEvent): ParseStage {
  const stage = event.stage || 'stage';
  const parsed: ParseStage = {
    stage,
    label: event.stage_label ?? stage,
    status: PARSE_STATUS[event.state ?? ''] ?? 'ok',
    summary: event.summary ?? '',
    detail: event.detail ?? '',
    changed: event.changed ?? false,
    before: 0,
    after: 0,
    items: 0,
    ms: 0,
    failed: 0,
  };
  for (const key of STAGE_NUMBERS) {
    parsed[key] = event[key] ?? 0;
  }
  return parsed;
}

/** 摘要里按顺序展示的计数项：阶段名 + 中文单位，值为 0 时跳过。 */
const SUMMARY_COUNTERS: Array<[stage: string, unit: string]> = [
  ['attach', '附件'],
  ['detect', '引用'],
  ['redact', '脱敏'],
];

/** 该阶段是否真的动了输入：有改动、出错、或内部有降级。 */
function isUseful(stage: ParseStage): boolean {
  return stage.changed || stage.status === 'error' || stage.failed > 0;
}

function countItems(stages: ParseStage[], name: string): number {
  return stages.find((stage) => stage.stage === name)?.items ?? 0;
}

/** 父行摘要只统计真正生效的阶段，避免「6 阶段」里 5 个是无需处理。 */
export function parseSummary(stages: ParseStage[]): string {
  const useful = stages.filter(isUseful).length;
  const parts = [`${useful}/${stages.length} 阶段生效`];
  for (const [name, unit] of SUMMARY_COUNTERS) {
    const count = countItems(stages, name);
    if (count) parts.push(`${count} ${unit}`);
  }
  const failed = stages.reduce((sum, stage) => sum + stage.failed, 0);
  if (failed) parts.push(`${failed} 处降级`);
  return parts.join(' · ');
}
