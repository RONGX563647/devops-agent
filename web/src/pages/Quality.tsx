import { useState } from 'react';
import { Overview } from './quality/Overview';
import { OpenJudge } from './quality/OpenJudge';
import { Studio } from './quality/studio/Studio';

/** 三个视图：「这批能不能用」→「发起一次评测看明细」→「配模型、改用例」。 */
type QualityView = 'overview' | 'run' | 'studio';

const VIEWS: { key: QualityView; label: string }[] = [
  { key: 'overview', label: '概览' },
  { key: 'run', label: '运行与批次' },
  { key: 'studio', label: '用例与模型' },
];

export function Quality() {
  const [view, setView] = useState<QualityView>('overview');

  return (
    <div className="page quality-page">
      <h1>质量评估</h1>
      <p className="subtitle">先看这批能不能用，再看分数从哪来 —— 题和模型都能在这里直接改</p>

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
      {view === 'studio' && <Studio />}
    </div>
  );
}
