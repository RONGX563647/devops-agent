interface MetricCardProps {
  label: string;
  value: string | number | null;
  hint?: string;
  tone?: 'default' | 'good' | 'bad';
}

export function MetricCard({ label, value, hint, tone = 'default' }: MetricCardProps) {
  const display = value === null || value === undefined ? '—' : value;
  return (
    <div className={`metric-card tone-${tone}`}>
      <div className="metric-label">{label}</div>
      <div className="metric-value">{display}</div>
      {hint && <div className="metric-hint">{hint}</div>}
    </div>
  );
}
