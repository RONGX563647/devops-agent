/** 固定右侧诊断抽屉：遮罩、Esc 关闭、四个 Tab 与失败用例前后导航。 */

import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { fetchOpenJudgeCase } from '../../../api/client';
import type { OjCaseDetail, OjScoringConfig } from '../../../types';
import { verdictClass } from '../labels';
import { BasisTab } from './BasisTab';
import { ChecksTab } from './ChecksTab';
import { DRAWER_TABS, statusText, type DrawerTab } from './diagnosis';
import { JudgesTab } from './JudgesTab';
import { TraceTab } from './TraceTab';

interface Props {
  batchId: number;
  rowId: number;
  caseId: string;
  initialTab: DrawerTab;
  config: OjScoringConfig | null;
  failureCaseIds: string[];
  onClose: () => void;
  onNavigate: (caseId: string, tab: DrawerTab) => void;
}

function DrawerBody({
  detail,
  tab,
  config,
}: {
  detail: OjCaseDetail;
  tab: DrawerTab;
  config: OjScoringConfig | null;
}) {
  if (tab === 'checks') return <ChecksTab detail={detail} />;
  if (tab === 'judges') return <JudgesTab detail={detail} config={config} />;
  if (tab === 'trace') return <TraceTab detail={detail} />;
  return <BasisTab detail={detail} config={config} />;
}

export function CaseDrawer({
  batchId,
  rowId,
  caseId,
  initialTab,
  config,
  failureCaseIds,
  onClose,
  onNavigate,
}: Props) {
  // 切到另一条用例时重置 Tab：在渲染期同步，避免 effect 里 setState 的级联渲染。
  const [tabState, setTabState] = useState({ rowId, initialTab, tab: initialTab });
  const tab =
    tabState.rowId === rowId && tabState.initialTab === initialTab ? tabState.tab : initialTab;
  const chooseTab = (next: DrawerTab) => setTabState({ rowId, initialTab, tab: next });
  const detail = useQuery({
    queryKey: ['oj-case', batchId, rowId],
    queryFn: () => fetchOpenJudgeCase(batchId, rowId),
  });

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const index = failureCaseIds.indexOf(caseId);
  const previous = index > 0 ? failureCaseIds[index - 1] : null;
  const next = index >= 0 && index < failureCaseIds.length - 1 ? failureCaseIds[index + 1] : null;
  const item = detail.data;

  return (
    <>
      <div className="oj-drawer-overlay" onClick={onClose} aria-hidden="true" />
      <aside className="oj-drawer" role="dialog" aria-modal="true" aria-label="用例诊断">
        <div className="oj-drawer-head">
          <div className="tag-row">
            <code>{caseId}</code>
            {item && (
              <span className={`status-pill ${item.passed ? 'is-pass' : 'is-fail'}`}>
                {statusText(item.status)}
              </span>
            )}
            <button type="button" onClick={onClose}>
              关闭
            </button>
          </div>
          {item && (
            <p className="muted">
              {item.track} · {item.subject} · 风险 {item.risk} · {item.latency_ms}ms ·
              结论 <span className={`status-pill ${verdictClass(item.passed ? 'pass' : 'fail')}`}>
                {item.passed ? '通过' : '未通过'}
              </span>
            </p>
          )}
          <div className="tag-row">
            <button
              type="button"
              disabled={previous === null}
              onClick={() => previous !== null && onNavigate(previous, tab)}
            >
              上一个失败
            </button>
            <button
              type="button"
              disabled={next === null}
              onClick={() => next !== null && onNavigate(next, tab)}
            >
              下一个失败
            </button>
            <span className="muted">
              {index >= 0 ? `${index + 1}/${failureCaseIds.length}` : '非失败用例'}
            </span>
          </div>
        </div>
        <div className="oj-drawer-tabs" role="tablist" aria-label="诊断依据">
          {DRAWER_TABS.map((entry) => (
            <button
              key={entry.key}
              type="button"
              role="tab"
              aria-selected={tab === entry.key}
              className={`view-tab${tab === entry.key ? ' active' : ''}`}
              onClick={() => chooseTab(entry.key)}
            >
              {entry.label}
            </button>
          ))}
        </div>
        <div className="oj-drawer-body">
          {detail.isLoading && <p className="muted">加载用例明细…</p>}
          {(detail.error || !item) && !detail.isLoading && (
            <p className="muted">用例明细不可用</p>
          )}
          {item && <DrawerBody detail={item} tab={tab} config={config} />}
        </div>
      </aside>
    </>
  );
}
