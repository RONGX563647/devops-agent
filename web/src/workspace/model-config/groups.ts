/** 参数分组：左树的层级与右表单的渲染顺序都来自这里。
 *
 * 为什么把分类写死成常量而不是从后端拿：分组的依据是「人怎么理解这些参数」
 * （连接 / 采样 / 执行 / 压缩），不是接口契约；后端加参数时只需要在
 * `EDITABLE` 与这里的 items 各补一条，未归类的字段会在「高级」组兜底。
 */

import type { ModelValues } from '../../types';

export type FieldKey = keyof ModelValues;

/** `provider` / `model` 是组合控件（下拉 + 可手填），`auto` 是「跟随/手填」二选一。 */
export type FieldKind =
  | 'text'
  | 'number'
  | 'int'
  | 'effort'
  | 'tristate'
  | 'boolean'
  | 'provider'
  | 'model'
  | 'auto';

export interface FieldSpec {
  key: FieldKey;
  label: string;
  kind: FieldKind;
  hint?: string;
}

export interface GroupSpec {
  id: string;
  label: string;
  /** 一句话说明这组参数影响什么，展开时显示在标题下。 */
  summary: string;
  items: FieldSpec[];
}

export const GROUPS: GroupSpec[] = [
  {
    id: 'connection',
    label: '连接',
    summary: '用哪个网关的哪个模型、能吃多长的上下文',
    items: [
      // 顺序即因果：先选网关，它的 /models 才决定模型候选；选了模型才有能力卡
      { key: 'provider', label: '网关', kind: 'provider' },
      { key: 'model', label: '模型名', kind: 'model' },
      { key: 'context_auto', label: '窗口跟随', kind: 'auto' },
      {
        key: 'context_size',
        label: '上下文窗口',
        kind: 'int',
        hint: '「跟随」开着时以模型能力为准，这里的值会被覆盖',
      },
    ],
  },
  {
    id: 'sampling',
    label: '采样',
    summary: '影响输出的稳定性与长度，留空交给供应商默认',
    items: [
      { key: 'temperature', label: '温度', kind: 'number', hint: '空 = 供应商默认' },
      { key: 'top_p', label: 'top_p', kind: 'number', hint: '空 = 供应商默认' },
      { key: 'max_tokens', label: '最大输出', kind: 'int', hint: '空 = 不限制' },
      { key: 'reasoning_effort', label: '推理强度', kind: 'effort' },
    ],
  },
  {
    id: 'runtime',
    label: '执行',
    summary: '一次回复能走多少轮、失败后重试几次',
    items: [
      { key: 'max_iters', label: '迭代上限', kind: 'int', hint: '空 = 跟随下层' },
      { key: 'model_max_retries', label: '模型重试', kind: 'int', hint: 'HTTP 层 · 空 = 跟随下层' },
      { key: 'agent_max_retries', label: '智能体重试', kind: 'int', hint: 'ReAct 层 · 空 = 跟随下层' },
    ],
  },
  {
    id: 'compression',
    label: '上下文压缩',
    summary: '占用到多少开始压缩、压缩后保留多少',
    items: [
      { key: 'trigger_ratio', label: '触发比例', kind: 'number', hint: '空 = 跟随下层' },
      { key: 'reserve_ratio', label: '保留比例', kind: 'number', hint: '空 = 跟随下层' },
      { key: 'context_buffer_ratio', label: '预警缓冲', kind: 'number', hint: '空 = 跟随下层' },
    ],
  },
  {
    id: 'advanced',
    label: '高级与只读',
    summary: '少见开关；凭据与流式固定在环境变量里',
    items: [
      { key: 'thinking_enable', label: '思考开关', kind: 'tristate', hint: '空 = 供应商默认' },
      { key: 'parallel_tool_calls', label: '并行工具', kind: 'boolean' },
    ],
  },
];

/** 字段 → 所属分组：左树点击叶子、右侧滚动定位都要用。 */
export function groupOf(key: string): string {
  return GROUPS.find((group) => group.items.some((item) => item.key === key))?.id ?? 'advanced';
}

/** 一行的标签：徽标、链路面板的标题都用它，避免两处文案漂移。 */
export function labelOf(key: string): string {
  for (const group of GROUPS) {
    const item = group.items.find((entry) => entry.key === key);
    if (item) return item.label;
  }
  return key;
}
