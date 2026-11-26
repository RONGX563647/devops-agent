/**
 * 质量页共用文案：赛道名、结论、范围、校准档位与配色。
 *
 * 结论是**单一来源**：任何页面新增结论态都必须先在这里加文案，
 * 否则 `verdictText` 会把原始英文枚举直接吐到界面上。
 */

/** 赛道固定顺序：表格永远五行，缺哪条就显式标「未测」，不隐藏。 */
export const TRACK_KEYS = ['A', 'B', 'C', 'D', 'E'] as const;

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
  partial: '局部',
  unvalidated: '未证实',
  insufficient: '样本不足',
  warn: '回退',
  running: '运行中',
  cancelled: '已取消',
};

const VERDICT_VERBOSE: Record<string, string> = {
  pass: '通过：全部门禁达标且质量已证实',
  fail: '未通过：存在硬失败或门禁未达',
  partial: '局部结论：只测了部分赛道，不作发布门禁',
  unvalidated: '规则全过但质量未证实，需先完成裁判校准',
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

/**
 * 配色：`partial` 用中性蓝而不是黄 —— 它不是「差」，是「范围不够」，
 * 与 `unvalidated` 的「证据不够」也要能一眼分开。
 */
export function verdictClass(verdict: string): string {
  if (verdict === 'pass') return 'is-pass';
  if (verdict === 'fail') return 'is-fail';
  if (verdict === 'partial') return 'is-partial';
  if (verdict === 'unvalidated') return 'is-unvalidated';
  if (verdict === 'insufficient') return 'is-insufficient';
  if (verdict === 'warn') return 'is-warn';
  if (verdict === 'cancelled') return 'is-cancelled';
  return 'is-pending';
}

/** 范围口径：决定这批结论能不能当发布门禁。 */
export const SCOPE_LABEL: Record<string, string> = {
  single_track: '单赛道',
  selected: '部分赛道',
  full: '全量赛道',
};

/** 裁判档位：`off` 不是「校准失败」，是「没有校准记录」。*/
export const JUDGE_LEVEL_LABEL: Record<string, string> = {
  off: '未校准（裁判不否决）',
  soft: '软校准（可降为未证实）',
  hard: '硬校准（可否决）',
};

/** 原因严重度：blocking 挡发布，advisory 只提示。 */
export const SEVERITY_LABEL: Record<string, string> = {
  blocking: '阻断',
  advisory: '提示',
};

/** 用例分层：必过集一票否决，挑战集只进挑战分。 */
export const TIER_LABEL: Record<string, string> = {
  must_pass: '必过',
  challenge: '挑战',
};

/** 评测结论的过滤下拉：与 VERDICT_SHORT 的键保持一致。 */
export const VERDICT_OPTIONS = [
  { value: '', label: '全部结论' },
  { value: 'pass', label: '通过' },
  { value: 'partial', label: '局部' },
  { value: 'unvalidated', label: '未证实' },
  { value: 'fail', label: '未达标' },
  { value: 'insufficient', label: '样本不足' },
  { value: 'warn', label: '回退' },
  { value: 'cancelled', label: '已取消' },
];
