/** OpenJudge 评测视图：批次结论 → 赛道 → 失败用例 → 覆盖/对比/rubric。 */

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  fetchOpenJudgeBatch,
  fetchOpenJudgeBatches,
  fetchOpenJudgeCase,
  fetchOpenJudgeCases,
  fetchOpenJudgeCompare,
  fetchOpenJudgeCoverage,
  fetchOpenJudgeRubrics,
} from '../../api/client';
import { MetricCard } from '../../components/MetricCard';
import type { OjBatch, OjCaseRow, OjTrackStat } from '../../types';
import { formatPercent as percent } from './format';
import { TRACK_LABEL, VERDICT_TEXT, verdictClass } from './labels';
import { RunBar } from './run/RunBar';
import { SuitesCard } from './run/SuitesCard';

function TrackRow({ track, stat }: { track: string; stat: OjTrackStat }) {
  return (
    <tr>
      <td>{TRACK_LABEL[track] ?? track}</td>
      <td>{stat.n}</td>
      <td>{stat.passed}</td>
      <td>{percent(stat.pass_rate)}</td>
      <td>{stat.judge_avg === null ? '—' : stat.judge_avg.toFixed(2)}</td>
      <td>{stat.violations}</td>
      <td>
        <span className={`status-pill ${verdictClass(stat.verdict)}`}>
          {VERDICT_TEXT[stat.verdict] ?? stat.verdict}
        </span>
      </td>
    </tr>
  );
}

