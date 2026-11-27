/**
 * 记忆效果评测视图 —— 回答「记忆到底有没有用、有没有害、代价多少」。
 *
 * 只读：批次由 CLI 触发；这里展示结论、六赛道门禁、配对增益与红线。
 * 表格用语义化 <table> + caption，外层 region 可键盘滚动（见 responsive-table 指南）。
 */
import { useQuery } from '@tanstack/react-query';
import { fetchMemoryBatches, fetchMemoryCases } from '../../api/client';
import type { MemoryGain, MemorySummary, MemoryTrackRow } from '../../types';

const TRACK_LABELS: Record<string, string> = {
  MT: '多轮记忆',
  MS: '跨会话记忆',
  CP: '压缩保真',
  RT: '检索精度',
  NB: '负例',
  PR: '隐私与隔离',
};

function pct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return '—';
  return `${(value * 100).toFixed(digits)}%`;
}

function numberOrDash(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return '—';
  return value.toFixed(digits);
}

function verdictText(verdict: string): string {
  const labels: Record<string, string> = {
    pass: '通过',
    fail: '未通过',
    insufficient: '样本不足',
    unvalidated: '未证实',
    partial: '局部',
  };
  return labels[verdict] ?? verdict;
}

function verdictClass(verdict: string): string {
  if (verdict === 'pass') return 'badge badge-success';
  if (verdict === 'fail') return 'badge badge-failed';
  return 'badge';
}

function TrackTable({ tracks }: { tracks: Record<string, MemoryTrackRow> }) {
  return (
    <div className="table-scroll" role="region" aria-label="记忆赛道门禁结果" tabIndex={0}>
      <table className="data-table">
        <caption>记忆赛道门禁结果（主变体）</caption>
        <thead>
          <tr>
            <th scope="col">赛道</th>
            <th scope="col">通过</th>
            <th scope="col">TSR</th>
            <th scope="col">探针正确率</th>
            <th scope="col">Recall@5</th>
            <th scope="col">Precision@5</th>
            <th scope="col">MRR</th>
            <th scope="col">红线</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(tracks).map(([track, row]) => (
            <tr key={track}>
              <th scope="row">
                {track} · {TRACK_LABELS[track] ?? track}
              </th>
              <td>
                {row.passed}/{row.total}
              </td>
              <td>{pct(row.tsr)}</td>
              <td>{pct(row.probe_acc)}</td>
              <td>{pct(row.recall_at_5)}</td>
              <td>{pct(row.precision_at_5)}</td>
              <td>{numberOrDash(row.mrr)}</td>
              <td>
                {row.leak_count > 0 || row.redaction_violation > 0
                  ? `泄漏 ${row.leak_count} / 脱敏 ${row.redaction_violation}`
                  : '0'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function gainText(gain: MemoryGain): string {
  if (gain.n === 0) return '没有配对样本';
  const interval = gain.ci ? `CI [${pct(gain.ci[0])}, ${pct(gain.ci[1])}]` : 'CI 不可用';
  const p = gain.p === null ? 'p 不可用' : `McNemar p=${gain.p.toFixed(3)}`;
  return `${gain.full_tsr === null ? '—' : pct(gain.full_tsr)} vs ${gain.off_tsr === null ? '—' : pct(gain.off_tsr)} · ${interval} · ${p}（n=${gain.n}）`;
}

function SummaryPanel({ summary }: { summary: MemorySummary }) {
  const redLine = summary.red_lines;
  return (
    <div className="card">
      <p>
        <span className={verdictClass(summary.verdict)}>{verdictText(summary.verdict)}</span>{' '}
        记忆增益：{' '}
        {['MT', 'MS'].map((track) => {
          const gain = summary.gains[track];
          if (!gain) return null;
          return (
            <span key={track} className="muted">
              {track} {gain.gain_pp === null ? '—' : `${gain.gain_pp}pp`}；{' '}
            </span>
          );
        })}
      </p>
      <p className="muted">
        用例 {summary.kpi.cases} · 运行 {summary.kpi.runs} · 通过 {summary.kpi.passed}（
        {pct(summary.kpi.pass_rate)}）
      </p>
      <p className="muted">
        红线：跨会话泄漏 {redLine.leak_count} · 未脱敏 {redLine.redaction_violation} · 约束保留{' '}
        {redLine.constraint_retention === null ? '—' : pct(redLine.constraint_retention)}
      </p>
      <p className="muted">
        代价：token 开销 p50 {pct(summary.cost.token_overhead_p50)} · p95{' '}
        {pct(summary.cost.token_overhead_p95)} · 延迟 p95{' '}
        {summary.cost.latency_p95_ms === null ? '—' : `${summary.cost.latency_p95_ms}ms`} ·
        记忆体积 {summary.cost.memory_bytes_total}B
      </p>
      {summary.causes.length > 0 && (
        <ul>
          {summary.causes.map((cause) => (
            <li key={`${cause.code}-${cause.message}`} className="muted">
              {cause.message}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function GainsPanel({ gains }: { gains: Record<string, MemoryGain> }) {
  const entries = Object.entries(gains).filter(([track]) => track === 'MT' || track === 'MS');
  if (entries.length === 0) return null;
  return (
    <div className="card">
      <h3>配对增益（full vs off）</h3>
      <ul>
        {entries.map(([track, gain]) => (
          <li key={track}>
            <strong>{track}</strong> {gainText(gain)}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function Memory() {
  const batches = useQuery({
    queryKey: ['memory-batches'],
    queryFn: () => fetchMemoryBatches(20, 0),
  });
  const latest = batches.data?.items[0];
  const cases = useQuery({
    queryKey: ['memory-cases', latest?.id ?? 0],
    queryFn: () => fetchMemoryCases(latest?.id ?? 0, { limit: 50 }),
    enabled: Boolean(latest),
  });

  if (batches.isLoading) return <p className="muted">正在加载记忆批次…</p>;
  if (batches.isError) {
    const message = batches.error instanceof Error ? batches.error.message : '未知错误';
    return <p className="muted">加载记忆批次失败：{message}</p>;
  }
  if (!latest) {
    return (
      <div>
        <p className="muted">还没有记忆评测批次。</p>
        <p className="muted">
          运行 <code>python -m devops_agent.openjudge memory run</code> 生成第一批。
        </p>
      </div>
    );
  }

  return (
    <div className="quality-memory">
      <h2>记忆批次 #{latest.id}</h2>
      <SummaryPanel summary={latest.summary} />
      <GainsPanel gains={latest.summary.gains} />
      <TrackTable tracks={latest.summary.tracks} />
      <div className="table-scroll" role="region" aria-label="记忆用例明细" tabIndex={0}>
        <table className="data-table">
          <caption>记忆用例明细（最近 50 条）</caption>
          <thead>
            <tr>
              <th scope="col">用例</th>
              <th scope="col">赛道</th>
              <th scope="col">变体</th>
              <th scope="col">结果</th>
              <th scope="col">探针</th>
              <th scope="col">延迟</th>
            </tr>
          </thead>
          <tbody>
            {(cases.data?.items ?? []).map((row) => (
              <tr key={row.id}>
                <th scope="row">{row.case_id}</th>
                <td>{row.track}</td>
                <td>{row.variant}</td>
                <td>{row.passed ? '通过' : '未通过'}</td>
                <td>
                  {row.probe_correct}/{row.probe_total}
                </td>
                <td>{row.latency_ms}ms</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
