import { useState } from 'react';
import { Overview } from './quality/Overview';
import { SuitePanel } from './quality/SuitePanel';
import { EvalBatches } from './EvalBatches';

/** 三个视图：「能不能用」→「拿什么测的」→「逐条明细」。 */
type QualityView = 'overview' | 'suite' | 'batches';

const VIEWS: { key: QualityView; label: string }[] = [
  { key: 'overview', label: '概览' },
  { key: 'suite', label: '评测集' },
  { key: 'batches', label: '评测批次' },
];

export function Quality() {
  const [view, setView] = useState<QualityView>('overview');

  return (
    <div className="page">
      <h1>质量评估</h1>
      <p className="subtitle">结论 → 题库 → 明细：先看这批能不能用，再看分数是从哪来的</p>

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

      {view === 'overview' && <Overview onShowBatches={() => setView('batches')} />}
      {view === 'suite' && <SuitePanel />}
      {view === 'batches' && <EvalBatches />}
    </div>
  );
}
