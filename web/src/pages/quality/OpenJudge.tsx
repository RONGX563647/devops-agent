/** OpenJudge 评测视图：任务区 → 结果区（批次/评分流程/失败定位/明细/对比）。 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import {
  fetchOpenJudgeBatch,
  fetchOpenJudgeBatches,
  fetchOpenJudgeCalibration,
  fetchOpenJudgeCase,
  fetchOpenJudgeCases,
  fetchOpenJudgeCompare,
  fetchOpenJudgeCoverage,
  fetchOpenJudgeRubrics,
  fetchOpenJudgeScoringConfig,
} from '../../api/client';
import { MetricCard } from '../../components/MetricCard';
import type { OjBatch, OjCaseRow, OjTrackStat } from '../../types';
import { formatPercent as percent } from './format';
import { TRACK_LABEL, verdictClass, verdictText } from './labels';
import { BatchStrip } from './run/BatchStrip';
import { CaseDrawer } from './run/CaseDrawer';
import { CaseFilters } from './run/CaseFilters';
import { CaseTable } from './run/CaseTable';
import { FAILURE_TAB, type DrawerTab } from './run/diagnosis';
import { FailureList } from './run/FailureList';
import { ResultsExtras, RubricPanel } from './run/ResultsExtras';
import { RunBar } from './run/RunBar';
import { ScoreFlow } from './run/ScoreFlow';
import { SuitesCard } from './run/SuitesCard';

const PAGE_SIZES = [10, 20, 50, 100];

function parsePageSize(raw: string | null): number {
  const value = Number(raw ?? 50);
  return PAGE_SIZES.includes(value) ? value : 50;
}

/** jsdom 里没有 scrollIntoView；有也不保证接受 options，逐层防护。 */
function reveal(node: HTMLElement | null): void {
  if (!node || typeof node.scrollIntoView !== 'function') return;
  try {
    node.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch {
    node.scrollIntoView();
  }
}

function TrackRow({ track, stat, highlighted }: {
  track: string;
  stat: OjTrackStat;
  highlighted: boolean;
}) {
  const [showReasons, setShowReasons] = useState(false);
  const reasons = stat.reasons ?? [];
  return (
    <>
      <tr className={highlighted ? 'is-highlight' : undefined}>
        <td>{TRACK_LABEL[track] ?? track}</td>
        <td>{stat.n}</td>
        <td>{stat.passed}</td>
        <td>{percent(stat.pass_rate)}</td>
        <td>{stat.judge_avg === null ? '—' : stat.judge_avg.toFixed(2)}</td>
        <td>{stat.violations}</td>
        <td>
          <span className={`status-pill ${verdictClass(stat.verdict)}`}>
            {verdictText(stat.verdict)}
          </span>
          {reasons.length > 0 && (
            <button
              type="button"
              className="oj-link-btn"
              onClick={() => setShowReasons((value) => !value)}
            >
              依据
            </button>
          )}
        </td>
      </tr>
      {showReasons && (
        <tr className="oj-reason-row">
          <td colSpan={7}>
            <ul>
              {reasons.map((reason) => (
                <li key={reason.code}>
                  {reason.message}（实际 {reason.actual ?? '—'} · 阈值 {reason.threshold ?? '—'}）
                </li>
              ))}
            </ul>
          </td>
        </tr>
      )}
    </>
  );
}

export function OpenJudge() {
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const track = params.get('track') ?? '';
  const status = params.get('status') ?? '';
  const subject = params.get('subject') ?? '';
  const verdictFilter = params.get('verdict') ?? '';
  const focus = params.get('focus') ?? '';
  const pageSize = parsePageSize(params.get('pageSize'));
  const onlyFailed = params.get('failed') === '1';
  const offset = Math.max(0, Number(params.get('offset') ?? 0) || 0);

  const updateParams = useCallback(
    (changes: Record<string, string>) => {
      setParams((prev) => {
        const next = new URLSearchParams(prev);
        for (const [key, value] of Object.entries(changes)) {
          if (value) next.set(key, value);
          else next.delete(key);
        }
        if (!Object.prototype.hasOwnProperty.call(changes, 'offset')) next.delete('offset');
        return next;
      }, { replace: true });
    },
    [setParams],
  );

  const batchesQuery = useQuery({
    queryKey: ['oj-batches', verdictFilter],
    queryFn: () => fetchOpenJudgeBatches(20, verdictFilter || undefined),
  });
  const batches: OjBatch[] = batchesQuery.data?.items ?? [];
  const [selected, setSelected] = useState<number | null>(null);
  const activeId = batches.some((item) => item.id === selected)
    ? selected
    : (batches[0]?.id ?? null);

  const batchQuery = useQuery({
    queryKey: ['oj-batch', activeId],
    queryFn: () => fetchOpenJudgeBatch(activeId as number),
    enabled: activeId !== null,
  });
  const configQuery = useQuery({
    queryKey: ['oj-scoring-config'],
    queryFn: fetchOpenJudgeScoringConfig,
    staleTime: 5 * 60 * 1000,
  });
  const calibrationQuery = useQuery({
    queryKey: ['oj-calibration'],
    queryFn: fetchOpenJudgeCalibration,
    staleTime: 5 * 60 * 1000,
    retry: false,
  });
  const casesQuery = useQuery({
    queryKey: ['oj-cases', activeId, track, status, subject, onlyFailed, pageSize, offset],
    queryFn: () =>
      fetchOpenJudgeCases(activeId as number, {
        track: track || undefined,
        status: status || undefined,
        subject: subject || undefined,
        passed: onlyFailed ? false : undefined,
        pageSize,
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
  const coverageQuery = useQuery({ queryKey: ['oj-coverage'], queryFn: fetchOpenJudgeCoverage });
  const rubricsQuery = useQuery({ queryKey: ['oj-rubrics'], queryFn: fetchOpenJudgeRubrics });

  const summary = batchQuery.data?.summary;
  const tracks = useMemo(() => summary?.tracks ?? {}, [summary]);
  const rows = useMemo<OjCaseRow[]>(() => casesQuery.data?.items ?? [], [casesQuery.data]);
  const total = casesQuery.data?.total ?? 0;
  const trackOptions = Object.keys(tracks).sort();
  const statusOptions = Array.from(
    new Set(['success', 'failed', 'parked', 'timeout', ...Object.keys(summary?.kpi.status_counts ?? {})]),
  );
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const currentPage = Math.min(pageCount - 1, Math.floor(offset / pageSize));
  const empty = !batchesQuery.isLoading && batches.length === 0;

  const resultRef = useRef<HTMLDivElement>(null);
  const trackRef = useRef<HTMLDivElement>(null);
  const scoreFlowRef = useRef<HTMLDivElement>(null);
  const focusHandled = useRef('');
  const [toast, setToast] = useState<string | null>(null);
  const [openCase, setOpenCase] = useState<{ id: number; caseId: string } | null>(null);
  const [initialTab, setInitialTab] = useState<DrawerTab>('basis');
  const [highlightTrack, setHighlightTrack] = useState<string | null>(null);

  useEffect(() => {
    if (focus !== 'scoreflow' || !summary || focusHandled.current === focus) return;
    focusHandled.current = focus;
    const timer = window.setTimeout(() => {
      const node = scoreFlowRef.current;
      if (!node || typeof node.scrollIntoView !== 'function') return;
      const focusHeading = () =>
        node.querySelector<HTMLElement>('h2')?.focus({ preventScroll: true });
      // DOM lib 把返回值声明为 void，但个别实现会返回 Promise；用 unknown 承接后再判定。
      const result: unknown = node.scrollIntoView({ behavior: 'smooth', block: 'start' });
      if (result instanceof Promise) void result.then(focusHeading);
      else focusHeading();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [focus, summary]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 3000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const onFinished = useCallback(
    (batchId: number) => {
      setSelected(batchId);
      updateParams({});
      setOpenCase(null);
      setToast(`批次 #${batchId} 已就绪`);
      const scroll = () => reveal(resultRef.current);
      if (typeof requestAnimationFrame === 'function') requestAnimationFrame(scroll);
      else scroll();
    },
    [updateParams],
  );

  const locateFailure = useCallback(
    async (caseId: string, tab?: DrawerTab) => {
      if (activeId === null) return;
      let row = rows.find((item) => item.case_id === caseId);
      if (!row) {
        try {
          const all = await fetchOpenJudgeCases(activeId, { limit: 500 });
          row = all.items.find((item) => item.case_id === caseId);
        } catch {
          row = undefined;
        }
      }
      if (!row) {
        setToast(`未在批次中找到 ${caseId}`);
        return;
      }
      let preferred = tab;
      if (!preferred) {
        try {
          const detail = await queryClient.fetchQuery({
            queryKey: ['oj-case', activeId, row.id],
            queryFn: () => fetchOpenJudgeCase(activeId, row.id),
          });
          const type = detail.scoring?.failure_type;
          if (type) preferred = FAILURE_TAB[type];
        } catch {
          preferred = undefined;
        }
      }
      setOpenCase({ id: row.id, caseId: row.case_id });
      setInitialTab(preferred ?? 'basis');
    },
    [activeId, rows, queryClient],
  );

  const copyRepro = useCallback((text: string) => {
    const clipboard = navigator.clipboard;
    if (!clipboard?.writeText) {
      setToast('复制不可用，请手动选择命令');
      return;
    }
    void clipboard
      .writeText(text)
      .then(() => setToast('已复制复现命令'))
      .catch(() => setToast('复制失败，请手动选择命令'));
  }, []);

  const inspect = (tab: DrawerTab) => {
    const first = summary?.failures[0];
    if (first) void locateFailure(first.case_id, tab);
    else setToast('本批次没有失败用例可下钻');
  };

  const showReasons = useCallback(
    (key?: string) => {
      const target = key ?? Object.keys(tracks)[0] ?? null;
      if (target) setHighlightTrack(target);
      reveal(trackRef.current);
    },
    [tracks],
  );

  const goPage = (index: number) =>
    updateParams({ offset: index === 0 ? '' : String(index * pageSize) });

  return (
    <div className="oj-view">
      <RunBar onFinished={onFinished} />
      <SuitesCard />

      {batchesQuery.isLoading && <div className="muted">加载 OpenJudge 批次…</div>}
      {empty && (
        <div className="panel">
          <h2>尚无批次</h2>
          <p className="muted">在上方发起一次评测，跑完后这里会出现批次结论与逐条明细。</p>
        </div>
      )}

      {batches.length > 0 && (
        <div ref={resultRef} className="oj-result-anchor">
          <BatchStrip
            batches={batches}
            activeId={activeId}
            verdict={verdictFilter}
            onVerdictChange={(value) => updateParams({ verdict: value })}
            onSelect={(id) => {
              setSelected(id);
              setOpenCase(null);
              updateParams({});
            }}
          />

          {summary && (
            <>
              <div className="panel-grid">
                <MetricCard label="用例" value={summary.kpi.cases} hint={`通过 ${summary.kpi.passed}`} />
                <MetricCard
                  label="通过率"
                  value={percent(summary.kpi.cases ? summary.kpi.passed / summary.kpi.cases : 0)}
                  tone={summary.verdict === 'pass' ? 'good' : 'bad'}
                />
                <MetricCard
                  label="安全违规"
                  value={summary.kpi.violations}
                  tone={summary.kpi.violations > 0 ? 'bad' : 'good'}
                />
                <MetricCard label="已判分" value={summary.kpi.judge_n} hint="送入语义裁判的用例数" />
                <MetricCard label="规则失败" value={summary.kpi.rule_failed_cases ?? 0} />
                <MetricCard label="裁判低分" value={summary.kpi.judge_failed_cases ?? 0} />
                <MetricCard label="未判分" value={summary.kpi.not_judged_cases ?? 0} />
              </div>

              <div ref={scoreFlowRef}>
                <ScoreFlow
                  key={activeId}
                  summary={summary}
                  config={configQuery.data ?? null}
                  gates={configQuery.data?.gates}
                  calibration={summary.calibration ?? calibrationQuery.data}
                  defaultOpen={summary.verdict !== 'pass' || focus === 'scoreflow'}
                  onInspect={inspect}
                  onReasons={() => showReasons()}
                />
              </div>

              <div ref={trackRef} className="panel">
                <h2>
                  赛道{' '}
                  <span className={`status-pill ${verdictClass(summary.verdict)}`}>
                    {verdictText(summary.verdict)}
                  </span>
                </h2>
                <div className="table-scroll">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>赛道</th><th>用例</th><th>通过</th><th>通过率</th>
                        <th>裁判均分</th><th>违规</th><th>结论</th>
                      </tr>
                    </thead>
                    <tbody>
                      {trackOptions.map((key) => (
                        <TrackRow
                          key={key}
                          track={key}
                          stat={tracks[key]}
                          highlighted={highlightTrack === key}
                        />
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>

              <FailureList
                failures={summary.failures}
                pareto={summary.pareto}
                onLocate={(caseId) => void locateFailure(caseId)}
                onCopy={copyRepro}
              />

              <div className="panel">
                <h2>用例明细</h2>
                <CaseFilters
                  track={track}
                  status={status}
                  subject={subject}
                  pageSize={pageSize}
                  onlyFailed={onlyFailed}
                  trackOptions={trackOptions}
                  statusOptions={statusOptions}
                  total={total}
                  pageCount={pageCount}
                  currentPage={currentPage}
                  onChange={updateParams}
                  onPage={goPage}
                />
                <CaseTable
                  rows={rows}
                  onOpen={(row) => {
                    setOpenCase({ id: row.id, caseId: row.case_id });
                    setInitialTab('basis');
                  }}
                />
              </div>

              <ResultsExtras
                batches={batches}
                base={base}
                head={head}
                onBase={setBase}
                onHead={setHead}
                compareEnabled={compareEnabled}
                compare={compareQuery.data}
                coverage={coverageQuery.data}
              />
              <RubricPanel rubrics={rubricsQuery.data?.items ?? []} />
            </>
          )}

          {openCase !== null && activeId !== null && (
            <CaseDrawer
              batchId={activeId}
              rowId={openCase.id}
              caseId={openCase.caseId}
              initialTab={initialTab}
              config={configQuery.data ?? null}
              failureCaseIds={(summary?.failures ?? []).map((failure) => failure.case_id)}
              onClose={() => setOpenCase(null)}
              onNavigate={(caseId, tab) => void locateFailure(caseId, tab)}
            />
          )}
        </div>
      )}

      {toast && <div className="oj-toast" role="status">{toast}</div>}
    </div>
  );
}
