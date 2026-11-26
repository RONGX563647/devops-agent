/** 搜索命中的高亮片段与步骤匹配判定。 */

import type { TraceStep } from '../../types';

/** 行首徽标：按步骤类别给一个中文短名。 */
export const BADGE: Record<TraceStep['kind'], string> = {
  tool: '工具',
  text: '思考',
  model: '模型',
  error: '异常',
  parse: '解析',
  memory: '记忆',
};

/** 搜索命中高亮：只标出命中片段，不改变行高。 */
export function Highlight({ text, query }: { text: string; query: string }) {
  if (!query) return <>{text}</>;
  const lower = text.toLowerCase();
  const needle = query.toLowerCase();
  const parts: (string | { hit: string })[] = [];
  let cursor = 0;
  for (let at = lower.indexOf(needle); at >= 0; at = lower.indexOf(needle, cursor)) {
    if (at > cursor) parts.push(text.slice(cursor, at));
    parts.push({ hit: text.slice(at, at + needle.length) });
    cursor = at + needle.length;
  }
  if (cursor < text.length) parts.push(text.slice(cursor));
  return (
    <>
      {parts.map((part, i) =>
        typeof part === 'string' ? (
          <span key={i}>{part}</span>
        ) : (
          <mark key={i}>{part.hit}</mark>
        ),
      )}
    </>
  );
}

/** 这一条是否命中搜索：标题、入参、返回、正文都算。 */
export function matchesStep(step: TraceStep, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return [step.title, step.args, step.result, step.text, step.toolName]
    .join(' ')
    .toLowerCase()
    .includes(needle);
}
