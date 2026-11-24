/**
 * 单次运行的健康分面板 —— 总分、等级、置信度、四维明细。
 *
 * 设计取舍：分数一定配「为什么」。v1 只画四根条，用户只能看到 0 分却不知道为什么，
 * 也无法判断这个分数值不值得信（没评分、没权限判定时分数其实是残缺的）。
 * 这里把 coverage / confidence 放在最显眼的位置，并逐条列出扣分项。
 */
import type { RunHealth } from '../types';
import { DimensionRadar, toneFor } from '../components/HealthCharts';
import { formatDuration } from './time';

interface Props {
  health: RunHealth;
}

/** 百分比显示：null 用「—」，避免 0% 与「无数据」混淆。 */
function pct(value: number | null): string {
  return value === null ? '—' : `${Math.round(value * 100)}%`;
}

export function HealthPanel({ health }: Props) {
  const dimensions = Object.values(health.dimensions);
  const { budget_ms: budget, actual_ms: actual } = health;
  const scale = Math.max(budget, actual, 1);
  const overBudget = budget > 0 && actual > budget;

  return (
    <section className="panel">
      <h2>健康分拆解</h2>
      <div className="health-head">
        <div className="health-score">
          <span className="health-score-value">总分 {health.score}</span>
          <span className={`health-grade grade-${health.grade}`}>{health.grade}</span>
        </div>
        <dl className="health-meta">
          <div>
            <dt>口径</dt>
            <dd className="mono">{health.version}</dd>
          </div>
          <div>
            <dt>覆盖度</dt>
            <dd>{pct(health.coverage)}</dd>
          </div>
          <div>
            <dt>置信度</dt>
            <dd>{pct(health.confidence)}</dd>
          </div>
          <div>
            <dt>耗时预算</dt>
            <dd className="mono">
              {formatDuration(actual)} / {formatDuration(budget)}
            </dd>
          </div>
        </dl>
      </div>

      <div className="health-budget" title="横条长度 = 实际耗时占「实际与预算中较大者」的比例">
        <div className="health-budget-track">
          <div
            className={`health-budget-fill${overBudget ? ' over' : ''}`}
            style={{ width: `${(actual / scale) * 100}%` }}
          />
          <div className="health-budget-mark" style={{ left: `${(budget / scale) * 100}%` }} />
        </div>
        <span className="health-budget-caption hint">
          {budget > 0
            ? `实际 ${formatDuration(actual)}，预算 ${formatDuration(budget)}（${(actual / budget).toFixed(1)}×）${
                overBudget ? ' · 超预算按幂律衰减' : ' · 在预算内'
              }`
            : '没有耗时记录，效率维度不参与评分'}
        </span>
      </div>

      <div className="health-panel-grid">
        <DimensionRadar dimensions={health.dimensions} caption="四维得分（0-100），缺口越大越该先修" />
        <ul className="health-dim-list" aria-label="维度明细">
          {dimensions.map((dimension) => (
            <li className="health-dim-item" key={dimension.key}>
              <div className="health-dim-row">
                <span className="health-dim-name">{dimension.label}</span>
                <span className={`health-dim-value${dimension.value === null ? ' muted' : ''}`}>
                  {dimension.value === null ? '—' : dimension.value}
                </span>
                <span className="health-dim-weight hint">权重 {Math.round(dimension.weight * 100)}%</span>
              </div>
              <div className="health-dim-track">
                <div
                  className="health-dim-fill"
                  style={{
                    width: `${dimension.value ?? 0}%`,
                    background: dimension.value === null ? 'transparent' : toneFor(dimension.value),
                  }}
                />
              </div>
              <p className="health-dim-note hint">
                {dimension.note} · 置信度 {Math.round(dimension.confidence * 100)}%
              </p>
              {dimension.items.length > 0 && (
                <ul className="health-items">
                  {dimension.items.map((item) => (
                    <li key={item.label}>
                      <span>{item.label}</span>
                      <span className={item.delta < 0 ? 'delta-neg mono' : 'delta-zero mono'}>
                        {item.delta === 0 ? '—' : item.delta.toFixed(1)}
                      </span>
                      <span className="hint">{item.detail}</span>
                    </li>
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ul>
      </div>
      <p className="hint">
        缺失维度（无评分 / 无耗时记录）会从加权里剔除，权重按剩余维度重新归一化——
        所以「覆盖度」低于 100% 时，这个总分只代表部分信号。
      </p>
    </section>
  );
}
