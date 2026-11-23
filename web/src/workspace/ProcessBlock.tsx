/** 过程块：把不同类别的事件步骤渲染成带区分标注的时间线，可折叠。 */
import { useState } from 'react';
import type { ProcessEntry, ProcessKind } from '../types';

interface Props {
  steps: ProcessEntry[];
  running: boolean;
  modelName: string;
  inputTokens: number;
  outputTokens: number;
  startedAt: string | null;
  endedAt: string | null;
}

/** 每类步骤的视觉标注：标签文字 + 语义色 class。 */
const KIND_META: Record<ProcessKind, { tag: string; tone: string }> = {
  lifecycle: { tag: '系统', tone: 'sys' },
  text: { tag: '正文', tone: 'text' },
  thinking: { tag: '思考', tone: 'think' },
  tool: { tag: '工具', tone: 'tool' },
  control: { tag: '控制', tone: 'control' },
  error: { tag: '异常', tone: 'error' },
};

/** 毫秒差格式化为秒（1 位小数）；缺时间戳返回空串。 */
export function formatDuration(startedAt: string | null, endedAt: string | null): string {
  if (!startedAt || !endedAt) return '';
  const ms = new Date(endedAt).getTime() - new Date(startedAt).getTime();
  if (!Number.isFinite(ms) || ms < 0) return '';
  return `${(ms / 1000).toFixed(1)}s`;
}

function StateIcon({ state }: { state: ProcessEntry['state'] }) {
  if (state === 'success') {
    return (
      <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-label="完成" className="proc-icon success">
        <path d="M3 8.5l3 3 7-7" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }
  if (state === 'error') {
    return (
      <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-label="失败" className="proc-icon error">
        <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
      </svg>
    );
  }
  return <span className="proc-icon running" aria-label="进行中" />;
}

export function ProcessBlock({
  steps,
  running,
  modelName,
  inputTokens,
  outputTokens,
  startedAt,
  endedAt,
}: Props) {
  // 默认折叠成一行胶囊：过程再详细也不该比答案更占地方。
  const [collapsed, setCollapsed] = useState(true);
  if (steps.length === 0) return null;

  const duration = formatDuration(startedAt, endedAt);
  const summary = [`${steps.length} 步`, duration].filter(Boolean).join(' · ');

  return (
    <div className={`process-block ${collapsed ? 'collapsed' : 'expanded'}`}>
      <button
        className="process-head"
        onClick={() => setCollapsed((c) => !c)}
        aria-expanded={!collapsed}
      >
        <StateIcon state={running ? 'running' : 'success'} />
        <span className="process-title">{running ? '执行中…' : '已完成'}</span>
        <span className="process-meta">{summary}</span>
        <span className="process-toggle">{collapsed ? '展开' : '收起'}</span>
      </button>
      {!collapsed && (
        <>
          <ol className="process-steps">
            {steps.map((step) => {
              const tag = KIND_META[step.kind];
              return (
                <li className={`process-step tone-${tag.tone}`} key={step.id} data-kind={step.kind}>
                  <StateIcon state={step.state} />
                  <span className="process-tag">{tag.tag}</span>
                  <span className="process-label">{step.label}</span>
                  {step.detail && <span className="process-detail">{step.detail}</span>}
                </li>
              );
            })}
          </ol>
          <div className="process-foot">
            {[modelName, `输入 token ${inputTokens} / 输出 token ${outputTokens}`]
              .filter(Boolean)
              .join(' · ')}
          </div>
        </>
      )}
    </div>
  );
}
