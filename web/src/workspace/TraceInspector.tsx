/** 轨迹详情检查面板：概述 / 上下文 / 参数 / 结果 / Schema / 计时。
 *
 * 与列表并排常驻，点哪条看哪条，不需要来回展开——这是排查工具调用失败时
 * 最常用的一段路径（看参数形状 → 看返回原文 → 看计时来源）。
 *
 * 两种粒度：选中步骤看这一步；只选中轮次（点轮次标题）时看整轮概要 + 完整上下文。
 */
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { AgentContext } from '../api/client';
import { fetchToolSchemas } from '../api/client';
import type { TraceStep, TraceTurn } from '../types';
import { ContextTab } from './TraceContext';
import { stageMetric } from './StepRow';
import { formatDateTime, formatDuration } from './time';

interface Props {
  step: TraceStep | null;
  turn: TraceTurn | null;
  totalMs: number;
  /** 当前会话档位：Schema 页签要按同一档位的工具集查，否则会答「没有这个工具」。 */
  profile?: string;
  /** 本轮装配的上下文：上下文页签与列表第 00 步同源。 */
  context?: AgentContext | null;
  /** 全部轮次：上下文页签要用它列出本轮之前的历史。 */
  turns?: TraceTurn[];
}

type Tab = 'summary' | 'context' | 'args' | 'result' | 'text' | 'schema' | 'timing';

interface TabSpec {
  key: Tab;
  label: string;
}

/** 思考与模型输出在数据里都是 text 类步骤，靠标题区分。 */
function isThinking(step: TraceStep): boolean {
  return step.kind === 'text' && step.title === '思考';
}

/** 面板按这一步「真的有什么」出页签：思考没有入参，输出没有 Schema，工具没有正文。
 *
 * 「上下文」常驻在概述之后：它说的是模型这一轮手里有什么，与选中哪一步无关。
 */
function tabsFor(step: TraceStep): TabSpec[] {
  const tabs: TabSpec[] = [
    { key: 'summary', label: '概述' },
    { key: 'context', label: '上下文' },
  ];
  if (step.args) tabs.push({ key: 'args', label: '参数' });
  if (step.result || step.diff || (step.stages?.length ?? 0) > 0) {
    const label = step.diff ? '改动' : (step.stages?.length ?? 0) > 0 ? '阶段' : '结果';
    tabs.push({ key: 'result', label });
  }
  if (step.text) tabs.push({ key: 'text', label: isThinking(step) ? '思考' : '正文' });
  if (step.toolName) tabs.push({ key: 'schema', label: 'Schema' });
  tabs.push({ key: 'timing', label: '计时' });
  return tabs;
}

/** 轮次粒度只有三个页签：概要说这一轮干了什么，上下文说它手里有什么。 */
const TURN_TABS: TabSpec[] = [
  { key: 'summary', label: '概述' },
  { key: 'context', label: '上下文' },
  { key: 'timing', label: '计时' },
];

const LAYER_TEXT: Record<TraceStep['layer'], string> = {
  model: '模型',
  tool: '工具',
  input: '输入解析',
  control: '控制',
  memory: '记忆',
};

const KIND_TEXT: Record<TraceStep['kind'], string> = {
  tool: '工具调用',
  text: '思考',
  model: '模型',
  error: '异常',
  parse: '解析',
  memory: '记忆',
};

/** 计时来源的读法：这三档的可信度完全不同，必须写清楚。 */
const TIMING_TEXT: Record<TraceStep['timing']['source'], string> = {
  measured: '事件时间戳',
  derived: '阶段自报',
  estimated: '相邻步骤估算',
};

function pretty(text?: string): string {
  const raw = (text ?? '').trim();
  if (!raw) return '';
  try {
    return JSON.stringify(JSON.parse(raw) as unknown, null, 2);
  } catch {
    return raw;
  }
}

async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard?.writeText(text);
  } catch {
    // 非安全上下文没有剪贴板：忽略
  }
}

function Block({ label, text }: { label: string; text: string }) {
  return (
    <div className="inspect-block">
      <div className="inspect-block-head">
        <span>{label}</span>
        <button type="button" onClick={() => void copyText(text)}>
          复制
        </button>
      </div>
      <pre className="inspect-code mono">{text || '（空）'}</pre>
    </div>
  );
}

