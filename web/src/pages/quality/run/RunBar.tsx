/**
 * 运行栏：发起一次在线评测并跟踪进度。
 *
 * 单飞锁由服务端保证：撞到已有任务时返回 409，`detail.run_id` 指向正在跑的那个。
 * 这里不把 409 当错误弹出来，而是直接挂上去继续轮询 —— 用户要的是进度，不是提示。
 */
import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ApiError,
  cancelOpenJudgeRun,
  fetchOpenJudgeLibrary,
  fetchOpenJudgeRun,
  startOpenJudgeRun,
} from '../../../api/client';
import type { OjRunConflictDetail } from '../../../types';

const TRACK_OPTIONS: { value: string; label: string }[] = [
  { value: 'A', label: 'A 仓库问答' },
  { value: 'B', label: 'B 权限边界' },
  { value: 'C', label: 'C 改动能力' },
  { value: 'D', label: 'D 输入管线' },
  { value: 'E', label: 'E 多轮会话' },
];

const STATUS_TEXT: Record<string, string> = {
  pending: '排队中',
  running: '运行中',
  done: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

/** 默认值刻意保守：10 条、不跑裁判，避免一次误点烧掉额度。 */
const DEFAULT_LIMIT = 10;
const MAX_LIMIT = 30;
const MAX_SELECTED = 30;

function clampLimit(value: number): number {
  if (Number.isNaN(value)) return DEFAULT_LIMIT;
  return Math.min(MAX_LIMIT, Math.max(1, Math.round(value)));
}

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (typeof err.detail === 'string') return err.detail;
    if (err.detail && typeof err.detail === 'object' && 'message' in err.detail) {
      return String((err.detail as { message: unknown }).message);
    }
    return `请求失败 ${err.status}`;
  }
  return err instanceof Error ? err.message : '请求失败';
}

/** 409 时取出正在跑的任务 id；其它错误返回 null。 */
function conflictRunId(err: unknown): number | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null;
  const detail = err.detail as OjRunConflictDetail | null;
  return detail && typeof detail.run_id === 'number' ? detail.run_id : null;
}

interface CasePickerProps {
  selected: string[];
  onToggle: (id: string) => void;
  onClear: () => void;
}

/** 题库多选面板：搜索后勾选，选满上限后其余项只读。 */
function CasePicker({ selected, onToggle, onClear }: CasePickerProps) {
  const [search, setSearch] = useState('');
  const query = useQuery({
    queryKey: ['oj-library', 'run-picker'],
    queryFn: () => fetchOpenJudgeLibrary(),
  });

  if (query.isLoading) return <p className="muted">加载题库…</p>;
  if (query.error || !query.data) return <p className="muted">题库不可用</p>;

  const keyword = search.trim().toLowerCase();
  const items = query.data.items.filter(
    (item) =>
      !keyword ||
      item.id.toLowerCase().includes(keyword) ||
      item.subject.toLowerCase().includes(keyword) ||
      item.track.toLowerCase().includes(keyword),
  );

  return (
    <div className="oj-case-picker">
      <div className="tag-row">
        <input
          type="search"
          aria-label="搜索用例"
          placeholder="按用例 id / 主题 / 赛道搜索"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <span className="muted">
          已选 {selected.length}/{MAX_SELECTED}
        </span>
        <button type="button" disabled={selected.length === 0} onClick={onClear}>
          清空
        </button>
      </div>
      <div className="oj-case-picker-list">
        {items.map((item) => {
          const checked = selected.includes(item.id);
          return (
            <label key={item.id} className="oj-case-picker-item">
              <input
                type="checkbox"
                checked={checked}
                disabled={!checked && selected.length >= MAX_SELECTED}
                onChange={() => onToggle(item.id)}
              />
              <code>{item.id}</code> · {item.track} · {item.subject}
            </label>
          );
        })}
        {items.length === 0 && <p className="muted">没有匹配的用例</p>}
      </div>
    </div>
  );
}

interface RunBarProps {
  /** 批次落库后回调：父组件据此切到新批次。 */
  onFinished: (batchId: number) => void;
}

