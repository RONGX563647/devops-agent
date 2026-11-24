/** Edit / Write 的增删行 diff。 */

import type { TraceStep } from '../../types';

export function Diff({ step }: { step: TraceStep }) {
  if (!step.diff) return null;
  return (
    <div className="trace-diff">
      {step.diff.oldLines.map((line, i) => (
        <div className="diff-old" key={`o${i}`}>
          - {line || ' '}
        </div>
      ))}
      {step.diff.newLines.map((line, i) => (
        <div className="diff-new" key={`n${i}`}>
          + {line || ' '}
        </div>
      ))}
    </div>
  );
}
