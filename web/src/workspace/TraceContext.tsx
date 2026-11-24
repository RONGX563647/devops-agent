/** 上下文页签：模型动手前手里到底有什么，给完整原文而不是摘要。
 *
 * 和列表里第 00 步（ContextRow）同源同口径，只是这里不折叠进一行：
 * - 构成条：系统提示 / 工具 schema / 历史 / 本轮输入 / 档位约束
 * - 历史上下文：本轮之前每一轮的「用户说了什么、助手回了什么」，按轮分开
 * - 本轮输入、系统提示词、工具清单、档位约束：原文照录
 *
 * token 只用模型回传的精确值：首枪 input_tokens 与其中命中的 cache_input_tokens。
 * 历史/各段的 token 拆分一律不估算——那会把误差全堆到「历史」那一段。
 * 历史正文直接从 messages 派生的 TraceTurn 里取，不另外请求接口。
 */
import type { AgentContext } from '../api/client';
import type { TraceTurn } from '../types';
import { Composition, fmtTok } from './TraceList';

interface Props {
  context: AgentContext | null;
  turn: TraceTurn;
  /** 全部轮次：历史 = index 比本轮小的那些。 */
  turns: TraceTurn[];
}

/** 助手在这一轮说出的最终正文：合成行就是模型输出，不参与耗时。 */
function assistantText(turn: TraceTurn): string {
  return turn.items.find((step) => step.synthetic && step.kind === 'text')?.text ?? '';
}

export function ContextTab({ context, turn, turns }: Props) {
  const tools = context?.tools ?? [];
  // token 只用模型回传的精确值：首枪输入与其中命中的缓存量。
  const firstIn = turn.firstCall?.in ?? 0;
  const hit = turn.firstCall?.cache ?? 0;
  const inputChars = (turn.userText ?? '').length;
  const history = turns.filter((item) => item.index < turn.index);
  const files = turn.attachments ?? turn.parse?.attachments ?? 0;

  return (
    <div className="ctx-panel">
      {context ? (
        <>
          <div className="ctx-meta mono">
            <span>{context.profile_label}</span>
            <span>指纹 {context.prefix_fingerprint}</span>
          </div>
          <Composition input={firstIn} cache={hit} />
          {firstIn > 0 && (
            <p className="inspect-note mono">
              首枪输入 {firstIn.toLocaleString()} tok · 命中缓存 {hit.toLocaleString()} tok
              {hit > 0 ? ` (${Math.round((hit / firstIn) * 100)}%)` : ''}
              ；两项均为模型回传的精确值，原文见下列。
            </p>
          )}
        </>
      ) : (
        <p className="trace-detail-empty">上下文接口不可用，读不到这一轮装配了什么。</p>
      )}

      {/* 历史上下文：分开按轮列，默认收起——长会话全展开等于没分块 */}
      <section className="ctx-section">
        <div className="ctx-section-head">
          <span>历史上下文</span>
          <span className="mono">{history.length} 轮</span>
        </div>
        {history.length === 0 ? (
          <p className="inspect-note">这是第一轮，模型手里只有系统提示与工具清单。</p>
        ) : (
          <div className="ctx-history">
            {history.map((item) => {
              const said = assistantText(item);
              return (
                <details className="ctx-history-turn" key={item.id}>
                  <summary>
                    <span className="ctx-history-no mono">第 {item.index} 轮</span>
                    <span className="ctx-history-q">{item.question || item.userText || '（空问题）'}</span>
                    <span className="mono ctx-history-size">
                      {fmtTok(item.userText.length + said.length)} 字
                    </span>
                  </summary>
                  <div className="ctx-history-body">
                    <span className="trace-detail-label">用户</span>
                    <pre className="trace-detail-code mono">{item.userText || '（空输入）'}</pre>
                    <span className="trace-detail-label">助手</span>
                    <pre className="trace-detail-code mono">{said || '（这一轮没有产出正文）'}</pre>
                  </div>
                </details>
              );
            })}
          </div>
        )}
      </section>

      <section className="ctx-section">
        <div className="ctx-section-head">
          <span>本轮输入</span>
          <span className="mono">{inputChars} 字</span>
        </div>
        <pre className="trace-detail-code mono">
          {turn.userText || '（空输入）'}
          {files ? `\n\n附件 ${files} 个` : ''}
        </pre>
      </section>

      <section className="ctx-section">
        <div className="ctx-section-head">
          <span>系统提示词</span>
          <span className="mono">{context ? `${context.system_prompt_chars} 字` : '—'}</span>
        </div>
        <pre className="trace-detail-code mono">{context?.system_prompt || '读不到系统提示词。'}</pre>
      </section>

      <section className="ctx-section">
        <div className="ctx-section-head">
          <span>工具清单</span>
          <span className="mono">
            {tools.length > 0 ? `${tools.length} 个 · ${context?.tools_chars ?? 0} 字` : '—'}
          </span>
        </div>
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
      </section>

      {context && (
        <section className="ctx-section">
          <div className="ctx-section-head">
            <span>档位约束</span>
            <span className="mono">追加在消息末尾</span>
          </div>
          <pre className="trace-detail-code mono">{context.policy_note || '（无附加约束）'}</pre>
        </section>
      )}
    </div>
  );
}
