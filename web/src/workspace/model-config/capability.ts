/** 能力来源、匹配方式、模型规模的文案与纯函数。
 *
 * 单独成文件的两个理由：
 * 1. 「这个数字从哪来」要能被单测直接断言，不该埋在组件里；
 * 2. 后端将来新增来源时，界面必须降级成「未知来源」而不是白屏或 `undefined`
 *    ——正是 A44 要守的那条线。
 */

import type { MatchKind, ModelCapability, ModelValues, ProviderModel } from '../../types';
import { formatTokens } from '../ContextRing';

/** 来源徽标文案。键是后端 `sources` 的取值，不在这里的走 `UNKNOWN_SOURCE`。 */
export const SOURCE_LABEL: Record<string, string> = {
  session: '本会话',
  stored: '部署默认',
  yaml: '配置文件',
  env: '环境变量',
  'framework-default': '未配置',
  // 能力合并没有「人填过」这回事：来源说的是「这个数字怎么查出来的」
  calibrated: '校准表',
  discovered: '网关声明',
  card: '内置卡',
};

/** 认不出来的来源。刻意不是空串：空串看起来像「没有来源」，而这里是有来源但看不懂。 */
export const UNKNOWN_SOURCE = '未知来源';

/** 来源的一句话解释：徽标只有两个字，悬停时才说得清它意味着什么。 */
const SOURCE_HINT: Record<string, string> = {
  session: '只影响本次会话，换会话失效',
  stored: '写在部署默认里，所有会话共享',
  yaml: 'config/model.yaml 或环境变量',
  env: '环境变量，重启生效',
  'framework-default': '没人设过，用的是框架默认值',
  calibrated: 'config/models.yaml 里人工校准的值',
  discovered: '网关 /models 自己声明的值',
  card: 'AgentScope 内置模型卡，可能滞后于新模型',
};

export function sourceLabel(source: string | undefined | null): string {
  if (!source) return UNKNOWN_SOURCE;
  return SOURCE_LABEL[source] ?? UNKNOWN_SOURCE;
}

export function sourceHint(source: string | undefined | null): string {
  if (!source) return '';
  return SOURCE_HINT[source] ?? `这个来源本界面还不认识（${source}），值仍按后端给的显示`;
}

/** 来源是否可信到「不用提示校准」：与后端 `_CONTEXT_TRUSTED` 同一套判断。 */
const TRUSTED = new Set(['session', 'stored', 'calibrated', 'discovered', 'card']);

export function sourceNeedsCalibration(source: string | undefined | null): boolean {
  return !source || !TRUSTED.has(source);
}

/** 匹配方式文案：`none` 不是错误，是「网关给的名单里没有它」。 */
export const MATCH_LABEL: Record<MatchKind | string, string> = {
  exact: '精确匹配',
  prefix: '前缀匹配',
  normalized: '归一化匹配',
  none: '未匹配',
};

export function matchLabel(matchedBy: string | undefined | null): string {
  if (!matchedBy) return MATCH_LABEL.none;
  return MATCH_LABEL[matchedBy] ?? matchedBy;
}

/** 卡里的模型状态：`active` 之外的值都在提示「这个模型该换了」。 */
const STATUS_LABEL: Record<string, string> = {
  active: '可用',
  deprecated: '已弃用',
  preview: '预览版',
  implicitly_active: '未声明状态',
};

export function statusLabel(status: string | undefined | null): string {
  if (!status) return STATUS_LABEL.implicitly_active;
  return STATUS_LABEL[status] ?? status;
}

/** 单个候选的展示文案：名字 + 合并后的窗口，下拉里能一眼看出差别。 */
export function modelOptionLabel(item: ProviderModel): string {
  const name = item.label || item.id;
  if (item.id === name) return item.context_size ? `${name} · ${formatTokens(item.context_size)}` : name;
  const size = item.context_size ? ` · ${formatTokens(item.context_size)}` : '';
  return `${name}（${item.id}）${size}`;
}

/** 能力卡的一句话摘要：抽屉里「当前模型」那一栏用它。 */
export function capabilityLine(capability: ModelCapability | null | undefined): string {
  if (!capability) return '';
  const parts = [`窗口 ${formatTokens(capability.capability.context_size)}`];
  const output = capability.capability.output_size;
  if (output) parts.push(`输出 ${formatTokens(output)}`);
  const matched = capability.matched_by;
  parts.push(matched === 'exact' ? '内置卡' : matchLabel(matched));
  return parts.join(' · ');
}

/** 当前生效的网关 id：空串在后端等于「注册表默认」。 */
export function activeProvider(values: Pick<ModelValues, 'provider'> | null, fallback: string): string {
  return values?.provider || fallback;
}

/** 七种失败各给一句人话 + 下一步做什么。表驱动是为了让「加一种状态」只改一处，
 * 也免得一个 switch 把圈复杂度顶到上限。 */
const DISCOVERY_FAILURE: Record<string, string> = {
  not_configured: '这个网关没配 key，取不到模型列表',
  unauthorized: 'key 被网关拒绝（401/403）：检查环境变量里的 key 是否过期',
  unsupported: '该网关没有 /models（404）：把 discover 改成 manual 或 off',
  rate_limited: '上游限流（429）：过一会儿再刷新',
  unavailable: '连不上网关（超时或 5xx）：地址或网络不通',
  empty: '网关返回了空列表：换 discover: manual 手工填模型名',
  malformed: '返回内容不是预期的模型列表格式',
};

/** 发现结果状态码 → 一句人话，并给出「下一步做什么」。
 *
 * 七码与后端一致。失败不弹窗、不拦保存：面板能打开就意味着「现在这个模型仍可用」，
 * 只是列表没刷新。
 */
export function discoveryText(code: string, total: number, stale: boolean): string {
  const suffix = stale ? '（显示的是上次成功的结果）' : '';
  if (code === '' || code === 'ok') {
    return total > 0 ? `${total} 个模型${suffix}` : '列表为空';
  }
  return DISCOVERY_FAILURE[code] ?? `未知状态 ${code}`;
}
