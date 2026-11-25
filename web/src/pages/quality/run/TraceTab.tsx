/** 轨迹证据 Tab：时间线分节折叠，长文本默认只给摘要。 */

import { useState, type ReactNode } from 'react';
import type { OjCaseDetail } from '../../../types';
import { excerpt, statusText } from './diagnosis';

function asText(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value === undefined || value === null) return '—';
  return JSON.stringify(value, null, 2) ?? '—';
}

function RawText({ value }: { value: unknown }) {
  const [expanded, setExpanded] = useState(false);
  const text = asText(value);
  return (
    <div className="oj-raw-text">
      <p className="muted">{excerpt(text)}</p>
      {text.length > 200 && (
        <button type="button" className="oj-link-btn" onClick={() => setExpanded((open) => !open)}>
          {expanded ? '收起原文' : '展开原文'}
        </button>
      )}
      {expanded && <pre className="oj-raw">{text}</pre>}
    </div>
  );
}

function Evidence({ title, children }: { title: string; children: ReactNode }) {
  return (
    <details className="oj-evidence">
      <summary>{title}</summary>
      <div className="oj-evidence-body">{children}</div>
    </details>
  );
}

interface Props {
  detail: OjCaseDetail;
}

export function TraceTab({ detail }: Props) {
  const trace = detail.trace;
  const turns = trace.turns ?? [];
  const calls = trace.tool_calls ?? [];
  const events = trace.events.length > 0 ? trace.events : detail.events;
  const workspace: Record<string, unknown> = {
    ...(detail.workspace ?? {}),
    ...(trace.workspace ?? {}),
  };

  return (
    <div className="oj-trace">
      <p className="hint">
        轨迹终态：{statusText(trace.status ?? detail.status)}
        {trace.error ? ` · ${trace.error}` : ''}
        {trace.model ? ` · 模型 ${trace.model}` : ''}
      </p>
      <Evidence title={`对话轮次（${turns.length}）`}>
        {turns.length === 0 ? (
          <p className="muted">没有轮次记录</p>
        ) : (
          <ol className="oj-turns">
            {turns.map((turn, index) => (
              <li key={index}>
                <RawText value={turn} />
              </li>
            ))}
          </ol>
        )}
      </Evidence>
      <Evidence title={`工具调用（${calls.length}）`}>
        {calls.length === 0 ? (
          <p className="muted">没有工具调用</p>
        ) : (
          <ul className="oj-calls">
            {calls.map((call, index) => (
              <li key={`${call.name}-${index}`}>
                <strong>{call.name}</strong> <span className="muted">{call.state}</span>
                <div className="muted">入参</div>
                <RawText value={call.args} />
                <div className="muted">结果摘要</div>
                <RawText value={call.result_excerpt} />
              </li>
            ))}
          </ul>
        )}
      </Evidence>
      <Evidence title={`事件（${events.length}）`}>
        {events.length === 0 ? (
          <p className="muted">没有事件</p>
        ) : (
          <div className="tag-row">
            {events.map((event, index) => (
              <span className="tag-chip" key={`${event}-${index}`}>
                {event}
              </span>
            ))}
          </div>
        )}
      </Evidence>
      <Evidence title="输入阶段">
        <RawText value={trace.input_stages.length > 0 ? trace.input_stages : detail.input_stages} />
      </Evidence>
      <Evidence title="工作区状态与 diff">
        <div className="muted">文件状态</div>
        <RawText value={workspace.states ?? {}} />
        <div className="muted">diff</div>
        <RawText value={workspace.diff ?? ''} />
        <div className="muted">指纹</div>
        <RawText value={workspace.hashes ?? {}} />
      </Evidence>
      <Evidence title="裁判输入快照">
        <RawText value={trace.judge_input ?? {}} />
      </Evidence>
      <Evidence title="最终回复">
        <RawText value={trace.final_text ?? ''} />
      </Evidence>
    </div>
  );
}
