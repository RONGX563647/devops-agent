interface MetricCardProps {
  label: string;
  value: string | number | null;
  hint?: string;
  tone?: 'default' | 'good' | 'bad';
  /** 右上角徽标，例如样本不足提示。 */
  badge?: string;
}

export function MetricCard({ label, value, hint, tone = 'default', badge }: MetricCardProps) {
  const display = value === null || value === undefined ? '—' : value;
  return (
    <div className={`metric-card tone-${tone}`}>
      {badge && <span className="metric-badge">{badge}</span>}
      <div className="metric-label">{label}</div>
      <div className="metric-value">{display}</div>
      {hint && <div className="metric-hint">{hint}</div>}
    </div>
  );
}
