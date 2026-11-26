/**
 * OpenJudge 评测视图的编排层：查询、URL 参数、下钻路由都在这里，
 * 展示细节下放给 `run/` 下的组件。
 *
 * URL 是唯一视图状态：批次页、用例页、筛选条件都能从地址栏还原，
 * 分享一个链接看到的就是同一批数据。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import {
  createCaseFromBatch,
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
import type { OjBatchList, OjCaseRow, OjFailure } from '../../types';
import { BatchHeader } from './run/BatchHeader';
import { BatchStrip } from './run/BatchStrip';
import { CaseDrawer } from './run/CaseDrawer';
import { CaseFilters } from './run/CaseFilters';
import { CaseTable } from './run/CaseTable';
import { CausesPanel } from './run/CausesPanel';
import { FAILURE_TAB, type DrawerTab } from './run/diagnosis';
import { FailureList } from './run/FailureList';
import { ResultsExtras, RubricPanel } from './run/ResultsExtras';
import { RunBar } from './run/RunBar';
import { ScoreFlow } from './run/ScoreFlow';
import { findCaseRow, pickFailure, type InspectScope } from './run/select';
import { SuitesCard } from './run/SuitesCard';
import { TrackTable } from './run/TrackTable';

const PAGE_SIZES = [10, 20, 50, 100];
/** 批次一页 20 条：一屏能扫完，又不会一次拉几百个 chip。 */
const BATCH_PAGE = 20;

function parsePageSize(raw: string | null): number {
  const value = Number(raw ?? 50);
  return PAGE_SIZES.includes(value) ? value : 50;
}