/** 解析阶段：只列真正生效或出错的，无改动的折成一句计数。 */
function Stages({ stages }: { stages: NonNullable<TraceStep['stages']> }) {
  const visible = stages.filter(
    (stage) => stage.changed || stage.status === 'error' || stage.failed > 0,
  );
  const hidden = stages.length - visible.length;
  return (
    <>
      <div className="inspect-stages">
        {visible.map((stage) => (
          <div className="inspect-stage" key={stage.stage}>
            <span>{stage.label}</span>
            <span className="mono">{stage.summary}</span>
            <span className="mono">{stageMetric(stage)}</span>
          </div>
        ))}
      </div>
      {hidden > 0 && <p className="inspect-note">另有 {hidden} 项无改动，完整解析链见运行记录。</p>}
    </>
  );
}

/** 比例条：这一条占本轮 / 全会话多少，比只看毫秒数直观。 */
function Ratio({ label, value, total }: { label: string; value: number; total: number }) {
  const ratio = total > 0 ? Math.min(1, value / total) : 0;
  return (
    <div className="inspect-ratio">
      <span className="inspect-key">{label}</span>
      <div className="inspect-ratio-track">
        <div className="inspect-ratio-fill" style={{ width: `${Math.round(ratio * 100)}%` }} />
      </div>
      <span className="mono">{Math.round(ratio * 100)}%</span>
    </div>
  );
}

function Summary({ step, turn }: { step: TraceStep; turn: TraceTurn | null }) {
  // 分母用本轮实测耗时（span 并集）：墙钟含用户空闲，除出来会把占比压小
  const turnMs = turn ? turn.measuredMs : 0;
  return (
    <div className="inspect-summary">
      <div className="inspect-grid">
        <span className="inspect-key">层级</span>
        <span>
          {LAYER_TEXT[step.layer]}
          {step.toolName ? ` · ${step.toolName}` : ''}
        </span>
        <span className="inspect-key">状态</span>
        <span className={`inspect-status ${step.status ?? 'success'}`}>
          {step.status === 'running' ? '执行中' : step.status === 'error' ? '失败' : '已完成'}
        </span>
        <span className="inspect-key">耗时</span>
        <span className="mono">
          {step.timing.source === 'estimated' && step.durationMs > 0 ? '≈' : ''}
          {formatDuration(step.durationMs)}
          {turnMs > 0 && ` · 占本轮 ${Math.round((step.durationMs / turnMs) * 100)}%`}
        </span>
        {(step.tokensIn !== undefined || step.tokensOut !== undefined) && (
          <>
            <span className="inspect-key">Token</span>
            <span className="mono">
              入 {step.tokensIn ?? 0} / 出 {step.tokensOut ?? 0}
              {step.cacheTokens ? ` · 缓存 ${step.cacheTokens}` : ''}
            </span>
          </>
        )}
        {turn && (
          <>
            <span className="inspect-key">位置</span>
            <span className="mono">
              第 {turn.index} 轮 · 第 {String(step.index).padStart(2, '0')} 步
            </span>
          </>
        )}
        {step.toolCallId && (
          <>
            <span className="inspect-key">调用 ID</span>
            <span className="mono inspect-muted">{step.toolCallId}</span>
          </>
        )}
      </div>
      {step.argsSummary && (
        <div className="inspect-line">
          <span className="inspect-key">入参</span>
          <span className="mono">{step.argsSummary}</span>
        </div>
      )}
      {step.resultSummary && (
        <div className="inspect-line">
          <span className="inspect-key">返回</span>
          <span className="mono">{step.resultSummary}</span>
        </div>
      )}
      {step.text && (
        <div className="inspect-line">
          <span className="inspect-key">说明</span>
          <span>{step.text}</span>
        </div>
      )}
    </div>
  );
}

