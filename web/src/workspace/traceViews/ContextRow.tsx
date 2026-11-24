/** 每轮第 0 步：模型动手前手里有什么。 */

import { useState } from 'react';
import type { AgentContext } from '../../api/client';
import type { TraceTurn } from '../../types';
import { Composition, fmtTok } from './Composition';

/** 每轮第 0 步：模型动手前手里有什么——系统提示、工具 schema、历史、这条输入。 */
export function ContextRow({ turn, context }: { turn: TraceTurn; context: AgentContext | null }) {
  const [open, setOpen] = useState(false);
  const files = turn.attachments ?? turn.parse?.attachments ?? 0;
  const tools = context?.tools ?? [];
  const prompt = context?.system_prompt ?? '';
  // token 只用模型回传的精确值：首枪输入与其中命中的缓存量。历史/各段占比不再估算。
  const firstIn = turn.firstCall?.in ?? 0;
  const hit = turn.firstCall?.cache ?? 0;
  const inputChars = (turn.userText ?? '').length;
  const hitPct = firstIn > 0 && hit > 0 ? Math.round((hit / firstIn) * 100) : null;
  return (
    <>
      <div className="trace-step-wrap">
        <button
          type="button"
          className="trace-step"
          data-kind="context"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
        >
          <span className="trace-idx mono">00</span>
          <span className="trace-badge" data-kind="context">上下文</span>
          <span className="trace-main">
            <span className="trace-summary mono">
              首枪 {fmtTok(firstIn)} tok · 工具 {tools.length || turn.tools} 个 · 输入{' '}
              {inputChars} 字
              {hitPct !== null ? ` · 命中 ${hitPct}%` : ''}
              {files ? ` · 附件 ${files}` : ''}
            </span>
          </span>
          {/* 缓存 / 占比两列：占位也要渲染，否则整行会错位 */}
          <span className="trace-cache-slot" aria-hidden="true" />
          <span className="trace-share" aria-hidden="true" />
          <span className="trace-dur mono">—</span>
          <span className="trace-dot" aria-hidden="true" />
          <span className="trace-caret" aria-hidden="true">{open ? '⌄' : '›'}</span>
        </button>
      </div>
      {open && (
        <div className="trace-detail trace-context-body">
          {context ? (
            <>
              <div className="ctx-meta mono">
                <span>{context.profile_label}</span>
                <span>指纹 {context.prefix_fingerprint}</span>
                {firstIn > 0 && (
                  <span>
                    首枪 {hit.toLocaleString()} / {firstIn.toLocaleString()} tok
                  </span>
                )}
              </div>
              <Composition input={firstIn} cache={hit} />
            </>
          ) : (
            <p className="trace-detail-empty">上下文接口不可用，读不到这一轮装配了什么。</p>
          )}
          <div className="trace-detail-block">
            <span className="trace-detail-label">用户输入</span>
            <pre className="trace-detail-code mono">
              {turn.userText || '（空输入）'}
              {files ? `\n\n附件 ${files} 个` : ''}
            </pre>
          </div>
          <div className="trace-detail-block">
            <span className="trace-detail-label">
              系统提示词{context ? ` · ${context.system_prompt_chars} 字` : ''}
            </span>
            <pre className="trace-detail-code mono">{prompt || '读不到系统提示词。'}</pre>
          </div>
          {context && (
            <div className="trace-detail-block">
              <span className="trace-detail-label">档位约束 · 追加在消息末尾</span>
              <pre className="trace-detail-code mono">{context.policy_note}</pre>
            </div>
          )}
          <div className="trace-detail-block">
            <span className="trace-detail-label">
              工具{context ? ` · ${context.tools.length} 个 · ${context.tools_chars} 字` : ''}
            </span>
            {tools.length > 0 ? (
              <div className="ctx-tools">
                {tools.map((tool) => (
                  <details className="ctx-tool" key={tool.name}>
                    <summary>
                      <code>{tool.name}</code>
                      <span className="ctx-tool-meta mono">{tool.schema_chars} 字</span>
                    </summary>
                    <p className="ctx-tool-desc">{tool.description}</p>
                    <pre className="trace-detail-code mono">
                      {JSON.stringify(tool.parameters, null, 2)}
                    </pre>
                  </details>
                ))}
              </div>
            ) : (
              <p className="trace-detail-empty">没有读到工具注册表。</p>
            )}
          </div>
        </div>
      )}
    </>
  );
}
