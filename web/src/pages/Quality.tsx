/**
 * 质量页外壳：四个视图，全部由 URL 决定。
 *
 * 视图切换用导航链接而不是 `role="tab"`：这是在换页面内容（各自有独立的
 * 查询与地址栏状态），不是同一份数据的分页签。ARIA tabs 模式明确不适用于
 * 页面导航；用 `<a>` + `aria-current` 才能让「复制链接 / 前进后退 / 新标签」都正常。
 *
 * 旧地址 `?focus=scoreflow` 仍然有效：按评分流程渲染，切换视图时由 `view`
 * 取代（见 `quality/view.ts` 的 `viewHref`），已分享出去的链接不会失效。
 */

import { useMemo } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { OpenJudge } from './quality/OpenJudge';
import { Overview } from './quality/Overview';
import { Memory } from './quality/Memory';
import { Studio } from './quality/studio/Studio';
import { VIEWS, parseView, viewHref } from './quality/view';

export type { QualityView } from './quality/view';

export function Quality() {
  const [params] = useSearchParams();
  const view = useMemo(
    () => parseView(params.get('view'), params.get('focus')),
    [params],
  );

  return (
    <div className="page quality-page">
      <h1>质量评估</h1>
      <p className="subtitle">
        先看这批能不能用，再看分数从哪来 —— 题和模型都能在这里直接改
      </p>

      <nav className="quality-views" aria-label="质量视图">
        {VIEWS.map((item) => (
          <Link
            key={item.key}
            to={viewHref(params, item.key)}
            replace
            className={`quality-view-link${view === item.key ? ' active' : ''}`}
            aria-current={view === item.key ? 'page' : undefined}
          >
            {item.label}
            <span className="muted">{item.hint}</span>
          </Link>
        ))}
      </nav>

      {view === 'overview' && <Overview />}
      {view === 'batches' && <OpenJudge />}
      {view === 'scoreflow' && <OpenJudge focusScoreFlow />}
      {view === 'studio' && <Studio />}
      {view === 'memory' && <Memory />}
    </div>
  );
}