/** 轮次概览：这一轮问了什么、花了多久、跑了几步、吐了多少 token。 */
function TurnSummary({ turn }: { turn: TraceTurn }) {
  const slowest = turn.items
    .filter((item) => !item.synthetic && item.timing.source !== 'estimated')
    .reduce<TraceStep | null>(
      (best, item) => (!best || item.durationMs > best.durationMs ? item : best),
      null,
    );
  return (
    <div className="inspect-summary">
      <div className="inspect-grid">
        <span className="inspect-key">轮次</span>
        <span className="mono">
          第 {turn.index} 轮
          {turn.repeatOf ? ` · 同第 ${turn.repeatOf} 轮` : ''}
        </span>
        <span className="inspect-key">问题</span>
        <span>{turn.question || '（空问题）'}</span>
        <span className="inspect-key">开始</span>
        <span className="mono">{formatDateTime(turn.startedAt)}</span>
        <span className="inspect-key">实测</span>
        <span className="mono">
          {formatDuration(turn.measuredMs)} · 墙钟 {formatDuration(turn.wallMs)}
        </span>
        <span className="inspect-key">规模</span>
        <span className="mono">
          {turn.steps} 步 · {turn.tools} 次工具
        </span>
        <span className="inspect-key">Token</span>
        <span className="mono">
          入 {turn.tokens.in} / 出 {turn.tokens.out}
          {turn.tokens.cache ? ` · 缓存 ${turn.tokens.cache}` : ''}
        </span>
        <span className="inspect-key">异常</span>
        <span className={turn.errorCount > 0 ? 'inspect-status error' : ''}>
          {turn.errorCount} 个
        </span>
        {slowest && (
          <>
            <span className="inspect-key">最慢</span>
            <span className="mono">
              {slowest.toolName || slowest.title} {formatDuration(slowest.durationMs)}
            </span>
          </>
        )}
      </div>
      {turn.userText && (
        <div className="inspect-line">
          <span className="inspect-key">输入</span>
          <span>{turn.userText.length > 200 ? `${turn.userText.slice(0, 200)}…` : turn.userText}</span>
        </div>
      )}
      <p className="inspect-note">
        想看模型这一轮手里有什么（系统提示、工具 schema、此前各轮原文），切到「上下文」。
      </p>
    </div>
  );
}

/** 轮次计时：实测与墙钟分开列，别把用户读消息的空闲算进模型耗时。 */
function TurnTiming({ turn, totalMs }: { turn: TraceTurn; totalMs: number }) {
  return (
    <div className="inspect-timing">
      <div className="inspect-grid">
        <span className="inspect-key">开始</span>
        <span className="mono">{formatDateTime(turn.startedAt)}</span>
        <span className="inspect-key">结束</span>
        <span className="mono">{formatDateTime(turn.endedAt)}</span>
        <span className="inspect-key">实测</span>
        <span className="mono">{formatDuration(turn.measuredMs)}</span>
        <span className="inspect-key">墙钟</span>
        <span className="mono">{formatDuration(turn.wallMs)}</span>
      </div>
      <Ratio label="占全会话实测" value={turn.measuredMs} total={totalMs} />
      <p className="inspect-note">
        实测 = 本轮所有步骤占用区间的并集；墙钟含你读上一条回复的空闲，两者不等是正常的。
      </p>
    </div>
  );
}

function SchemaTab({ step, profile }: { step: TraceStep; profile?: string }) {
  const enabled = Boolean(step.toolName);
  const schemas = useQuery({
    queryKey: ['tool-schemas', profile ?? 'default'],
    queryFn: () => fetchToolSchemas(profile),
    staleTime: 5 * 60_000,
    enabled,
  });
  if (!enabled) return <p className="inspect-empty">这一步不是工具调用，没有 Schema。</p>;
  if (schemas.isLoading) return <p className="inspect-empty">正在读取工具注册表…</p>;
  if (schemas.isError) return <p className="inspect-empty">工具 schema 读取失败（后端未启动？）。</p>;
  const tool = schemas.data?.find(
    (item) => item.function.name.toLowerCase() === (step.toolName ?? '').toLowerCase(),
  );
  if (!tool) {
    return (
      <p className="inspect-empty">
        工具注册表里没有「{step.toolName}」，可能来自历史运行或已下线。
      </p>
    );
  }
  return (
    <div className="inspect-schema">
      <p className="inspect-desc">{tool.function.description || '（无说明）'}</p>
      <Block label="参数定义" text={pretty(JSON.stringify(tool.function.parameters ?? {}))} />
    </div>
  );
}

