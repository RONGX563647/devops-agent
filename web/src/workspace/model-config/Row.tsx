/** 来源徽标：一个参数值来自哪一层。
 *
 * `framework-default` 不是「默认值」，是「没人设过」——用虚边 + 淡色表示，
 * 正是 context_size 猜了半年没被发现的根因。`pending` 表示草稿还没保存，
 * 这时不能谎称它已经是本会话的值。
 *
 * 文案取自 `capability.ts`：能力合并带来了三个新来源（校准表 / 网关声明 /
 * 内置卡），后端将来再加来源时这里显示「未知来源」而不是空白（A44）。
 */

import type { ModelSource } from '../../types';
import { sourceHint, sourceLabel } from './capability';

export function SourceBadge({ source, pending }: { source: ModelSource; pending?: boolean }) {
  return (
    <span
      className={`mc-src is-${pending ? 'pending' : source}`}
      title={pending ? '草稿改动，保存后才生效' : sourceHint(source)}
    >
      {pending ? '待保存' : sourceLabel(source)}
    </span>
  );
}
