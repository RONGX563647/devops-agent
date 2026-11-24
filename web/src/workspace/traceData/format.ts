/** 轨迹视图的文本与 diff 格式化。 */

import type { ProcessEntry, TraceDiff } from '../../types';

/** Edit / Write 的 diff 单侧最多展示的行数。 */
const MAX_DIFF_LINES = 14;

export function toMs(value?: string | null): number {
  if (!value) return 0;
  const ms = new Date(value).getTime();
  return Number.isNaN(ms) ? 0 : ms;
}

/** 区间并集长度：模型与工具可能嵌套/重叠，直接求和会把同一段时间算两次。 */
export function unionLength(intervals: Array<[number, number]>): number {
  let total = 0;
  let curStart: number | null = null;
  let curEnd = 0;
  for (const [start, end] of [...intervals].sort((a, b) => a[0] - b[0])) {
    if (end <= start) continue;
    if (curStart === null) {
      curStart = start;
      curEnd = end;
    } else if (start <= curEnd) {
      curEnd = Math.max(curEnd, end);
    } else {
      total += curEnd - curStart;
      curStart = start;
      curEnd = end;
    }
  }
  if (curStart !== null) total += curEnd - curStart;
  return total;
}

export function shortQuestion(text: string): string {
  return text.replace(/\s+/g, ' ').trim().slice(0, 42);
}

/** 把任意文本压成一行，超出截断。 */
export function compact(text: string, max: number = 56): string {
  const line = text.replace(/\s+/g, ' ').trim();
  return line.length > max ? `${line.slice(0, max)}…` : line;
}

/** 入参摘要只取前两个标量字段：看的是「它被要求做什么」，不是参数全文。 */
export function summarizeArgs(args?: string): string {
  const raw = (args ?? '').trim();
  if (!raw) return '';
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      const entries = Object.entries(parsed as Record<string, unknown>).filter(
        ([, value]) => value !== null && typeof value !== 'object',
      );
      if (entries.length > 0) {
        return entries
          .slice(0, 2)
          .map(([key, value]) => `${key}: ${compact(String(value), 40)}`)
          .join(' · ');
      }
    }
  } catch {
    // 不是 JSON（旧数据或纯文本入参）：按原文压一行
  }
  return compact(raw, 64);
}

/** 返回摘要：首行 + 行数，够判断「有没有东西回来」。 */
export function summarizeResult(
  result?: string,
  status?: ProcessEntry['state'],
): string {
  const raw = (result ?? '').trim();
  if (!raw) return '';
  const lines = raw.split('\n');
  const head = compact(lines[0], 56);
  const suffix = lines.length > 1 ? ` · ${lines.length} 行` : '';
  const failed = status === 'error' ? '失败 · ' : '';
  return `${failed}${head}${suffix}`;
}

/** 只有写入类工具才有「旧串 → 新串」可比。 */
const DIFF_TOOLS = /^(edit|write|multiedit|notebookedit)$/i;

/** 按顺序取第一个字符串字段：不同版本的写入工具字段名不一致。 */
function pickString(record: Record<string, unknown>, keys: string[]): string | undefined {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === 'string') return value;
  }
  return undefined;
}

/** Edit / Write 的入参里带旧串与新串，能直接画成增删行。 */
export function buildDiff(toolName: string | undefined, args?: string): TraceDiff | undefined {
  if (!toolName || !DIFF_TOOLS.test(toolName)) return undefined;
  let parsed: unknown;
  try {
    parsed = JSON.parse((args ?? '').trim());
  } catch {
    return undefined;
  }
  if (!parsed || typeof parsed !== 'object') return undefined;
  const record = parsed as Record<string, unknown>;
  const before = pickString(record, ['old_string', 'old_str', 'content']);
  const after = pickString(record, ['new_string', 'new_str', 'new_content']);
  if (!before || !after) return undefined;
  return {
    oldLines: before.split('\n').slice(0, MAX_DIFF_LINES),
    newLines: after.split('\n').slice(0, MAX_DIFF_LINES),
  };
}