export function RunBar({ onFinished }: RunBarProps) {
  const qc = useQueryClient();
  const [track, setTrack] = useState('A');
  const [limit, setLimit] = useState(DEFAULT_LIMIT);
  const [judge, setJudge] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [caseIds, setCaseIds] = useState<string[]>([]);
  const [runId, setRunId] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const finishedRef = useRef<number | null>(null);

  const runQuery = useQuery({
    queryKey: ['oj-run', runId],
    queryFn: () => fetchOpenJudgeRun(runId as number),
    enabled: runId !== null,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === 'running' || status === 'pending' ? 2000 : false;
    },
  });
  const run = runQuery.data;

  const hasCases = caseIds.length > 0;
  const toggleCase = (id: string) =>
    setCaseIds((prev) =>
      prev.includes(id)
        ? prev.filter((item) => item !== id)
        : prev.length >= MAX_SELECTED
          ? prev
          : [...prev, id],
    );

  const start = useMutation({
    mutationFn: () =>
      startOpenJudgeRun(
        hasCases
          ? { case_ids: caseIds, judge }
          : { track: track || undefined, limit, judge },
      ),
    onSuccess: (data) => {
      setRunId(data.run_id);
      setStartError(null);
      setNotice(`已发起评测 #${data.run_id}，共 ${data.total} 条用例。`);
    },
    onError: (err) => {
      const running = conflictRunId(err);
      if (running !== null) {
        setRunId(running);
        setStartError(null);
        setNotice(`已有任务在运行，已接上 #${running} 的进度。`);
        return;
      }
      setStartError(errorMessage(err));
    },
  });

  const cancel = useMutation({
    mutationFn: (id: number) => cancelOpenJudgeRun(id),
    onSuccess: () => {
      void runQuery.refetch();
    },
  });

  useEffect(() => {
    if (run?.status !== 'done' || run.batch_id === null) return;
    if (finishedRef.current === run.batch_id) return;
    finishedRef.current = run.batch_id;
    void qc.invalidateQueries({ queryKey: ['oj-batches'] });
    void qc.invalidateQueries({ queryKey: ['oj-batch', run.batch_id] });
    void qc.invalidateQueries({ queryKey: ['oj-suites'] });
    void qc.invalidateQueries({ queryKey: ['oj-series'] });
    onFinished(run.batch_id);
  }, [run, qc, onFinished]);

  const active = run?.status === 'pending' || run?.status === 'running';
  const finished = run?.status === 'done';
  const stopped = run?.status === 'failed' || run?.status === 'cancelled';

  return (
    <div className="panel">
      <h2>发起评测</h2>
      <div className="tag-row">
        <label>
          赛道{' '}
          <select
            value={track}
            disabled={hasCases}
            onChange={(event) => setTrack(event.target.value)}
          >
            {TRACK_OPTIONS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          条数上限{' '}
          <input
            type="number"
            min={1}
            max={MAX_LIMIT}
            value={limit}
            disabled={hasCases}
            onChange={(event) => setLimit(Number(event.target.value))}
            onBlur={() => setLimit(clampLimit(limit))}
            style={{ width: '5em' }}
          />
        </label>
        <label>
          <input
            type="checkbox"
            checked={judge}
            onChange={(event) => setJudge(event.target.checked)}
          />{' '}
          跑语义裁判
        </label>
        <button
          type="button"
          disabled={start.isPending || active}
          onClick={() => setPickerOpen((open) => !open)}
        >
          {pickerOpen ? '收起用例' : '选择用例'}
          {hasCases && `（已选 ${caseIds.length}）`}
        </button>
        <button
          type="button"
          className="primary-btn"
          disabled={start.isPending || active}
          onClick={() => start.mutate()}
        >
          {start.isPending ? '发起中…' : '开始运行'}
        </button>
        {active && run && (
          <button type="button" disabled={cancel.isPending} onClick={() => cancel.mutate(run.id)}>
            取消
          </button>
        )}
      </div>

      {hasCases && (
        <p className="hint">已按所选 {caseIds.length} 条用例发起，赛道与条数上限不生效。</p>
      )}
      {pickerOpen && (
        <CasePicker
          selected={caseIds}
          onToggle={toggleCase}
          onClear={() => setCaseIds([])}
        />
      )}

      {notice && !startError && <p className="hint">{notice}</p>}
      {startError && <p className="query-error">发起失败：{startError}</p>}

      {run && (
        <div className="run-progress">
          <p className="hint">
            任务 #{run.id} · {STATUS_TEXT[run.status] ?? run.status}
            {active && ` · 已完成 ${run.done}/${run.total}`}
            {active && run.current_case ? ` · 当前 ${run.current_case}` : ''}
          </p>
          {(active || finished) && (
            <progress value={run.done} max={run.total || 1} style={{ width: '100%' }}>
              {run.done}/{run.total}
            </progress>
          )}
          {finished && (
            <p className="hint">
              共 {run.total} 条，批次 #{run.batch_id ?? '—'} 已生成并自动选中。
            </p>
          )}
          {stopped && (
            <>
              <p className="query-error">
                任务{STATUS_TEXT[run.status] ?? run.status}
                {run.error ? `：${run.error}` : '，可调整参数后重试。'}
              </p>
              <button type="button" onClick={() => start.mutate()}>
                重试
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
