/** 诊断抽屉的共享契约：Tab 标识、失败类型路由与少量展示文案。 */

import type { OjFailureType } from '../../../types';

export type DrawerTab = 'basis' | 'checks' | 'judges' | 'trace';

export const DRAWER_TABS: { key: DrawerTab; label: string }[] = [
  { key: 'basis', label: '判定依据' },
  { key: 'checks', label: '规则断言' },
  { key: 'judges', label: '语义裁判' },
  { key: 'trace', label: '轨迹证据' },
];

/** 失败类型决定「定位」按钮预选哪个 Tab；后端给类型，前端不重判。 */
export const FAILURE_TAB: Record<Exclude<OjFailureType, ''>, DrawerTab> = {
  security: 'checks',
  runtime: 'trace',
  rule: 'checks',
  judge_skipped: 'judges',
  judge_score: 'judges',
};

export function failureTypeText(value: OjFailureType): string {
  const table: Record<Exclude<OjFailureType, ''>, string> = {
    runtime: '执行异常',
    security: '安全违例',
    rule: '规则未通过',
    judge_skipped: '裁判未执行',
    judge_score: '裁判分低于门槛',
  };
  return value === '' ? '无失败' : (table[value] ?? value);
}

export function statusText(status: string): string {
  const table: Record<string, string> = {
    success: '成功',
    failed: '失败',
    parked: '挂起',
    timeout: '超时',
    running: '运行中',
    error: '异常',
  };
  return (table[status] ?? status) || '—';
}

/** 跳过原因的后端代码保持原样兜底：新增代码无需前端发版即可显示。 */
export function skipReasonText(reason: string | undefined): string {
  if (!reason) return '—';
  const table: Record<string, string> = {
    'judge=false': '未开启语义裁判',
    rule_failed: '规则门禁失败，未进入裁判',
    no_judge_spec: '用例没有裁判声明',
    no_reference_response: '缺少参考答案',
    confirm_turn: '确认轮不参与判分',
    no_expected_tool: '没有预期工具调用',
    loop_need_2_calls: '循环检测至少需要两次调用',
  };
  return table[reason] ?? reason;
}

/** 长文本只渲染摘要，展开后才放原文，避免把大 JSON 一次塞进 DOM。 */
export function excerpt(text: string, limit = 200): string {
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}
