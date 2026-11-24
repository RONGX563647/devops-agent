/** 门禁三态表：解释「为什么是 insufficient」，不在前端重算阈值。 */

import type { EvalBatch } from '../../types';
import { GATE_REASON_LABEL, GATE_STATE_LABEL } from './format';

export function GatesTable({ batch }: { batch: EvalBatch }) {
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