function CaseDrawer({ rowId }: { rowId: number }) {
  const detail = useQuery({
    queryKey: ['oj-case', rowId],
    queryFn: () => fetchOpenJudgeCase(rowId),
  });
  if (detail.isLoading) return <div className="muted">加载用例明细…</div>;
  if (detail.error || !detail.data) return <div className="muted">用例明细不可用</div>;
  const item = detail.data;
  const failed = item.checks.filter((check) => !check.passed);
  return (
    <div className="oj-drawer">
      <h3>
        <code>{item.case_id}</code>{' '}
        <span className={`status-pill ${item.passed ? 'is-pass' : 'is-fail'}`}>
          {item.status}
        </span>
      </h3>
      <div className="muted">
        赛道 {item.track} · 主题 {item.subject} · 风险 {item.risk} · 耗时 {item.latency_ms}ms
      </div>
      <h4>规则失败项</h4>
      {failed.length === 0 ? (
        <p className="muted">全部规则通过</p>
      ) : (
        <ul>
          {failed.map((check) => (
            <li key={check.name}>
              <code>{check.name}</code> {check.detail}
            </li>
          ))}
        </ul>
      )}
      <h4>裁判意见</h4>
      <ul>
        {Object.entries(item.judge).map(([key, value]) => (
          <li key={key}>
            <strong>{key}</strong> {value.score ?? '—'}（归一 {value.normalized ?? '—'}）{' '}
            <span className="muted">{value.reason ?? ''}</span>
          </li>
        ))}
        {Object.keys(item.judge).length === 0 && <li className="muted">未跑语义裁判</li>}
      </ul>
      {item.events.length > 0 && (
        <>
          <h4>事件</h4>
          <div className="tag-row">
            {item.events.map((event) => (
              <span className="tag-chip" key={event}>
                {event}
              </span>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

export function OpenJudge() {
  const batchesQuery = useQuery({
    queryKey: ['oj-batches'],
    queryFn: () => fetchOpenJudgeBatches(20),
  });
  const list: OjBatch[] = batchesQuery.data?.items ?? [];
  const [selected, setSelected] = useState<number | null>(null);
  const activeId = selected ?? list[0]?.id ?? null;

  const batchQuery = useQuery({
    queryKey: ['oj-batch', activeId],
    queryFn: () => fetchOpenJudgeBatch(activeId as number),
    enabled: activeId !== null,
  });
  const [track, setTrack] = useState('');
  const [onlyFailed, setOnlyFailed] = useState(false);
  const [offset, setOffset] = useState(0);
  const [openCase, setOpenCase] = useState<number | null>(null);

  const casesQuery = useQuery({
    queryKey: ['oj-cases', activeId, track, onlyFailed, offset],
    queryFn: () =>
      fetchOpenJudgeCases(activeId as number, {
        track: track || undefined,
        passed: onlyFailed ? false : undefined,
        limit: 50,
        offset,
      }),
    enabled: activeId !== null,
  });

  const [base, setBase] = useState<number | null>(null);
  const [head, setHead] = useState<number | null>(null);
  const compareEnabled = base !== null && head !== null && base !== head;
  const compareQuery = useQuery({
    queryKey: ['oj-compare', base, head],
    queryFn: () => fetchOpenJudgeCompare(base as number, head as number),
    enabled: compareEnabled,
  });

  const coverageQuery = useQuery({
    queryKey: ['oj-coverage'],
    queryFn: fetchOpenJudgeCoverage,
  });
  const rubricsQuery = useQuery({ queryKey: ['oj-rubrics'], queryFn: fetchOpenJudgeRubrics });

  const batch = batchQuery.data;
  const summary = batch?.summary;
  const tracks = summary?.tracks ?? {};
  const rows: OjCaseRow[] = casesQuery.data?.items ?? [];
  const total = casesQuery.data?.total ?? 0;
  const coverage = coverageQuery.data?.coverage ?? {};
  const trackOptions = Object.keys(tracks).sort();
  const empty = !batchesQuery.isLoading && list.length === 0;

  return (
    <div className="oj-view">
      <RunBar
        onFinished={(batchId) => {
          setSelected(batchId);
          setOffset(0);
          setOpenCase(null);
        }}
      />
      <SuitesCard />

      {batchesQuery.isLoading && <div className="muted">加载 OpenJudge 批次…</div>}
      {empty && (
        <div className="panel">
          <h2>尚无批次</h2>
          <p className="muted">
            在上方发起一次评测，跑完后这里会出现批次结论与逐条明细。
          </p>
        </div>
      )}

      {list.length > 0 && (
        <>
          <div className="panel">
            <h2>批次</h2>
        <div className="tag-row">
          {list.map((item) => (
            <button
              key={item.id}
              type="button"
              className={`view-tab${item.id === activeId ? ' active' : ''}`}
              onClick={() => {
                setSelected(item.id);
                setOffset(0);
                setOpenCase(null);
              }}
            >
              #{item.id} · {VERDICT_TEXT[item.verdict] ?? item.verdict} · {item.model || 'unknown'}
            </button>
          ))}
        </div>
      </div>

      {summary && (
        <>
          <div className="panel-grid">
            <MetricCard
              label="用例"
              value={summary.kpi.cases}
              hint={`通过 ${summary.kpi.passed}`}
            />
            <MetricCard
              label="通过率"
              value={percent(summary.kpi.cases ? summary.kpi.passed / summary.kpi.cases : 0)}
              tone={summary.verdict === 'pass' ? 'good' : 'bad'}
            />
            <MetricCard label="安全违规" value={summary.kpi.violations} tone={summary.kpi.violations > 0 ? 'bad' : 'good'} />
            <MetricCard label="已判分" value={summary.kpi.judge_n} hint="送入语义裁判的用例数" />
          </div>

          <div className="panel">
            <h2>
              赛道 <span className={`status-pill ${verdictClass(summary.verdict)}`}>
                {VERDICT_TEXT[summary.verdict] ?? summary.verdict}
              </span>
            </h2>
            <table className="data-table">
              <thead>
                <tr>
                  <th>赛道</th>
                  <th>用例</th>
                  <th>通过</th>
                  <th>通过率</th>
                  <th>裁判均分</th>
                  <th>违规</th>
                  <th>结论</th>
                </tr>
              </thead>
              <tbody>
                {trackOptions.map((key) => (
                  <TrackRow key={key} track={key} stat={tracks[key]} />
                ))}
              </tbody>
            </table>
          </div>

          <div className="panel-grid">
            <div className="panel">
              <h2>失败用例</h2>
              {summary.failures.length === 0 ? (
                <p className="muted">本批次没有失败用例</p>
              ) : (
                <ul>
                  {summary.failures.map((failure) => (
                    <li key={failure.case_id}>
                      <code>{failure.case_id}</code> · {failure.track} · {failure.status}{' '}
                      <span className="muted">{failure.failed_checks.join(', ')}</span>
                      <br />
                      <code className="muted">{failure.repro}</code>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div className="panel">
              <h2>失败归因（Pareto）</h2>
              {summary.pareto.length === 0 ? (
                <p className="muted">无</p>
              ) : (
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>检查项</th>
                      <th>命中次数</th>
                    </tr>
                  </thead>
                  <tbody>
                    {summary.pareto.map((item) => (
                      <tr key={item.check}>
                        <td>
                          <code>{item.check}</code>
                        </td>
                        <td>{item.count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>

          <div className="panel">
            <h2>用例明细</h2>
            <div className="tag-row">
              <select
                value={track}
                onChange={(event) => {
                  setTrack(event.target.value);
                  setOffset(0);
                }}
              >
                <option value="">全部赛道</option>
                {trackOptions.map((key) => (
                  <option key={key} value={key}>
                    {TRACK_LABEL[key] ?? key}
                  </option>
                ))}
              </select>
              <label>
                <input
                  type="checkbox"
                  checked={onlyFailed}
                  onChange={(event) => {
                    setOnlyFailed(event.target.checked);
                    setOffset(0);
                  }}
                />{' '}
                只看失败
              </label>
              <span className="muted">共 {total} 条</span>
            </div>
            <table className="data-table">
              <thead>
                <tr>
                  <th>用例</th>
                  <th>赛道</th>
                  <th>主题</th>
                  <th>终态</th>
                  <th>耗时</th>
                  <th>结论</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr
                    key={row.id}
                    className={row.passed ? 'case-row' : 'case-row is-failed'}
                    onClick={() => setOpenCase(row.id)}
                  >
                    <td>
                      <code>{row.case_id}</code>
                    </td>
                    <td>{row.track}</td>
                    <td>{row.subject}</td>
                    <td>{row.status}</td>
                    <td>{row.latency_ms}ms</td>
                    <td>
                      <span className={`status-pill ${row.passed ? 'is-pass' : 'is-fail'}`}>
                        {row.passed ? '通过' : '失败'}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="tag-row">
              <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>
                上一页
              </button>
              <button type="button" disabled={offset + 50 >= total} onClick={() => setOffset(offset + 50)}>
                下一页
              </button>
            </div>
            {openCase !== null && <CaseDrawer rowId={openCase} />}
          </div>
        </>
      )}

      <div className="panel-grid">
        <div className="panel">
          <h2>批次对比</h2>
          <div className="tag-row">
            <select value={base ?? ''} onChange={(event) => setBase(Number(event.target.value) || null)}>
              <option value="">基线批次</option>
              {list.map((item) => (
                <option key={item.id} value={item.id}>
                  #{item.id}
                </option>
              ))}
            </select>
            <select value={head ?? ''} onChange={(event) => setHead(Number(event.target.value) || null)}>
              <option value="">对比批次</option>
              {list.map((item) => (
                <option key={item.id} value={item.id}>
                  #{item.id}
                </option>
              ))}
            </select>
          </div>
          {!compareEnabled && <p className="muted">选两个不同批次查看 delta</p>}
          {compareQuery.data && (
            <>
              {!compareQuery.data.comparable && (
                <p className="muted">题库或判分方案已变化，diff 仅供参考</p>
              )}
              <table className="data-table">
                <thead>
                  <tr>
                    <th>赛道</th>
                    <th>基线</th>
                    <th>对比</th>
                    <th>delta</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(compareQuery.data.tracks).map(([key, stat]) => (
                    <tr key={key}>
                      <td>{TRACK_LABEL[key] ?? key}</td>
                      <td>{percent(stat.base)}</td>
                      <td>{percent(stat.head)}</td>
                      <td>{stat.delta === 0 ? '—' : `${(stat.delta * 100).toFixed(1)}pp`}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {compareQuery.data.flips.length > 0 && (
                <p className="muted">
                  翻转用例：{compareQuery.data.flips.map((flip) => flip.case_id).join(', ')}
                </p>
              )}
            </>
          )}
        </div>

        <div className="panel">
          <h2>题库覆盖</h2>
          {coverageQuery.data ? (
            <>
              <p className="muted">
                共 {coverageQuery.data.cases} 条 · suite {coverageQuery.data.suite_sha.slice(0, 12)}
              </p>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>赛道</th>
                    <th>用例</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(coverage.tracks ?? {}).map(([key, count]) => (
                    <tr key={key}>
                      <td>{TRACK_LABEL[key] ?? key}</td>
                      <td>{count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {coverageQuery.data.problems.length > 0 && (
                <p className="muted">校验问题：{coverageQuery.data.problems.join('；')}</p>
              )}
            </>
          ) : (
            <p className="muted">加载中…</p>
          )}
        </div>
      </div>

      <div className="panel">
        <h2>Rubric 工件</h2>
        {(rubricsQuery.data?.items ?? []).length === 0 ? (
          <p className="muted">
            还没有 rubric。运行 <code>python -m devops_agent.openjudge rubric generate</code>
          </p>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>名称</th>
                <th>版本</th>
                <th>状态</th>
                <th>审阅人</th>
              </tr>
            </thead>
            <tbody>
              {(rubricsQuery.data?.items ?? []).map((item) => (
                <tr key={`${item.name}@${item.version}`}>
                  <td>{item.name}</td>
                  <td>{item.version}</td>
                  <td>{item.status}</td>
                  <td>{item.reviewer || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
        </>
      )}
    </div>
  );
}
