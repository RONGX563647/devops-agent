/** 质量页共用文案：赛道名、结论文案与结论配色（verdict 文案单一来源）。 */

export const TRACK_LABEL: Record<string, string> = {
  A: 'A 仓库问答',
  B: 'B 权限边界',
  C: 'C 改动能力',
  D: 'D 输入管线',
  E: 'E 多轮会话',
};

/** 短文案：徽标与列表用；长文案：概览结论用。 */
const VERDICT_SHORT: Record<string, string> = {
  pass: '通过',
  fail: '未达标',
  insufficient: '样本不足',
  warn: '回退',
  running: '运行中',
  cancelled: '已取消',
};

const VERDICT_VERBOSE: Record<string, string> = {
  pass: '通过：全部门禁达标',
  fail: '未通过：存在硬失败或门禁未达',
  insufficient: '样本不足，不参与发布门禁',
  warn: '回退：指标下降超过精度上限',
  running: '运行中',
  cancelled: '已取消',
};

/** 保留导出给尚未迁移的调用方；新代码统一用 verdictText。 */
export const VERDICT_TEXT = VERDICT_SHORT;

export function verdictText(verdict: string, verbose = false): string {
  const table = verbose ? VERDICT_VERBOSE : VERDICT_SHORT;
  return table[verdict] ?? verdict;
}

/** 三态之外还有回退/取消两个独立语义，不合并成「待定」。 */
export function verdictClass(verdict: string): string {
  if (verdict === 'pass') return 'is-pass';
  if (verdict === 'fail') return 'is-fail';
  if (verdict === 'insufficient') return 'is-insufficient';
  if (verdict === 'warn') return 'is-warn';
  if (verdict === 'cancelled') return 'is-cancelled';
  return 'is-pending';
}
