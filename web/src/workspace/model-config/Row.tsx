/** 设置面板的一行：左标签 + 中间控件 + 右来源。 */

import type { ReactNode } from 'react';
import type { ModelSource } from '../../types';

/** 来源徽标文案。`framework-default` 不是「默认值」，是「没人设过」。 */
export const SOURCE_LABEL: Record<ModelSource, string> = {
  session: '本会话',
  stored: '部署默认',
  yaml: '配置文件',
  env: '环境变量',
  'framework-default': '未配置',
};

/** 来源徽标：只读项与框架默认各用不同色调，一眼能分出「这行能不能改」。 */
function SourceBadge({ source }: { source: ModelSource }) {
  return <span className={`mc-src is-${source}`}>{SOURCE_LABEL[source] ?? source}</span>;
}

interface RowProps {
  label: string;
  source?: ModelSource;
  hint?: string;
  children: ReactNode;
}

/** 一行：左标签 + 中间控件 + 右来源。 */
export function Row({ label, source, hint, children }: RowProps) {
  return (
    <div className="mc-row">
      <span className="mc-row-label">{label}</span>
      <span className="mc-row-control">{children}</span>
      {source ? <SourceBadge source={source} /> : <span />}
      {hint && <span className="mc-row-hint">{hint}</span>}
    </div>
  );
}
