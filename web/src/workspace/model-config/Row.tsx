/** 来源徽标：一个参数值来自哪一层。
 *
 * `framework-default` 不是「默认值」，是「没人设过」——用虚边 + 淡色表示，
 * 正是 context_size 猜了半年没被发现的根因。`pending` 表示草稿还没保存，
 * 这时不能谎称它已经是本会话的值。
 */

import type { ModelSource } from '../../types';

/** 来源徽标文案。 */
export const SOURCE_LABEL: Record<ModelSource, string> = {
  session: '本会话',
  stored: '部署默认',
  yaml: '配置文件',
  env: '环境变量',
  'framework-default': '未配置',
};

export function SourceBadge({ source, pending }: { source: ModelSource; pending?: boolean }) {
  return (
    <span className={`mc-src is-${pending ? 'pending' : source}`}>
      {pending ? '待保存' : (SOURCE_LABEL[source] ?? source)}
    </span>
  );
}
