/** 质量页共用文案：赛道名、结论文案与结论配色。 */

export const TRACK_LABEL: Record<string, string> = {
  A: 'A 仓库问答',
  B: 'B 权限边界',
  C: 'C 改动能力',
  D: 'D 输入管线',
  E: 'E 多轮会话',
};

export const VERDICT_TEXT: Record<string, string> = {
  pass: '通过',
  fail: '未达标',
  insufficient: '样本不足',
  warn: '回退',
  running: '运行中',
  cancelled: '已取消',
};

export function verdictClass(verdict: string): string {
  if (verdict === 'pass') return 'is-pass';
  if (verdict === 'fail') return 'is-fail';
  return 'is-pending';
}
