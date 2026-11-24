/** 批次结论徽标。 */

import { VERDICT_LABEL } from './format';

export function VerdictBadge({ verdict }: { verdict: string }) {
  return (
    <span className={`badge verdict-badge verdict-${verdict}`}>
      {VERDICT_LABEL[verdict] ?? verdict}
    </span>
  );
}
