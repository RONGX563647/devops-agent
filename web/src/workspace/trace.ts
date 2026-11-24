/** 把会话消息派生为轨迹视图：轮次 + 步骤 + 真实耗时 + 三条资源泳道 + 聚合指标。
 *
 * 三条视图（时长/调用/轮次）读的是同一份 TraceStep，计时也只有一个来源：
 * - measured：起止事件都在；
 * - derived：阶段自报耗时（解析）或落库 span 反推；
 * - estimated：相邻步骤插值，只能看趋势，默认沉底且不参与「最慢 Top N」。
 * 会话级同时给两个口径：实测（span 并集）与墙钟（起止差），差值就是用户空闲。
 */
import type {
  ChatMessage,
  ParseStage,
  ProcessEntry,
  TraceDiff,
  TraceLanes,
  TraceSpan,
  TraceStats,
  TraceStep,
  TraceTiming,
  TraceTurn,
} from '../types';

/** 步骤耗时上限：一轮卡住时不要把整段等待算进最后一步。 */
const MAX_STEP_MS = 10 * 60_000;
/** Edit / Write 的 diff 单侧最多展示的行数。 */
const MAX_DIFF_LINES = 14;

function toMs(value?: string | null): number {
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

function shortQuestion(text: string): string {
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
export function summarizeResult(result?: string, status?: ProcessEntry['state']): string {
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

function layerOf(step: ProcessEntry): TraceStep['layer'] {
  if (step.kind === 'tool') return 'tool';
  if (step.kind === 'parse') return 'input';
  if (step.kind === 'error' || step.kind === 'control') return 'control';
  return 'model';
}

function kindOf(step: ProcessEntry): TraceStep['kind'] {
  if (step.kind === 'tool') return 'tool';
  if (step.kind === 'parse') return 'parse';
  if (step.kind === 'error') return 'error';
  if (step.kind === 'control') return step.state === 'error' ? 'error' : 'text';
  if (step.kind === 'model') return 'model';
  return 'text';
}

/** 行首标题只留「是谁」：徽标已经说明了类别，标题里不再重复「模型调用 ·」这类前缀。 */
function stepTitle(step: ProcessEntry): string {
  if (step.kind === 'thinking') return '思考';
  if (step.kind === 'model') return step.label.replace(/^模型调用\s*·\s*/, '') || '模型';
  if (step.kind === 'tool') return step.toolName ?? step.label.replace(/^调用工具\s*·\s*/, '');
  return step.label;
}

/** 唯一的计时口径：有起止事件就是 measured，只有阶段自报就是 derived，都没有先留空。 */
function timingOf(at: number, ended: number, selfMs: number): TraceTiming {
  if (ended > at) {
    const durationMs = Math.min(MAX_STEP_MS, ended - at);
    return { start: at, end: at + durationMs, durationMs, source: 'measured' };
  }
  if (selfMs > 0) {
    const durationMs = Math.min(MAX_STEP_MS, selfMs);
    return { start: at, end: at + durationMs, durationMs, source: 'derived' };
  }
  return { start: at, end: at, durationMs: 0, source: 'estimated' };
}

/** 过程步骤 → 轨迹步骤；只有 lifecycle（运行日志）不是耗时步骤。
 *
 * 模型调用必须留在列表里：泳道里画着它占用的长条，列表里却没有对应行，
 * 「时长」页就永远答不了「这轮到底慢在哪」。
 */
function stepToTrace(step: ProcessEntry, index: number): TraceStep | null {
  if (step.kind === 'lifecycle') return null;
  const toolName = step.toolName;
  const at = toMs(step.at);
  const ended = toMs(step.endedAt);
  // 解析阶段没有配对的起止事件，耗时取各阶段自报的 ms（derived）
  const stagesMs = (step.parseStages ?? []).reduce((sum, stage) => sum + stage.ms, 0);
  const timing = timingOf(at, ended, stagesMs);
  return {
    id: step.id,
    kind: kindOf(step),
    layer: layerOf(step),
    index,
    title: stepTitle(step),
    text: step.detail,
    at,
    timing,
    durationMs: timing.durationMs,
    measured: timing.source !== 'estimated',
    share: 0,
    status: step.state,
    toolCallId: step.toolCallId,
    toolName,
    args: step.args,
    result: step.result,
    argsSummary: summarizeArgs(step.args),
    resultSummary: summarizeResult(step.result, step.state),
    diff: buildDiff(toolName, step.args),
    stages: step.parseStages,
    tokensIn: step.tokensIn,
    tokensOut: step.tokensOut,
    cacheTokens: step.cacheTokens,
  };
}

/** 把解析阶段折成轮级计数：附件数、引用条数、脱敏处数、降级处数。 */
function accumulateParse(turn: TraceTurn, stages: ParseStage[]): void {
  if (stages.length === 0) return;
  const items = (name: string): number =>
    stages.find((stage) => stage.stage === name)?.items ?? 0;
  const degraded = stages.reduce(
    (sum, stage) => sum + Math.max(stage.failed, stage.status === 'error' ? 1 : 0),
    0,
  );
  turn.parse = {
    attachments: items('attach'),
    refs: items('detect'),
    redactions: items('redact'),
    degraded,
  };
}

function spanStatus(step: ProcessEntry): TraceSpan['status'] {
  if (step.state === 'error') return 'error';
  return step.state === 'running' ? 'running' : 'ok';
}

/** 模型调用进入泳道：同一 reply 的 START/END 已在事件折叠时配对。 */
function modelSpan(step: ProcessEntry, at: number): TraceSpan | null {
  const end = toMs(step.endedAt);
  if (!at || !end || end < at) return null;
  return {
    id: `span:${step.id}`,
    lane: 'model',
    start: at,
    end,
    status: spanStatus(step),
    label: step.kind === 'thinking' ? '思考' : step.label,
    stepId: step.id,
  };
}

/** 工具调用进入泳道；缺结束事件时画成 1ms 的点，仍在跑的用 running 态。 */
function toolSpan(step: ProcessEntry, at: number): TraceSpan {
  return {
    id: `span:${step.id}`,
    lane: 'tool',
    start: at,
    end: Math.max(toMs(step.endedAt), at + 1),
    status: spanStatus(step),
    label: step.toolName ?? step.label.replace(/^调用工具\s*·\s*/, ''),
    stepId: step.id,
  };
}

function pushFailure(turn: TraceTurn, message: ChatMessage, at: number): void {
  turn.failed = true;
  turn.items.push({
    id: `${message.id}:error`,
    kind: 'error',
    layer: 'control',
    index: 0,
    title: '运行失败',
    text: message.error ?? '',
    at,
    timing: { start: at, end: at, durationMs: 0, source: 'derived' },
    durationMs: 0,
    measured: true,
    share: 0,
    synthetic: true,
  });
}

/** 单条过程步骤进轮次：泳道区间、解析计数、列表条目各归各的。 */
/** 步骤 → 泳道区间：模型（含思考）走模型泳道，工具走工具泳道，其余不画。 */
function spanFor(step: ProcessEntry, at: number): TraceSpan | null {
  if (step.kind === 'model' || step.kind === 'thinking') return modelSpan(step, at);
  if (step.kind === 'tool') return toolSpan(step, at);
  return null;
}

/** 记下本轮第一次模型调用的输入与命中：它等于「前缀 + 历史 + 输入」。 */
function noteFirstCall(turn: TraceTurn, step: ProcessEntry): void {
  if (step.kind !== 'model' || (step.tokensIn ?? 0) <= 0 || turn.firstCall) return;
  turn.firstCall = { in: step.tokensIn ?? 0, cache: step.cacheTokens };
}

function absorbStep(turn: TraceTurn, step: ProcessEntry): void {
  const span = spanFor(step, toMs(step.at));
  if (span) turn.spans.push(span);
  noteFirstCall(turn, step);
  if (step.kind === 'parse' && step.parseStages) accumulateParse(turn, step.parseStages);
  const trace = stepToTrace(step, 0);
  if (!trace) return;
  // 解析阶段自己出错不把整轮判失败：材料取不到只降级，不中断对话
  if (trace.kind === 'error') turn.failed = true;
  turn.items.push(trace);
}

/** 把一条助手消息并入当前轮：泳道区间、异常、过程步骤与正文。 */
function appendAssistant(turn: TraceTurn, message: ChatMessage): void {
  const at = toMs(message.at);
  const endedAt = toMs(message.endedAt) || at;
  turn.endedAt = Math.max(turn.endedAt, endedAt, at);
  turn.tokens.in += message.inputTokens ?? 0;
  turn.tokens.out += message.outputTokens ?? 0;
  turn.tokens.cache += message.cacheInputTokens ?? 0;

  if (message.error) pushFailure(turn, message, endedAt);
  for (const step of message.steps) absorbStep(turn, step);
  if (message.text) {
    turn.items.push({
      id: `${message.id}:text`,
      kind: 'text',
      layer: 'model',
      index: 0,
      title: '模型输出',
      text: message.text,
      at: endedAt,
      // 最终正文是在最后一次模型调用里产出的，再记一次时长就把模型时间算了两遍
      timing: { start: endedAt, end: endedAt, durationMs: 0, source: 'derived' },
      durationMs: 0,
      measured: true,
      share: 0,
      synthetic: true,
    });
  }
  if (message.stopped) turn.failed = true;
}

/** 排序、编序号、给缺真实计时的步骤补一个估算值（并标记 source=estimated）。 */
function finalizeTurn(turn: TraceTurn): void {
  turn.items.sort((a, b) => a.at - b.at);
  turn.items.forEach((item, i) => {
    item.index = i + 1;
  });
  for (let i = 0; i < turn.items.length; i += 1) {
    const item = turn.items[i];
    // 正文/异常这类合成行本来就没有耗时，插值只会造出一条假的「最慢」
    if (item.synthetic || item.timing.source !== 'estimated' || item.timing.durationMs > 0) continue;
    const nextAt = turn.items[i + 1]?.at ?? turn.endedAt;
    const durationMs = Math.min(MAX_STEP_MS, Math.max(0, nextAt - item.at));
    item.timing = { start: item.at, end: item.at + durationMs, durationMs, source: 'estimated' };
    item.durationMs = durationMs;
  }
  turn.steps = turn.items.length;
  turn.tools = turn.items.filter((item) => item.kind === 'tool').length;
  turn.errorCount = turn.items.filter(
    (item) => item.kind === 'error' || item.status === 'error',
  ).length;
  turn.wallMs = Math.max(0, turn.endedAt - turn.startedAt);
  turn.measuredMs = unionLength(turn.spans.map((span) => [span.start, span.end]));
}

/** 占比分母：全会话非估算、非合成步骤的耗时之和。
 *
 * 估算值不进分母，否则「插值出来的 30 秒」会把真实步骤的占比压小。
 */
function applyShares(turns: TraceTurn[]): void {
  const total = turns.reduce(
    (sum, turn) =>
      sum +
      turn.items.reduce(
        (acc, item) =>
          acc + (item.synthetic || item.timing.source === 'estimated' ? 0 : item.durationMs),
        0,
      ),
    0,
  );
  if (total <= 0) return;
  for (const turn of turns) {
    for (const item of turn.items) {
      item.share =
        item.synthetic || item.timing.source === 'estimated' ? 0 : item.durationMs / total;
    }
  }
}

/**
 * 一轮 = 一条 user 消息 → 下一条 user 消息之前的全部内容。
 *
 * 返回的每个步骤都带轮内序号；耗时优先取真实结束事件，缺失才按相邻步骤估算。
 */
export function turnsFromMessages(messages: ChatMessage[]): TraceTurn[] {
  const turns: TraceTurn[] = [];
  let current: TraceTurn | null = null;

  for (const message of messages) {
    if (message.role === 'user') {
      if (current) finalizeTurn(current);
      const at = toMs(message.at);
      const repeat = turns.find((turn) => turn.userText.trim() === message.text.trim());
      current = {
        id: message.id,
        index: turns.length + 1,
        question: shortQuestion(message.text),
        userText: message.text,
        at,
        startedAt: at,
        endedAt: at,
        steps: 0,
        tools: 0,
        failed: false,
        items: [],
        measuredMs: 0,
        wallMs: 0,
        attachments: message.attachments?.length,
        spans: [],
        tokens: { in: 0, out: 0, cache: 0 },
        errorCount: 0,
        repeatOf: repeat?.index,
      };
      turns.push(current);
      continue;
    }
    if (current) appendAssistant(current, message);
  }
  if (current) finalizeTurn(current);
  applyShares(turns);
  return turns;
}

/** 同一泳道内重叠的区间合并成一段：思考通常发生在模型调用内部，不该叠两层。 */
function mergeSpans(spans: TraceSpan[]): TraceSpan[] {
  const merged: TraceSpan[] = [];
  for (const span of spans) {
    const last = merged[merged.length - 1];
    if (last && span.start < last.end) {
      last.end = Math.max(last.end, span.end);
      continue;
    }
    merged.push({ ...span });
  }
  return merged;
}

/** 泳道只画真实占用：模型一支、工具一支，空隙就是空隙，不补「等待」块。
 *
 * 空闲多久改由泳道右端的占用率说明（占窗口百分之多少），
 * 这样既不会让一大片灰块盖住真正的耗时，也还能看出哪一支在吃时间。
 */
export function buildLanes(turns: TraceTurn[]): TraceLanes {
  const model: TraceSpan[] = [];
  const tool: TraceSpan[] = [];
  for (const turn of turns) {
    for (const span of turn.spans) {
      if (span.lane === 'model') model.push(span);
      else tool.push(span);
    }
  }
  return {
    // 思考发生在模型调用内部，合并成一段；工具之间的重叠保留原样，免得把红色的失败块吞进前一段
    model: mergeSpans(model.sort((a, b) => a.start - b.start)),
    tool: tool.sort((a, b) => a.start - b.start),
  };
}

/** 实测耗时的区间来源：模型/工具用 span 并集，输入解析用阶段自报区间。 */
function workIntervals(turns: TraceTurn[], lanes: TraceLanes): Array<[number, number]> {
  const intervals = [...lanes.model, ...lanes.tool].map(
    (span) => [span.start, span.end] as [number, number],
  );
  for (const turn of turns) {
    for (const item of turn.items) {
      if (item.kind === 'parse' && !item.synthetic && item.durationMs > 0) {
        intervals.push([item.at, item.at + item.durationMs]);
      }
    }
  }
  return intervals;
}

/** 两个数相除给比例；分子为 0 时返回 null（0% 与「没数据」必须可区分）。 */
function ratio(value: number, total: number): number | null {
  if (total <= 0 || value <= 0) return null;
  return value / total;
}

/** 允许分子为 0 的比例：首枪命中 0 是必然成本，不能当成「没数据」。 */
function shareOfTotal(value: number, total: number): number | null {
  return total > 0 ? value / total : null;
}

/** 吞吐：输出 token ÷ 模型占用毫秒，没有模型耗时就给 null。 */
function throughput(modelMs: number, tokensOut: number): number | null {
  if (modelMs <= 0 || tokensOut <= 0) return null;
  return (tokensOut / modelMs) * 1000;
}

/** 顶部状态栏用的聚合指标：实测与墙钟分开给，差值才是用户空闲。 */
export function traceStats(turns: TraceTurn[], lanes: TraceLanes): TraceStats {
  const steps = turns.reduce((sum, turn) => sum + turn.items.length, 0);
  const tools = turns.reduce((sum, turn) => sum + turn.tools, 0);
  const modelCalls = turns.reduce(
    (sum, turn) => sum + turn.items.filter((item) => item.kind === 'model').length,
    0,
  );
  const errors = turns.reduce((sum, turn) => sum + turn.errorCount, 0);
  const tokensIn = turns.reduce((sum, turn) => sum + turn.tokens.in, 0);
  const tokensOut = turns.reduce((sum, turn) => sum + turn.tokens.out, 0);
  const cacheTokens = turns.reduce((sum, turn) => sum + turn.tokens.cache, 0);
  const first = turns[0]?.startedAt ?? 0;
  const last = turns.reduce((max, turn) => Math.max(max, turn.endedAt), first);
  const modelMs = lanes.model.reduce((sum, span) => sum + (span.end - span.start), 0);
  const cold = firstCalls(turns);
  // 实测 = 所有真实占用区间的并集；墙钟 = 起止差，差值即用户空闲
  const measuredMs = unionLength(workIntervals(turns, lanes));
  return {
    totalMs: measuredMs,
    measuredMs,
    wallMs: Math.max(0, last - first),
    turns: turns.length,
    steps,
    tools,
    modelCalls,
    errors,
    tokensIn,
    tokensOut,
    cacheTokens,
    tokPerSec: throughput(modelMs, tokensOut),
    cacheRate: ratio(cacheTokens, tokensIn),
    firstCallRate: cold.seen === 0 ? null : shareOfTotal(cold.cache, cold.in),
  };
}

/** 每轮第一次模型调用的 (命中, 输入, 有几轮真的带缓存字段)。
 *
 * 首枪命中为 0 是**必然**的（缓存还没建立），必须原样给 0 而不是 null；
 * 只有模型压根不回传缓存字段时才返回 null——两种情况含义完全不同。
 */
function firstCalls(turns: TraceTurn[]): { in: number; cache: number; seen: number } {
  let inTok = 0;
  let cacheTok = 0;
  let seen = 0;
  for (const turn of turns) {
    if (!turn.firstCall) continue;
    inTok += turn.firstCall.in;
    cacheTok += turn.firstCall.cache ?? 0;
    if (turn.firstCall.cache !== undefined) seen += 1;
  }
  return { in: inTok, cache: cacheTok, seen };
}

/** 时长榜的一行：步骤 + 它属于第几轮（平铺表格里必须标出来）。 */
export interface TraceRow {
  step: TraceStep;
  turnIndex: number;
}

/** 时长榜排序：估算值沉底，其余按耗时降序。
 *
 * 估算值是「下一步时间 − 本步时间」插出来的，把它排进 Top 3 会把真正慢的步骤挤掉。
 */
export function byDurationDesc(a: { step: TraceStep }, b: { step: TraceStep }): number {
  const estimated = (row: { step: TraceStep }) => (row.step.timing.source === 'estimated' ? 1 : 0);
  if (estimated(a) !== estimated(b)) return estimated(a) - estimated(b);
  return b.step.durationMs - a.step.durationMs;
}

/** 时长视图的行集合：纳入模型、思考、工具、解析、异常；正文行不参与排名。 */
export function durationRows(
  turns: TraceTurn[],
  match: (step: TraceStep) => boolean,
): TraceRow[] {
  return turns
    .flatMap((turn) =>
      turn.items
        .filter((step) => !step.synthetic && match(step))
        .map((step) => ({ step, turnIndex: turn.index })),
    )
    .sort(byDurationDesc);
}

/** p95（nearest-rank）。样本不足 2 个时没有「分位」可言，返回 null 让界面显示 —。 */
export function percentile(values: number[], p: number): number | null {
  if (values.length < 2) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil(p * sorted.length) - 1));
  return sorted[index];
}

/** 前 n 步的合计占比：一句话说清「慢得集中还是分散」。 */
export function topShare(rows: TraceRow[], n: number): number {
  return rows
    .filter((row) => row.step.timing.source !== 'estimated')
    .slice(0, n)
    .reduce((sum, row) => sum + row.step.share, 0);
}
