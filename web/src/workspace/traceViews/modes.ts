/** 轨迹页的四个维度：类型、顺序、提示语放在一起。
 *
 * 单独成文件是因为「维度」既被列表用（决定渲染哪种排布），又被工具栏用
 * （画出 tablist）。让工具栏去 import 列表组件只为拿一个类型，会把
 * 「工具栏依赖列表实现」写进依赖图里 —— 这条边本来不该存在。
 */

export type TraceMode = 'turn' | 'time' | 'call' | 'memory';

export const MODE_HINTS: Record<TraceMode, string> = {
  turn: '按轮次分组；再点一次本按钮 = 全部折叠/展开（只到步骤行，详情不展开）',
  time: '按耗时降序，最慢的排最前；模型调用也在内',
  call: '按模型与工具聚合，点一级节点看失败率与原因分类',
  memory: '记忆动作单独成轨：检索 / 裁剪 / 压缩 / 写入',
};

/** 展示顺序：轮次与记忆是「按语义切」，时长与调用是「按热度切」。 */
export const MODES: { key: TraceMode; label: string }[] = [
  { key: 'turn', label: '轮次' },
  { key: 'memory', label: '记忆' },
  { key: 'time', label: '时长' },
  { key: 'call', label: '调用' },
];
