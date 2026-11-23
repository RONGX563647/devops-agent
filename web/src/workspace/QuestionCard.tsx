/** 问答卡片：AskUser 让模型停下来问人时展示。
 *
 * 与确认卡片一样，选项以服务端下发的为准；提交只回 tool_call_id 与选择，
 * 问题原文由服务端保留，客户端改不了要回答什么。
 */
import { useState } from 'react';
import type { AnswerItem, PendingQuestion } from '../types';
import { formatClock } from './time';

interface Props {
  questions: PendingQuestion[];
  expiresAt: string | null;
  busy?: boolean;
  onSubmit: (answers: AnswerItem[]) => void;
  onCancel: () => void;
}

export function QuestionCard({ questions, expiresAt, busy, onSubmit, onCancel }: Props) {
  const [picked, setPicked] = useState<Record<string, string[]>>({});
  const [other, setOther] = useState<Record<string, string>>({});
  const deadline = expiresAt ? formatClock(expiresAt) : '';

  function toggle(text: string, label: string, multi: boolean) {
    setPicked((prev) => {
      const current = prev[text] ?? [];
      if (!multi) return { ...prev, [text]: current.includes(label) ? [] : [label] };
      const next = current.includes(label)
        ? current.filter((item) => item !== label)
        : [...current, label];
      return { ...prev, [text]: next };
    });
  }

  const ready = questions.every((item) =>
    item.questions.every((question) => {
      const selected = picked[question.question] ?? [];
      return selected.length > 0 || Boolean((other[question.question] ?? '').trim());
    }),
  );

  function submit() {
    if (!ready) return;
    onSubmit(
      questions.map((item) => ({
        tool_call_id: item.tool_call_id,
        answers: item.questions.map((question) => ({
          question: question.question,
          selected: picked[question.question] ?? [],
          other: (other[question.question] ?? '').trim() || null,
        })),
      })),
    );
  }

  return (
    <div className="question-card" role="group" aria-label="等待你回答的问题">
      <div className="confirm-head">
        <span className="confirm-title">模型在等你回答</span>
        {deadline && <span className="confirm-deadline mono">有效至 {deadline}</span>}
      </div>
      {questions.map((call) => (
        <div key={call.tool_call_id} className="question-group">
          {call.questions.map((question) => {
            const text = question.question;
            const multi = Boolean(question.multi_select);
            return (
              <fieldset key={text} className="question-item">
                <legend className="question-title">
                  {question.header ? `${question.header} · ` : ''}
                  {text}
                </legend>
                {question.context && <p className="question-context">{question.context}</p>}
                <div className="question-options">
                  {question.options.map((option) => {
                    const active = (picked[text] ?? []).includes(option.label);
                    return (
                      <button
                        key={option.label}
                        type="button"
                        className={`question-option${active ? ' active' : ''}`}
                        aria-pressed={active}
                        disabled={busy}
                        onClick={() => toggle(text, option.label, multi)}
                        title={option.description ?? undefined}
                      >
                        <span className="question-label">{option.label}</span>
                        {option.description && (
                          <span className="question-desc">{option.description}</span>
                        )}
                      </button>
                    );
                  })}
                </div>
                <input
                  className="question-other"
                  type="text"
                  placeholder="其他（可留空）"
                  value={other[text] ?? ''}
                  disabled={busy}
                  onChange={(event) =>
                    setOther((prev) => ({ ...prev, [text]: event.target.value }))
                  }
                />
              </fieldset>
            );
          })}
        </div>
      ))}
      <div className="confirm-actions">
        <button type="button" className="ghost-btn" disabled={busy} onClick={onCancel}>
          取消本轮
        </button>
        <button
          type="button"
          className="primary-btn"
          disabled={busy || !ready}
          onClick={submit}
        >
          提交回答
        </button>
      </div>
    </div>
  );
}