function parseOffset(raw: string | null): number {
  return Math.max(0, Number(raw ?? 0) || 0);
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

export function OpenJudge({ focusScoreFlow = false }: { focusScoreFlow?: boolean } = {}) {
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const track = params.get('track') ?? '';
  const status = params.get('status') ?? '';
  const subject = params.get('subject') ?? '';
  const verdictFilter = params.get('verdict') ?? '';
  const pageSize = parsePageSize(params.get('pageSize'));
  const onlyFailed = params.get('failed') === '1';
  const offset = parseOffset(params.get('offset'));
  const batchOffset = parseOffset(params.get('batchOffset'));

  const updateParams = useCallback(
    (changes: Record<string, string>) => {
      setParams((prev) => {
        const next = new URLSearchParams(prev);
        for (const [key, value] of Object.entries(changes)) {
          if (value) next.set(key, value);
          else next.delete(key);
        }
        // 用例页与批次页各自独立：改一个不能把另一个悄悄重置成首页。
        if (!('offset' in changes)) next.delete('offset');
        return next;
      }, { replace: true });
    },
    [setParams],
  );

  const batchesQuery = useQuery({
    queryKey: ['oj-batches', verdictFilter, batchOffset],
    queryFn: () => fetchOpenJudgeBatches(BATCH_PAGE, verdictFilter || undefined, batchOffset),
  });
  const batchPage: OjBatchList = batchesQuery.data ?? {
    items: [],
    total: 0,
    limit: BATCH_PAGE,
    offset: batchOffset,
  };
  const batches = batchPage.items;
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
    new Set([
      'success',
      'failed',
      'parked',
      'timeout',
      ...Object.keys(summary?.kpi.status_counts ?? {}),
    ]),
  );
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const currentPage = Math.min(pageCount - 1, Math.floor(offset / pageSize));
  const empty = !batchesQuery.isLoading && batches.length === 0;

  const resultRef = useRef<HTMLDivElement>(null);
  const trackRef = useRef<HTMLDivElement>(null);
  const scoreFlowRef = useRef<HTMLDivElement>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [openCase, setOpenCase] = useState<{ id: number; caseId: string } | null>(null);
  const [initialTab, setInitialTab] = useState<DrawerTab>('basis');
  const [highlightTrack, setHighlightTrack] = useState<string | null>(null);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 3000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  // 从概览点「看评分流程」进来时才滚过去：直接打开这个视图不该抢焦点。
  useEffect(() => {
    if (!focusScoreFlow || !summary) return;
    const timer = window.setTimeout(() => {
      reveal(scoreFlowRef.current);
      scoreFlowRef.current?.querySelector<HTMLElement>('h2')?.focus({ preventScroll: true });
    }, 0);
    return () => window.clearTimeout(timer);
  }, [focusScoreFlow, summary]);

  const onFinished = useCallback(
    (batchId: number) => {
      setSelected(batchId);
      updateParams({});
      setOpenCase(null);
      setToast(`批次 #${batchId} 已就绪`);
      void queryClient.invalidateQueries({ queryKey: ['oj-batches'] });
      const scroll = () => reveal(resultRef.current);
      if (typeof requestAnimationFrame === 'function') requestAnimationFrame(scroll);
      else scroll();
    },
    [queryClient, updateParams],
  );

  /**
   * 定位一条失败用例：先按赛道缩小范围翻页找行（不再写死 500 条），
   * 找到后再决定抽屉默认停在哪一页签。
   */
  const locateFailure = useCallback(
    async (failure: OjFailure, tab?: DrawerTab) => {
      if (activeId === null) return;
      let row = rows.find((item) => item.case_id === failure.case_id);
      if (!row) {
        try {
          row = await findCaseRow(
            failure.case_id,
            (options) => fetchOpenJudgeCases(activeId, options),
            failure.track || undefined,
          );
        } catch {
          row = undefined;
        }
      }
      if (!row) {
        setToast(`未在批次中找到 ${failure.case_id}`);
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

  /** 门禁/阶段下钻：按点击的那一项筛失败，而不是永远跳第一条。 */
  const inspect = useCallback(
    (scope: InspectScope) => {
      const target = pickFailure(summary?.failures ?? [], scope);
      if (target) {
        void locateFailure(target, scope.tab);
        return;
      }
      const reason = scope.kind === 'rule'
        ? '本批次没有规则断言失败的用例'
        : scope.kind === 'judge'
          ? '本批次没有裁判低分的用例'
          : '本批次没有失败用例可下钻';
      setToast(reason);
    },
    [summary, locateFailure],
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

  /** 回流：把线上出过问题的用例变成一条草稿，人工确认后才进必过集。 */
  const backfill = useCallback(
    async (failure: OjFailure) => {
      if (activeId === null) return;
      const row = rows.find((item) => item.case_id === failure.case_id);
      if (!row) {
        setToast('请先「定位」打开该用例再回流');
        return;
      }
      try {
        const created = await createCaseFromBatch(activeId, row.id);
        const newId = String(created.case?.id ?? '');
        setToast(`已回流为草稿 ${newId}`);
        void queryClient.invalidateQueries({ queryKey: ['oj-library'] });
      } catch (error) {
        setToast(error instanceof Error ? error.message : '回流失败');
      }
    },
    [activeId, rows, queryClient],
  );

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
      {empty && batchOffset > 0 && (
        <div className="panel">
          <h2>这一页没有批次</h2>
          <button type="button" onClick={() => updateParams({ batchOffset: '' })}>
            回到最新一页
          </button>
        </div>
      )}
      {empty && batchOffset === 0 && (
        <div className="panel">
          <h2>尚无批次</h2>
          <p className="muted">在上方发起一次评测，跑完后这里会出现批次结论与逐条明细。</p>
        </div>
      )}

      {batches.length > 0 && (
        <div ref={resultRef} className="oj-result-anchor">
          <BatchStrip
            batches={batches}
            total={batchPage.total}
            limit={batchPage.limit}
            offset={batchPage.offset}
            activeId={activeId}
            verdict={verdictFilter}
            onVerdictChange={(value) => updateParams({ verdict: value, batchOffset: '' })}
            onSelect={(id) => {
              setSelected(id);
              setOpenCase(null);
              updateParams({});
            }}
            onPage={(next) => updateParams({ batchOffset: next === 0 ? '' : String(next) })}
          />

          {summary && batchQuery.data && (
            <>
              <BatchHeader batch={batchQuery.data} />
              <CausesPanel
                summary={summary}
                onTrack={(key) => showReasons(key)}
                onInspect={(kind) =>
                  inspect({ tab: kind === 'rule' ? 'checks' : 'judges', kind })
                }
              />

              <div ref={scoreFlowRef}>
                <ScoreFlow
                  key={activeId}
                  summary={summary}
                  config={configQuery.data ?? null}
                  gates={configQuery.data?.gates}
                  calibration={summary.calibration ?? calibrationQuery.data}
                  defaultOpen={focusScoreFlow || summary.verdict !== 'pass'}
                  onInspect={inspect}
                  onReasons={showReasons}
                />
              </div>

              <div ref={trackRef}>
                <TrackTable
                  tracks={tracks}
                  failures={summary.failures}
                  highlight={highlightTrack}
                />
              </div>

              <FailureList
                failures={summary.failures}
                pareto={summary.pareto}
                truncated={summary.truncated}
                onLocate={(failure) => void locateFailure(failure)}
                onCopy={copyRepro}
                onBackfill={(failure) => void backfill(failure)}
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
              onNavigate={(caseId, tab) => {
                const failure = (summary?.failures ?? []).find((item) => item.case_id === caseId);
                if (failure) void locateFailure(failure, tab);
                else setToast(`未在失败清单中找到 ${caseId}`);
              }}
            />
          )}
        </div>
      )}

      {toast && <div className="oj-toast" role="status">{toast}</div>}
    </div>
  );
}
