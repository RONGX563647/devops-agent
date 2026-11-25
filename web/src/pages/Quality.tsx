import { useState } from 'react';
import { Overview } from './quality/Overview';
import { OpenJudge } from './quality/OpenJudge';

/** 两个视图：「这批能不能用」→「发起一次评测并看逐条明细」。 */
type QualityView = 'overview' | 'run';

const VIEWS: { key: QualityView; label: string }[] = [
  { key: 'overview', label: '概览' },
  { key: 'run', label: '运行与批次' },
];

export function Quality() {
  const [view, setView] = useState<QualityView>('overview');

  return (
    <div className="page">
      <h1>质量评估</h1>
      <p className="subtitle">先看这批能不能用，再发起一次评测看分数是从哪来的</p>

      <div className="view-tabs" role="tablist" aria-label="质量视图">
        {VIEWS.map((item) => (
          <button
            key={item.key}
            type="button"
            role="tab"
            aria-selected={view === item.key}
            className={`view-tab${view === item.key ? ' active' : ''}`}
            onClick={() => setView(item.key)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {view === 'overview' && <Overview onShowBatches={() => setView('run')} />}
      {view === 'run' && <OpenJudge />}
    </div>
  );
}
