import { Fragment, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {
  fetchEvalBatch,
  fetchEvalBatches,
  fetchEvalCases,
  fetchEvalCompare,
  fetchEvalMatrix,
} from '../api/client';
import type { EvalBatch, EvalCaseItem, EvalCompareMetric } from '../types';
import { HARD_KIND_KEYS, HARD_KIND_META } from '../types';
import { MetricCard } from '../components/MetricCard';
import { formatDateTime, formatDuration } from '../workspace/time';
import {
  HardFailureChart,
  JudgeRadar,
  PassMatrix,
  type HardBarPoint,
  type JudgePoint,
} from './quality/charts';

const PAGE_SIZE = 50;
const CLI_HINT =
  'python -m devops_agent.eval run --suite eval/suites/devops-core.v2.jsonl --out workspace/evals/<batch>';

const VERDICT_LABEL: Record<string, string> = {
  pass: '通过',
  warn: '警告',
  fail: '失败',
  insufficient: '样本不足',
  running: '运行中',
  not_comparable: '不可比',
};

/** 门禁三态：`insufficient` 不是「未达」，两者必须分开显示。 */
const GATE_STATE_LABEL: Record<string, string> = {
  pass: '通过',
  fail: '未达',
  insufficient: '样本不足',
};

const JUDGE_LABEL: Record<string, string> = {
  correctness: '正确性',
  coherence: '连贯性',
  relevance: '相关性',
  objectivity: '客观性',
  clarity: '清晰度',
  conciseness: '简洁性',
};

/** 硬失败总数：7 类全算。只加 3 类会让其余四类失败在界面上消失。 */
function hardTotal(batch: EvalBatch): number {
  return Object.values(batch.hard_failures ?? {}).reduce((sum, value) => sum + (value ?? 0), 0);
}

/** 按严重度列出有计数的分类，用于横幅文案。 */
function describeKinds(batch: EvalBatch, severity: 'blocking' | 'scoring'): string {
  const parts = HARD_KIND_KEYS.filter(
    (key) => HARD_KIND_META[key].severity === severity && (batch.hard_failures?.[key] ?? 0) > 0,
  ).map((key) => `${HARD_KIND_META[key].label} ${batch.hard_failures[key]}`);
  return parts.length > 0 ? parts.join(' / ') : '—';
}

function formatPercent(value: number | null): string | null {
  return value === null ? null : `${(value * 100).toFixed(1)}%`;
}

function formatScore(value: number | null): string | null {
  return value === null ? null : value.toFixed(2);
}

function formatNumber(value: number | null): string {
  if (value === null) return '—';
  if (Number.isInteger(value)) return String(value);
  return value.toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
}

function formatDelta(value: number | null): string {
  if (value === null) return '—';
  const text = formatNumber(value);
  return value > 0 ? `+${text}` : text;
}

/** better=lower 时 delta 为负才是正向，颜色方向据此反转。 */
function deltaTone(metric: EvalCompareMetric): 'good' | 'bad' | 'flat' {
  if (metric.delta === null || metric.delta === 0) return 'flat';
  const improved = metric.better === 'higher' ? metric.delta > 0 : metric.delta < 0;
  return improved ? 'good' : 'bad';
}

function VerdictBadge({ verdict }: { verdict: string }) {
  return (
    <span className={`badge verdict-badge verdict-${verdict}`}>
      {VERDICT_LABEL[verdict] ?? verdict}
    </span>
  );
}

/** 门禁「不能判」的成因：三种样本不足的处置完全不同，不能只写一句「样本不足」。 */
const GATE_REASON_LABEL: Record<string, string> = {
  n_below_gate: '题库样本不足',
  no_samples: '裁判未运行',
  samples_below_min: '裁判样本不足',
};

/** 门禁三态表：解释「为什么是 insufficient」，不在前端重算阈值。 */
function GatesTable({ batch }: { batch: EvalBatch }) {
  const gates = batch.gates ?? [];
  if (gates.length === 0) {
    return (
      <p className="hint">
        本批未记录门禁（旧版 CLI 产出）。用新版 <code>eval run</code> 重跑即可得到三态门禁表。
      </p>
    );
  }
  return (
    <table className="data-table eval-gates-table">
      <thead>
        <tr>
          <th>门禁</th>
          <th>值</th>
          <th>阈值</th>
          <th>样本</th>
          <th>结论</th>
          <th>原因</th>
        </tr>
      </thead>
      <tbody>
        {gates.map((gate) => (
          <tr key={gate.key}>
            <td>
              <code>{gate.key}</code>
            </td>
            <td>{gate.value === null ? '—' : gate.value.toFixed(3)}</td>
            <td>{gate.min}</td>
            <td>
              {gate.samples}
              {gate.min_samples > 0 ? ` / ${gate.min_samples}` : ''}
            </td>
            <td className={`gate-state gate-${gate.state}`}>
              {GATE_STATE_LABEL[gate.state] ?? gate.state}
            </td>
            <td className="gate-reason">
              {gate.reason ? (GATE_REASON_LABEL[gate.reason] ?? gate.reason) : '—'}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function CaseRow({
  item,
  expanded,
  onToggle,
}: {
  item: EvalCaseItem;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <Fragment>
      <tr className={item.passed ? 'case-row' : 'case-row is-failed'}>
        <td>
          {!item.passed && (
            <button
              type="button"
              className="case-expand"
              aria-label={`展开 ${item.case_id}`}
              onClick={onToggle}
            >
              {expanded ? '▾' : '▸'}
            </button>
          )}
          <code className="case-id">{item.case_id}</code>
        </td>
        <td className="case-tags">
          {item.tags.map((tag) => (
            <span className="tag-chip" key={tag}>{tag}</span>
          ))}
          {item.tags.length === 0 && <span className="muted">—</span>}
        </td>
        <td>
          <span className={`status-pill ${item.passed ? 'is-pass' : 'is-fail'}`}>
            {item.status || (item.passed ? 'passed' : 'failed')}
          </span>
          {/* 三档轨迹：模型没试 / 试了但被拒 / 真的跑了，三者含义完全不同 */}
          {item.denied_tools.length > 0 && (
            <span className="tier-chip is-denied" title={`被权限拒绝：${item.denied_tools.join(', ')}`}>
              被拒 {item.denied_tools.join(',')}
            </span>
          )}
          {item.denied_tools.length === 0 && item.executed_tools.length > 0 && (
            <span
              className="tier-chip is-executed"
              title={`真的执行了：${item.executed_tools.join(', ')}`}
            >
              已执行
            </span>
          )}
          {item.status === 'pending' && <span className="tier-chip is-pending">待确认</span>}
        </td>
        <td className="case-checks">
          {item.hard_failures.map((kind) => {
            const meta = HARD_KIND_META[kind];
            return (
              <span
                key={kind}
                className={`check-chip ${meta?.severity === 'blocking' ? 'bad' : 'warn'}`}
                title={`${meta?.label ?? kind}（${meta?.severity === 'blocking' ? '阻断' : '计分'}）`}
              >
                {meta?.label ?? kind}
              </span>
            );
          })}
          {item.hard_failures.length === 0 &&
            item.checks.map((check) => (
              <span key={check.name} className={`check-chip ${check.ok ? 'ok' : 'bad'}`}>
                {check.ok ? '✓' : '✗'} {check.name}
              </span>
            ))}
          {item.hard_failures.length === 0 && item.checks.length === 0 && (
            <span className="muted">—</span>
          )}
        </td>
        <td>{formatScore(item.trajectory_score) ?? '—'}</td>
        <td className="judge-cell">
          {Object.keys(item.judge).length === 0 && <span className="muted">—</span>}
          {Object.entries(item.judge).map(([dimension, score]) => (
            <span className="judge-mini" key={dimension} title={`${dimension}: ${score}`}>
              <span className="judge-mini-label">{dimension}</span>
              <span className="judge-mini-track">
                <span
                  className="judge-mini-fill"
                  style={{ width: `${Math.max(0, Math.min(1, (score - 1) / 4)) * 100}%` }}
                />
              </span>
            </span>
          ))}
        </td>
        <td>{formatDuration(item.latency_ms)}</td>
        <td>{item.tokens}</td>
      </tr>
      {expanded && (
        <tr className="case-detail-row">
          <td colSpan={8}>
            <ul className="case-check-list">
              {item.checks.map((check, index) => (
                <li key={`${check.name}-${index}`} className={check.ok ? 'ok' : 'bad'}>
                  <span>{check.ok ? '✓' : '✗'}</span>
                  <strong>{check.name}</strong>
                  <span className="muted">{check.detail}</span>
                </li>
              ))}
              {item.checks.length === 0 && <li className="muted">没有检查明细。</li>}
            </ul>
          </td>
        </tr>
      )}
    </Fragment>
  );
}

export function EvalBatches() {
  const [selected, setSelected] = useState<number | null>(null);
  const [baseSel, setBaseSel] = useState<number | null>(null);
  const [headSel, setHeadSel] = useState<number | null>(null);
  const [caseStatus, setCaseStatus] = useState('');
  const [caseTag, setCaseTag] = useState('');
  const [caseOffset, setCaseOffset] = useState(0);
  const [search, setSearch] = useState('');
  const [expanded, setExpanded] = useState<string | null>(null);

  const batchesQuery = useQuery({
    queryKey: ['eval-batches'],
    queryFn: () => fetchEvalBatches(20, 0),
    refetchInterval: (query) => {
      const items = query.state.data?.items ?? [];
      return items.some((item) => item.status === 'running') ? 5000 : false;
    },
  });

  const detail = useQuery({
    queryKey: ['eval-batch', selected],
    queryFn: () => fetchEvalBatch(selected as number),
    enabled: selected !== null,
  });

  const cases = useQuery({
    queryKey: ['eval-cases', selected, caseStatus, caseTag, caseOffset],
    queryFn: () =>
      fetchEvalCases(selected as number, {
        status: caseStatus || undefined,
        tag: caseTag || undefined,
        limit: PAGE_SIZE,
        offset: caseOffset,
      }),
    enabled: selected !== null,
  });

  const batches = useMemo(() => batchesQuery.data?.items ?? [], [batchesQuery.data]);
  const sortedBatches = useMemo(() => [...batches].sort((a, b) => b.id - a.id), [batches]);
  const defaultBase = sortedBatches[1]?.id ?? null;
  const defaultHead = sortedBatches[0]?.id ?? null;
  const baseId = baseSel ?? defaultBase;
  const headId = headSel ?? defaultHead;
  const canCompare = baseId !== null && headId !== null && baseId !== headId;

  const compare = useQuery({
    queryKey: ['eval-compare', baseId, headId],
    queryFn: () => fetchEvalCompare(baseId as number, headId as number),
    enabled: canCompare,
  });

  const matrix = useQuery({
    queryKey: ['eval-matrix'],
    queryFn: () => fetchEvalMatrix(10),
  });

  const batch = detail.data?.batch ?? null;
  const tagStats = useMemo(() => detail.data?.tags ?? {}, [detail.data]);
  const tagData = useMemo(
    () =>
      Object.entries(tagStats).map(([tag, stat]) => ({
        tag,
        rate: stat.n > 0 ? stat.passed / stat.n : 0,
        n: stat.n,
      })),
    [tagStats],
  );

  const visibleCases = (cases.data?.items ?? []).filter((item) =>
    item.case_id.toLowerCase().includes(search.trim().toLowerCase()),
  );
  const casesTotal = cases.data?.total ?? 0;
  const changedCases = compare.data?.cases.filter(
    (item) => item.base_passed !== item.head_passed,
  ) ?? [];

  function selectBatch(id: number) {
    setSelected(id);
    setCaseStatus('');
    setCaseTag('');
    setCaseOffset(0);
    setSearch('');
    setExpanded(null);
  }

  function updateStatus(value: string) {
    setCaseStatus(value);
    setCaseOffset(0);
  }

  function updateTag(value: string) {
    setCaseTag(value);
    setCaseOffset(0);
  }

  const hard = batch ? hardTotal(batch) : 0;
  // judge 六维：只放有样本的维度 —— 补 0 会把「没测」画成「很差」。
  const judgePoints: JudgePoint[] = Object.entries(batch?.judge ?? {})
    .filter(([, value]) => typeof value === 'number')
    .map(([key, value]) => ({ dimension: JUDGE_LABEL[key] ?? key, score: value }));
  const hardPoints: HardBarPoint[] = [...sortedBatches]
    .slice(0, 10)
    .reverse()
    .map((item) => ({
      label: `#${item.id}`,
      // 7 类全传：只画 3 类会让其余失败在图里消失
      values: item.hard_failures,
    }));
  // 门槛来自后端，不在前端硬编码 —— 两套阈值会互相打脸
  const sampleGate = batch?.min_cases_for_gate ?? 40;
  const enough = batch ? batch.n >= sampleGate : false;
  const sampleBadge = enough ? undefined : '样本不足';
  const rateTone: 'default' | 'good' | 'bad' = enough
    ? batch && batch.pass_rate >= 0.9
      ? 'good'
      : batch && batch.pass_rate < 0.8
        ? 'bad'
        : 'default'
    : 'default';
  const correctness = batch?.judge?.correctness ?? null;
  // 裁判分是 1–5：≥4.0 绿、<3.5 红
  const correctnessTone: 'default' | 'good' | 'bad' = enough && correctness !== null
    ? correctness >= 4.0
      ? 'good'
      : correctness < 3.5
        ? 'bad'
        : 'default'
    : 'default';

  return (
    <div className="eval-batches">
      <section className="panel">
        <h2>评测批次</h2>
        <p className="hint">只读视图 · 选中批次查看用例明细，下方可对比两次批次</p>

        {batchesQuery.isError && (
          <div className="eval-error">
            <p className="query-error">评测批次加载失败。</p>
            <button type="button" className="primary-btn" onClick={() => void batchesQuery.refetch()}>
              重试
            </button>
          </div>
        )}
        {batchesQuery.isLoading && (
          <p className="query-loading">正在加载评测批次…</p>
        )}
        {!batchesQuery.isLoading && !batchesQuery.isError && batches.length === 0 && (
          <div className="eval-empty">
            <p>还没有评测批次</p>
            <code className="cli-hint">{CLI_HINT}</code>
          </div>
        )}

        {batches.length > 0 && (
          <table className="data-table eval-batch-table">
            <thead>
              <tr>
                <th>批次</th>
                <th>时间</th>
                <th>suite@version</th>
                <th>prompt</th>
                <th>n</th>
                <th>通过率</th>
                <th>硬门槛</th>
                <th>verdict</th>
              </tr>
            </thead>
            <tbody>
              {batches.map((item) => (
                <tr
                  key={item.id}
                  className={`eval-row${selected === item.id ? ' is-selected' : ''}`}
                  onClick={() => selectBatch(item.id)}
                >
                  <td>#{item.id}</td>
                  <td className="muted">{formatDateTime(item.started_at)}</td>
                  <td>{item.suite}@{item.suite_version}</td>
                  <td className="muted">
                    {item.prompt_id} · {item.prompt_hash.slice(0, 8)}
                  </td>
                  <td>{item.n}</td>
                  <td>{formatPercent(item.pass_rate) ?? '—'}</td>
                  <td className={item.hard_blocking > 0 ? 'hard-fail' : 'hard-ok'}>
                    {item.hard_blocking > 0 ? `阻断 ${item.hard_blocking}` : '✓'}
                    {item.hard_scoring > 0 && (
                      <span className="hard-scoring-note"> · 计分 {item.hard_scoring}</span>
                    )}
                  </td>
                  <td><VerdictBadge verdict={item.verdict} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {batches.length > 0 && (
          <p className="hint">共 {batchesQuery.data?.total ?? batches.length} 个批次</p>
        )}
      </section>

      {selected !== null && detail.isError && (
        <section className="panel">
          <p className="query-error">批次详情加载失败。</p>
          <button type="button" className="primary-btn" onClick={() => void detail.refetch()}>
            重试
          </button>
        </section>
      )}
      {selected !== null && detail.isLoading && (
        <section className="panel">
          <p className="query-loading">正在加载批次详情…</p>
        </section>
      )}

      {batch && detail.data && (
        <Fragment>
          <section className="panel eval-detail">
            {/* 阻断类才拦合并；只有计分失败时要说清「已计入通过率」，
                否则读者会把「不计分」误读成「没有失败」 */}
            <div
              className={`hard-gate-banner ${batch.hard_blocking > 0 ? 'is-bad' : hard > 0 ? 'is-warn' : 'is-ok'}`}
              role="status"
            >
              {batch.hard_blocking > 0
                ? `不建议合并：阻断性失败 ${batch.hard_blocking} 条（${describeKinds(batch, 'blocking')}）`
                : hard > 0
                  ? `无阻断性失败；计分失败 ${batch.hard_scoring} 条已计入通过率（${describeKinds(batch, 'scoring')}）`
                  : '全部硬失败分类均为 0'}
            </div>

            <div className="metrics-grid eval-metrics">
              <MetricCard
                label="通过率"
                value={formatPercent(batch.pass_rate)}
                hint={`${batch.passed}/${batch.n} 通过`}
                tone={rateTone}
                badge={sampleBadge}
              />
              <MetricCard
                label="正确性"
                value={correctness === null ? null : `${correctness.toFixed(2)} / 5`}
                tone={correctnessTone}
                badge={sampleBadge}
              />
              <MetricCard
                label="轨迹分"
                value={formatScore(batch.trajectory_score)}
                badge={sampleBadge}
              />
              <MetricCard
                label="P95"
                value={batch.p95_ms === null ? null : formatDuration(batch.p95_ms)}
                badge={sampleBadge}
              />
              <MetricCard
                label="单例 token"
                value={batch.n > 0 ? Math.round(batch.tokens_total / batch.n) : null}
                badge={sampleBadge}
              />
              <MetricCard label="样本 n" value={batch.n} />
            </div>

            <p className="hint">
              批次 #{batch.id} · {batch.suite}@{batch.suite_version} · {batch.model} / {batch.profile}
              {batch.finished_at ? ` · 完成于 ${formatDateTime(batch.finished_at)}` : ''}
            </p>

            <h3>门禁</h3>
            <GatesTable batch={batch} />
          </section>

          <section className="panel">
            <h2>分标签通过率</h2>
            {tagData.length === 0 && <p className="muted">暂无标签数据。</p>}
            {tagData.length > 0 && (
              <div className="chart-wrap">
                <ResponsiveContainer width="100%" height={240}>
                  <BarChart data={tagData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#e5e5e5" />
                    <XAxis dataKey="tag" fontSize={12} />
                    <YAxis
                      domain={[0, 1]}
                      fontSize={12}
                      tickFormatter={(value: number) => `${Math.round(value * 100)}%`}
                    />
                    <Tooltip
                      content={({ active, payload }) => {
                        if (!active || !payload || payload.length === 0) return null;
                        const point = payload[0].payload as { tag: string; rate: number; n: number };
                        return (
                          <div className="chart-tooltip">
                            {point.tag}：{(point.rate * 100).toFixed(1)}%（n={point.n}）
                          </div>
                        );
                      }}
                    />
                    <Bar dataKey="rate" name="通过率" fill="#3b82f6" radius={[4, 4, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}
          </section>

          <section className="panel">
            <h2>质量维度</h2>
            <JudgeRadar
              points={judgePoints}
              caption={
                judgePoints.length === 0
                  ? '本批没有启用裁判，没有维度分。'
                  : `judge 均分：${judgePoints.map((item) => `${item.dimension} ${item.score.toFixed(2)}`).join('，')}（1–5 分）。`
              }
            />
          </section>

          <section className="panel">
            <h2>硬失败构成</h2>
            <HardFailureChart
              points={hardPoints}
              caption="按批次堆叠：区分「安全越界」「用了禁用工具」与「格式没对上」，三类处理方式不同。"
            />
          </section>

          <section className="panel">
            <h2>用例</h2>
            <div className="filters eval-case-filters">
              <label>
                状态：
                <select
                  aria-label="用例状态"
                  value={caseStatus}
                  onChange={(event) => updateStatus(event.target.value)}
                >
                  <option value="">全部</option>
                  <option value="failed">失败</option>
                </select>
              </label>
              <label>
                标签：
                <select
                  aria-label="用例标签"
                  value={caseTag}
                  onChange={(event) => updateTag(event.target.value)}
                >
                  <option value="">全部</option>
                  {Object.keys(tagStats).map((tag) => (
                    <option key={tag} value={tag}>{tag}</option>
                  ))}
                </select>
              </label>
              <label>
                搜索：
                <input
                  aria-label="搜索用例"
                  value={search}
                  placeholder="case_id"
                  onChange={(event) => setSearch(event.target.value)}
                />
              </label>
              <span className="muted">共 {casesTotal} 条</span>
            </div>

            {cases.isError && <p className="query-error">用例加载失败。</p>}
            {cases.isLoading && <p className="query-loading">正在加载用例…</p>}
            {!cases.isLoading && !cases.isError && visibleCases.length === 0 && (
              <p className="muted">没有匹配的用例。</p>
            )}
            {visibleCases.length > 0 && (
              <table className="data-table eval-case-table">
                <thead>
                  <tr>
                    <th>case_id</th>
                    <th>标签</th>
                    <th>状态</th>
                    <th>规则检查</th>
                    <th>轨迹分</th>
                    <th>裁判</th>
                    <th>延迟</th>
                    <th>token</th>
                  </tr>
                </thead>
                <tbody>
                  {visibleCases.map((item) => (
                    <CaseRow
                      key={item.case_id}
                      item={item}
                      expanded={expanded === item.case_id}
                      onToggle={() => setExpanded(expanded === item.case_id ? null : item.case_id)}
                    />
                  ))}
                </tbody>
              </table>
            )}

            <div className="pagination">
              <button
                type="button"
                disabled={caseOffset === 0}
                onClick={() => setCaseOffset(Math.max(0, caseOffset - PAGE_SIZE))}
              >
                上一页
              </button>
              <span className="muted">
                {casesTotal === 0 ? '0' : `${caseOffset + 1}–${Math.min(caseOffset + PAGE_SIZE, casesTotal)}`} / {casesTotal}
              </span>
              <button
                type="button"
                disabled={caseOffset + PAGE_SIZE >= casesTotal}
                onClick={() => setCaseOffset(caseOffset + PAGE_SIZE)}
              >
                下一页
              </button>
            </div>
          </section>
        </Fragment>
      )}

      <section className="panel">
        <h2>用例稳定性矩阵</h2>
        <p className="hint">
          每一列是一个批次、每一行是一条用例。看的是「整体退化」还是「某条一直挂」。
        </p>
        {matrix.isError && <p className="query-error">矩阵加载失败，稍后自动重试。</p>}
        {matrix.isLoading && <p className="query-loading">正在加载矩阵…</p>}
        {matrix.data && (
          <PassMatrix
            data={matrix.data}
            caption={`最近 ${matrix.data.batches.length} 个批次 × ${matrix.data.cases.length} 条用例；绿色通过、红色失败、灰色未运行。`}
          />
        )}
      </section>

      <section className="panel">
        <h2>批次对比</h2>
        {sortedBatches.length === 0 && <p className="muted">暂无可对比的批次。</p>}
        {sortedBatches.length > 0 && (
          <div className="filters compare-pickers">
            <label>
              基线 base：
              <select
                aria-label="基线批次"
                value={baseId ?? ''}
                onChange={(event) => setBaseSel(event.target.value ? Number(event.target.value) : null)}
              >
                {sortedBatches.map((item) => (
                  <option key={item.id} value={item.id}>
                    #{item.id} {item.suite}@{item.suite_version}
                  </option>
                ))}
              </select>
            </label>
            <label>
              对比 head：
              <select
                aria-label="对比批次"
                value={headId ?? ''}
                onChange={(event) => setHeadSel(event.target.value ? Number(event.target.value) : null)}
              >
                {sortedBatches.map((item) => (
                  <option key={item.id} value={item.id}>
                    #{item.id} {item.suite}@{item.suite_version}
                  </option>
                ))}
              </select>
            </label>
          </div>
        )}
        {sortedBatches.length === 1 && <p className="muted">至少需要两个批次才能对比。</p>}
        {sortedBatches.length >= 2 && !canCompare && (
          <p className="muted">请选择两个不同的批次。</p>
        )}

        {canCompare && compare.isError && <p className="query-error">对比加载失败。</p>}
        {canCompare && compare.isLoading && <p className="query-loading">正在加载对比…</p>}
        {compare.data && (
          <Fragment>
            <div className={`compare-banner verdict-${compare.data.verdict}`} role="status">
              对比结论：{VERDICT_LABEL[compare.data.verdict] ?? compare.data.verdict}
            </div>

            <p className="hint">
              精度上限 {((compare.data.mde ?? 0) * 100).toFixed(1)}pp：n=
              {Math.min(compare.data.n, compare.data.base_n)}，小于该值的变化视为抖动。
              {compare.data.compatibility?.level !== 'clean' && (
                <>
                  {' '}
                  可比性：
                  <strong>
                    {compare.data.compatibility?.level === 'blocking'
                      ? '不可比'
                      : compare.data.compatibility?.level === 'confounded'
                        ? '混杂'
                        : '未知'}
                  </strong>
                  {compare.data.compatibility?.reasons?.[0]
                    ? ` —— ${compare.data.compatibility.reasons[0]}`
                    : ''}
                </>
              )}
            </p>

            <table className="data-table eval-compare-table">
              <thead>
                <tr>
                  <th>指标</th>
                  <th>base</th>
                  <th>head</th>
                  <th>delta</th>
                  <th>方向</th>
                </tr>
              </thead>
              <tbody>
                {compare.data.metrics.map((metric) => (
                  <tr key={metric.key}>
                    <td>{metric.key}</td>
                    <td>{formatNumber(metric.base)}</td>
                    <td>{formatNumber(metric.head)}</td>
                    <td className={`delta-${deltaTone(metric)}`}>{formatDelta(metric.delta)}</td>
                    <td className="muted">
                      {metric.better === 'lower' ? '越低越好' : '越高越好'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            <h3 className="compare-cases-title">变化用例（{changedCases.length}）</h3>
            <ul className="compare-cases">
              {changedCases.map((item) => (
                <li
                  key={item.case_id}
                  className={item.head_passed ? 'is-fixed' : 'is-regressed'}
                >
                  <code>{item.case_id}</code>
                  <span>
                    {item.base_passed ? '通过' : '失败'} → {item.head_passed ? '通过' : '失败'}
                  </span>
                </li>
              ))}
              {changedCases.length === 0 && (
                <li className="muted">没有通过状态变化的用例。</li>
              )}
            </ul>
          </Fragment>
        )}
      </section>
    </div>
  );
}
