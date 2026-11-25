/** 规则断言 Tab：全部 checks 按 kind 分组，安全断言单列徽标。 */

import type { OjCaseDetail, OjCheck } from '../../../types';

interface Props {
  detail: OjCaseDetail;
}

export function ChecksTab({ detail }: Props) {
  const groups: Record<string, OjCheck[]> = {};
  for (const check of detail.checks) {
    (groups[check.kind] ??= []).push(check);
  }
  const kinds = Object.keys(groups).sort();
  const securityFailed = detail.checks.filter((check) => check.security && !check.passed);

  return (
    <div className="oj-checks">
      {securityFailed.length > 0 && (
        <div className="oj-alert">
          安全违例 {securityFailed.length} 条：
          {securityFailed.map((check) => check.name).join('、')}
          （不计入语义质量均分）
        </div>
      )}
      {kinds.length === 0 && <p className="muted">这条用例没有规则断言</p>}
      {kinds.map((kind) => (
        <section key={kind} className="oj-check-group">
          <h5>{kind}</h5>
          <ul className="oj-check-list">
            {groups[kind].map((check, index) => (
              <li key={`${check.name}-${index}`}>
                <span className={`status-pill ${check.passed ? 'is-pass' : 'is-fail'}`}>
                  {check.passed ? '通过' : '失败'}
                </span>
                {check.security && <span className="status-pill is-fail">安全断言</span>}{' '}
                <code>{check.name}</code>
                {check.detail && <span className="muted"> · {check.detail}</span>}
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
