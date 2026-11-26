/**
 * 失败用例与归因：规则失败与裁判低分分两个视图，各自可定位 / 复制 / 回流。
 *
 * 为什么必须分开：两种失败的修复动作完全不同（改断言 vs 改提示词或校准裁判），
 * 混在一张列表里只会得到「失败了很多条」这种不可操作的结论。
 *
 * 为什么用原生单选组而不是 `role="tab"`：页签模式要自己实现 roving tabindex，
 * 官方推荐的做法依赖 `focusgroup`（目前只有 Chromium 支持，需要 polyfill）。
 * 这里只是「同一份数据的两个视图」，radio 组的语义准确、方向键切换由浏览器
 * 负责、不需要任何补丁，所以选它。视觉上仍然做成分段控件。
 */

import { useState } from 'react';
import type { OjFailure, OjTruncated } from '../../../types';
import { TRACK_LABEL } from '../labels';
import { statusText } from './diagnosis';

type Bucket = 'rule' | 'judge';

interface Props {
  failures: OjFailure[];
  pareto: { rule: { check: string; count: number }[]; judge: { check: string; count: number }[] };
  /** 列表被截断时显式说明，否则「没有更多失败」是假的。 */
  truncated?: OjTruncated;
  onLocate: (failure: OjFailure) => void;
  onCopy: (text: string) => void;
  /** 回流成题库草稿；不传则不显示该入口。 */
  onBackfill?: (failure: OjFailure) => void;
}

function bucketOf(failure: OjFailure): Bucket {
  return (failure.failed_checks ?? []).length > 0 ? 'rule' : 'judge';
}

/** 规则失败与裁判维度一起说，读者不用在两个视图之间来回对照。 */
function summaryOf(failure: OjFailure): string {
  const parts: string[] = [];
  if ((failure.failed_checks ?? []).length > 0) {
    parts.push(`断言 ${failure.failed_checks.join('、')}`);
  }
  if ((failure.judge_dims ?? []).length > 0) {
    parts.push(`裁判 ${failure.judge_dims.join('、')}`);
  }
  return parts.join(' · ') || '—';
}

function ParetoTable({
  title,
  rows,
  empty,
}: {
  title: string;
  rows: { check: string; count: number }[];
  empty: string;
}) {
  return (
    <div className="oj-pareto-block">
      <h3>{title}</h3>
      {rows.length === 0 ? (
        <p className="muted">{empty}</p>
      ) : (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>归因</th>
                <th>命中次数</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((item) => (
                <tr key={item.check}>
                  <td>
                    <code>{item.check}</code>
                  </td>
                  <td>{item.count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function FailureRow({
  failure,
  onLocate,
  onCopy,
  onBackfill,
}: {
  failure: OjFailure;
  onLocate: (failure: OjFailure) => void;
  onCopy: (text: string) => void;
  onBackfill?: (failure: OjFailure) => void;
}) {
  return (
    <li>
      <div>
        <code>{failure.case_id}</code> · {TRACK_LABEL[failure.track] ?? failure.track} ·{' '}
        {statusText(failure.status)} <span className="muted">{summaryOf(failure)}</span>
      </div>
      <div className="tag-row">
        <code className="muted">{failure.repro}</code>
        <button type="button" onClick={() => onLocate(failure)}>
          定位
        </button>
        <button type="button" onClick={() => onCopy(failure.repro)}>
          复制复现命令
        </button>
        {onBackfill && (
          <button type="button" onClick={() => onBackfill(failure)}>
            回流成草稿
          </button>
        )}
      </div>
    </li>
  );
}

export function FailureList({
  failures,
  pareto,
  truncated,
  onLocate,
  onCopy,
  onBackfill,
}: Props) {
  const [bucket, setBucket] = useState<Bucket>('rule');
  const rule = failures.filter((item) => bucketOf(item) === 'rule');
  const judge = failures.filter((item) => bucketOf(item) === 'judge');
  const visible = bucket === 'rule' ? rule : judge;

  return (
    <div className="panel-grid">
      <div className="panel">
        <h2>失败用例</h2>
        {failures.length === 0 ? (
          <p className="muted">本批次没有失败用例</p>
        ) : (
          <>
            <fieldset className="oj-segmented">
              <legend className="sr-only">失败类型</legend>
              <label className={bucket === 'rule' ? 'is-active' : undefined}>
                <input
                  type="radio"
                  name="oj-failure-bucket"
                  value="rule"
                  checked={bucket === 'rule'}
                  onChange={() => setBucket('rule')}
                />
                <span>规则失败 {rule.length}</span>
              </label>
              <label className={bucket === 'judge' ? 'is-active' : undefined}>
                <input
                  type="radio"
                  name="oj-failure-bucket"
                  value="judge"
                  checked={bucket === 'judge'}
                  onChange={() => setBucket('judge')}
                />
                <span>裁判低分 {judge.length}</span>
              </label>
            </fieldset>
            {visible.length === 0 ? (
              <p className="muted">这一类没有失败</p>
            ) : (
              <ul className="oj-failures">
                {visible.map((failure) => (
                  <FailureRow
                    key={`${failure.case_id}-${failure.failure_type}-${failure.repro}`}
                    failure={failure}
                    onLocate={onLocate}
                    onCopy={onCopy}
                    onBackfill={onBackfill}
                  />
                ))}
              </ul>
            )}
            {truncated?.failures && (
              <p className="muted">
                仅显示前 {failures.length} 条，本批次共 {truncated.total_failures} 条失败。
              </p>
            )}
          </>
        )}
      </div>
      <div className="panel">
        <h2>失败归因</h2>
        <ParetoTable title="规则断言" rows={pareto.rule} empty="没有规则断言失败" />
        <ParetoTable title="裁判维度" rows={pareto.judge} empty="没有裁判维度低分" />
      </div>
    </div>
  );
}
