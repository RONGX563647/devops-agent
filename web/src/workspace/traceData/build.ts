/** 把会话消息派生为轨迹视图：轮次 + 步骤 + 真实耗时 + 三条资源泳道。 */

import type {
  ChatMessage,
  ParseStage,
  ProcessEntry,
  TraceLanes,
  TraceSpan,
  TraceStep,
  TraceTiming,
  TraceTurn,
} from '../../types';
import { buildDiff, shortQuestion, summarizeArgs, summarizeResult, toMs, unionLength } from './format';

/** 步骤耗时上限：一轮卡住时不要把整段等待算进最后一步。 */
const MAX_STEP_MS = 10 * 60_000;

function layerOf(step: ProcessEntry): TraceStep['layer'] {
  if (step.kind === 'tool') return 'tool';
  if (step.kind === 'parse') return 'input';
  if (step.kind === 'error' || step.kind === 'control') return 'control';
  if (step.kind === 'memory') return 'memory';
  return 'model';
}

function kindOf(step: ProcessEntry): TraceStep['kind'] {
  if (step.kind === 'tool') return 'tool';
  if (step.kind === 'parse') return 'parse';
  if (step.kind === 'error') return 'error';
  if (step.kind === 'control') return step.state === 'error' ? 'error' : 'text';
  if (step.kind === 'model') return 'model';
  if (step.kind === 'memory') return 'memory';
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
function stepToTrace(turn: TraceTurn, step: ProcessEntry, index: number): TraceStep | null {
  if (step.kind === 'lifecycle') return null;
  const toolName = step.toolName;
  const at = toMs(step.at);
  const ended = toMs(step.endedAt);
  // 解析阶段没有配对的起止事件，耗时取各阶段自报的 ms（derived）
  const stagesMs = (step.parseStages ?? []).reduce((sum, stage) => sum + stage.ms, 0);
  const timing = timingOf(at, ended, stagesMs);
  return {
    id: traceId(turn, step.id),
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

/** 轨迹内唯一 id：步骤 id 只在单条消息内唯一（记忆步骤按帧序号编号），
 * 而泳道与选中态都跨轮聚合 —— 不加消息命名空间就会撞 React key、点选落到错的轮。
 */
function traceId(turn: TraceTurn, stepId: string): string {
  return `${turn.id}:${stepId}`;
}

/** 模型调用进入泳道：同一 reply 的 START/END 已在事件折叠时配对。
 *
 * `id` 是已带轮次前缀的轨迹 id，泳道 key 与步骤行共用同一份，选中才不会串轮。
 */
function modelSpan(step: ProcessEntry, at: number, id: string): TraceSpan | null {
  const end = toMs(step.endedAt);
  if (!at || !end || end < at) return null;
  return {
    id: `span:${id}`,
    lane: 'model',
    start: at,
    end,
    status: spanStatus(step),
    label: step.kind === 'thinking' ? '思考' : step.label,
    stepId: id,
  };
}

/** 工具调用进入泳道；缺结束事件时画成 1ms 的点，仍在跑的用 running 态。 */
function toolSpan(step: ProcessEntry, at: number, id: string): TraceSpan {
  return {
    id: `span:${id}`,
    lane: 'tool',
    start: at,
    end: Math.max(toMs(step.endedAt), at + 1),
    status: spanStatus(step),
    label: step.toolName ?? step.label.replace(/^调用工具\s*·\s*/, ''),
    stepId: id,
  };
}

/** 记忆动作通常只有几毫秒：缺结束时间时画成 1ms 的点，仍能被选中。 */
function memorySpan(step: ProcessEntry, at: number, id: string): TraceSpan {
  return {
    id: `span:${id}`,
    lane: 'memory',
    start: at,
    end: Math.max(toMs(step.endedAt), at + 1),
    status: spanStatus(step),
    label: step.label,
    stepId: id,
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

/** 步骤 → 泳道区间：模型（含思考）走模型泳道，工具走工具泳道，其余不画。 */
function spanFor(turn: TraceTurn, step: ProcessEntry, at: number): TraceSpan | null {
  const id = traceId(turn, step.id);
  if (step.kind === 'model' || step.kind === 'thinking') return modelSpan(step, at, id);
  if (step.kind === 'tool') return toolSpan(step, at, id);
  if (step.kind === 'memory') return memorySpan(step, at, id);
  return null;
}

/** 记下本轮第一次模型调用的输入与命中：它等于「前缀 + 历史 + 输入」。 */
function noteFirstCall(turn: TraceTurn, step: ProcessEntry): void {
  if (step.kind !== 'model' || (step.tokensIn ?? 0) <= 0 || turn.firstCall) return;
  turn.firstCall = { in: step.tokensIn ?? 0, cache: step.cacheTokens };
}

function absorbStep(turn: TraceTurn, step: ProcessEntry): void {
  const span = spanFor(turn, step, toMs(step.at));
  if (span) turn.spans.push(span);
  noteFirstCall(turn, step);
  if (step.kind === 'parse' && step.parseStages) accumulateParse(turn, step.parseStages);
  const trace = stepToTrace(turn, step, 0);
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
  if (message.webAccess) turn.webAccess = message.webAccess;
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

/** 泳道 id 必须互不相同：React key 与选中态都靠它，重复 key 的渲染行为是未定义的。
 *
 * 上游已按轮次加过命名空间，这里再兜一层 —— 历史缓存或将来新增的事件族若又造出
 * 重复 id，加后缀至少不会让泳道静默丢块。
 */
function uniqueSpans(spans: TraceSpan[]): TraceSpan[] {
  const seen = new Map<string, number>();
  return spans.map((span) => {
    const hit = seen.get(span.id) ?? 0;
    seen.set(span.id, hit + 1);
    return hit === 0 ? span : { ...span, id: `${span.id}#${hit}` };
  });
}

/** 泳道只画真实占用：模型一支、工具一支，空隙就是空隙，不补「等待」块。
 *
 * 空闲多久改由泳道右端的占用率说明（占窗口百分之多少），
 * 这样既不会让一大片灰块盖住真正的耗时，也还能看出哪一支在吃时间。
 */
export function buildLanes(turns: TraceTurn[]): TraceLanes {
  const model: TraceSpan[] = [];
  const tool: TraceSpan[] = [];
  const memory: TraceSpan[] = [];
  for (const turn of turns) {
    for (const span of turn.spans) {
      if (span.lane === 'model') model.push(span);
      else if (span.lane === 'memory') memory.push(span);
      else tool.push(span);
    }
  }
  return {
    // 思考发生在模型调用内部，合并成一段；工具之间的重叠保留原样，免得把红色的失败块吞进前一段
    model: uniqueSpans(mergeSpans(model.sort((a, b) => a.start - b.start))),
    tool: uniqueSpans(tool.sort((a, b) => a.start - b.start)),
    memory: uniqueSpans(memory.sort((a, b) => a.start - b.start)),
  };
}