export function TraceInspector({ step, turn, totalMs, profile, context, turns }: Props) {
  const [tab, setTab] = useState<Tab>('summary');

  if (!step) {
    if (!turn) {
      return (
        <aside className="inspector inspector-empty-state">
          <strong>选中一轮或一条步骤</strong>
          <p>左侧点轮次标题看整轮概要，点任一步骤看参数、返回原文、工具 Schema 与计时来源。</p>
        </aside>
      );
    }
    // 只选中轮次：没有「这一步的参数」，给整轮概要与完整上下文
    const turnTab: Tab = TURN_TABS.some((item) => item.key === tab) ? tab : 'summary';
    return (
      <aside className="inspector" aria-label="轮次详情">
        <div className="inspector-head mono">
          <span>第 {turn.index} 轮 · {turn.steps} 步</span>
          <span className="inspector-kind" data-kind="context">整轮</span>
        </div>
        <div className="inspector-tabs" role="tablist">
          {TURN_TABS.map((item) => (
            <button
              key={item.key}
              role="tab"
              aria-selected={turnTab === item.key}
              className={turnTab === item.key ? 'active' : ''}
              onClick={() => setTab(item.key)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <div className="inspector-body" role="tabpanel">
          {turnTab === 'summary' && <TurnSummary turn={turn} />}
          {turnTab === 'context' && (
            <ContextTab context={context ?? null} turn={turn} turns={turns ?? [turn]} />
          )}
          {turnTab === 'timing' && <TurnTiming turn={turn} totalMs={totalMs} />}
        </div>
      </aside>
    );
  }

  const turnMs = turn ? turn.measuredMs : 0;
  const argsText = step.args ?? '';
  const resultText = step.result ?? '';
  const tabs = tabsFor(step);
  const current: Tab = tabs.some((item) => item.key === tab) ? tab : 'summary';

  return (
    <aside className="inspector" aria-label="步骤详情">
      <div className="inspector-head mono">
        <span>
          {turn ? `第 ${turn.index} 轮 · 第 ${String(step.index).padStart(2, '0')} 步` : '步骤详情'}
        </span>
        <span className="inspector-kind" data-kind={step.kind}>
          {step.toolName ?? KIND_TEXT[step.kind]}
        </span>
      </div>
      <div className="inspector-tabs" role="tablist">
        {tabs.map((item) => (
          <button
            key={item.key}
            role="tab"
            aria-selected={current === item.key}
            className={current === item.key ? 'active' : ''}
            onClick={() => setTab(item.key)}
          >
            {item.label}
          </button>
        ))}
      </div>
      <div className="inspector-body" role="tabpanel">
        {current === 'summary' && <Summary step={step} turn={turn} />}
        {current === 'context' && turn && (
          <ContextTab context={context ?? null} turn={turn} turns={turns ?? [turn]} />
        )}
        {current === 'args' && <Block label="入参原文" text={pretty(argsText)} />}
        {current === 'text' && (
          <Block label={isThinking(step) ? '思考原文' : '正文原文'} text={step.text ?? ''} />
        )}
        {current === 'result' && (
          <>
            {step.stages && step.stages.length > 0 && <Stages stages={step.stages} />}
            {step.diff && (
              <div className="inspect-diff">
                {step.diff.oldLines.map((line, i) => (
                  <div className="diff-old" key={`o${i}`}>- {line || ' '}</div>
                ))}
                {step.diff.newLines.map((line, i) => (
                  <div className="diff-new" key={`n${i}`}>+ {line || ' '}</div>
                ))}
              </div>
            )}
            {(step.result || !step.diff) && <Block label="返回原文" text={resultText} />}
          </>
        )}
        {current === 'schema' && <SchemaTab step={step} profile={profile} />}
        {current === 'timing' && (
          <div className="inspect-timing">
            <div className="inspect-grid">
              <span className="inspect-key">开始时间</span>
              <span className="mono">{formatDateTime(step.at)}</span>
              <span className="inspect-key">时长</span>
              <span className="mono">{formatDuration(step.durationMs)}</span>
              <span className="inspect-key">计时来源</span>
              <span>
                {TIMING_TEXT[step.timing.source]}
                {step.timing.source === 'estimated' && ' · 仅供参考'}
              </span>
            </div>
            <Ratio label="占本轮实测" value={step.durationMs} total={turnMs} />
            <Ratio label="占全会话实测" value={step.durationMs} total={totalMs} />
            <p className="inspect-note">
              估算值只出现在旧数据里：事件没有配对结束时间，只能取下一条的开始时间相减。
              实测 = 所有步骤占用区间的并集；墙钟含用户读消息的空闲，两者不等是正常的。
            </p>
          </div>
        )}
      </div>
    </aside>
  );
}

