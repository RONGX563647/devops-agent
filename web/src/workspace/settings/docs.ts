/**
 * 参数说明文案（SPEC-0022 §8）。
 *
 * 四段式固定：`title` 中文名、`what` 它是什么、`impact` 改了会怎样、`risk` 风险。
 * 为什么写在前端而不是从后端拿：这些是「人怎么理解参数」的解释，属于文案而不是
 * 接口契约；后端只负责值与边界。但**数字**不在这里 —— 影响里的具体数来自
 * `derived` / `catalog`，前端不重算口径。
 *
 * 未写文档的键走 `docFor()` 兜底：显示中文兜底标题 + 原键名 + 类型与范围，
 * 不空白也不报错。`fallback-allowlist.json` 记录缺口，只许减（与行数棘轮同思路）。
 */

export interface ParamDoc {
  /** 中文名：界面上做标签。 */
  title: string;
  /** 它是什么（一句话）。 */
  what: string;
  /** 改了会怎样（带方向）。 */
  impact: string;
  unit?: string;
  risk?: 'low' | 'mid' | 'high';
}

export const PARAM_DOCS: Record<string, ParamDoc> = {
  // --- 模型参数（用户直接面对的 16 项）---
  provider: {
    title: '网关',
    what: '请求发往哪个网关实例（config/providers.yaml 里的一个 id）。',
    impact: '换网关会让模型名指向另一家的目录；换了家却没有同名模型时会拒绝保存。',
    risk: 'mid',
  },
  model: {
    title: '模型名',
    what: '网关里要调用的模型标识，例如 qwen3-max。',
    impact: '换模型会同时改变窗口上限、价格与支持的采样参数；可手填网关没列出的名字。',
    risk: 'mid',
  },
  context_auto: {
    title: '窗口跟随',
    what: '是否让上下文窗口自动跟随模型能力卡（网关声明 > 本地校准 > 内置卡）。',
    impact: '开着时下面手填的窗口会被覆盖；关掉才以手填值为准。',
    risk: 'low',
  },
  context_size: {
    title: '上下文窗口',
    what: '一次请求里模型能看到的 token 上限，含历史、工具输出与本轮输入。',
    impact: '调大装得下更多历史但更贵更慢；调小会更早触发压缩，长任务被截断。',
    unit: 'token',
    risk: 'high',
  },
  temperature: {
    title: '温度',
    what: '采样随机性。0 最确定，越高越发散。',
    impact: '调高输出更多样但更不稳定；代码与工具调用建议 0–0.3。留空交给供应商默认。',
    risk: 'mid',
  },
  top_p: {
    title: '核采样 top_p',
    what: '只从累计概率前 p 的词里采样，与温度二选一调即可。',
    impact: '调低更保守、重复性更强；与温度同时压得过低会让输出变得机械。',
    risk: 'mid',
  },
  max_tokens: {
    title: '最大输出',
    what: '单次回复允许生成的最大 token 数。',
    impact: '调小可能截断长回答与工具参数；留空表示不限制（受模型输出上限约束）。',
    unit: 'token',
    risk: 'mid',
  },
  reasoning_effort: {
    title: '推理强度',
    what: '推理型模型的思考预算档位（none / minimal / low / medium / high）。',
    impact: '调高更擅长复杂问题但更慢更贵；非推理模型上设置无效。',
    risk: 'mid',
  },
  thinking_enable: {
    title: '思考开关',
    what: '是否让支持思考的模型输出思考过程。',
    impact: '关闭省 token；部分模型关闭后复杂任务质量下降。',
    risk: 'low',
  },
  parallel_tool_calls: {
    title: '并行工具',
    what: '一轮里是否允许同时发起多个工具调用。',
    impact: '开启更快，但要求工具之间无依赖；关掉更可预测、便于按顺序审查。',
    risk: 'low',
  },
  max_iters: {
    title: '迭代上限',
    what: '一次回复里「模型 → 工具 → 模型」最多循环几次。',
    impact: '调大能完成更长任务但更慢更贵；调小会让多步任务中途停下。',
    unit: '次',
    risk: 'mid',
  },
  model_max_retries: {
    title: '模型重试',
    what: 'HTTP 层失败（限流、5xx）后的重试次数。',
    impact: '与智能体重试相乘：最坏请求数 = (1+模型重试) × (1+智能体重试)。',
    unit: '次',
    risk: 'mid',
  },
  agent_max_retries: {
    title: '智能体重试',
    what: 'ReAct 层整轮失败后的重试次数。',
    impact: '调高能扛住偶发失败，但会把最坏请求数成倍放大，成本随之上升。',
    unit: '次',
    risk: 'mid',
  },
  trigger_ratio: {
    title: '压缩触发比例',
    what: '上下文占用超过「窗口 × 该比例」时开始压缩历史。',
    impact: '调高更晚压缩、单轮信息更全但更贵；调低更省钱，长任务更早丢细节。',
    unit: '比例',
    risk: 'high',
  },
  reserve_ratio: {
    title: '压缩保留比例',
    what: '压缩后保留多少上下文（相对窗口）。',
    impact: '调大保留更多原文但压缩收益变小；调小省 token 但容易丢关键上下文。',
    unit: '比例',
    risk: 'mid',
  },
  context_buffer_ratio: {
    title: '预警缓冲',
    what: '窗口占用接近上限时预留给下一轮的空间。',
    impact: '调大更安全但更早开始压缩；调小容易在工具输出很长时撞上限。',
    unit: '比例',
    risk: 'mid',
  },

  // --- 运行策略里最常改的 12 个键 ---
  'memory.short_term.tool_output_chars': {
    title: '单个工具输出上限',
    what: '旧轮次的工具结果最多保留多少字符（最新几个结果豁免）。',
    impact: '调小省 token 但读源码时更容易被截断；调大更完整但历史更贵。',
    unit: '字符',
    risk: 'mid',
  },
  'memory.short_term.max_turns': {
    title: '保留轮数',
    what: '短期记忆最多保留几轮对话。',
    impact: '调小更省但更容易忘记前文；调大更连贯但每轮都重放更多历史。',
    unit: '轮',
    risk: 'mid',
  },
  'memory.short_term.recent_exempt_results': {
    title: '豁免截断的工具结果数',
    what: '最近几个工具结果不做内容截断（按块计数）。',
    impact: '调大让「刚读到的东西」更完整；调小会让并行读多个文件时后面的被截。',
    unit: '个',
    risk: 'low',
  },
  'memory.long_term.enabled': {
    title: '长期记忆开关',
    what: '是否把本轮结论沉淀进长期记忆并在后续召回。',
    impact: '关闭后不再召回历史结论，回答更依赖当前会话。',
    risk: 'low',
  },
  'memory.long_term.backend': {
    title: '长期记忆后端',
    what: 'auto / mem0 / agentic / local / off 五种实现。',
    impact: '换后端会改变召回质量与依赖（mem0 需要外部服务）；off 等于关闭。',
    risk: 'mid',
  },
  'health.latency_baseline_ms': {
    title: '延迟基线',
    what: '健康分里「多慢算慢」的基准毫秒数。',
    impact: '调大让延迟扣分更宽松；调小会让正常波动也掉分。',
    unit: 'ms',
    risk: 'low',
  },
  'health.latency_exponent': {
    title: '延迟惩罚指数',
    what: '超出基线后的扣分曲线陡峭程度。',
    impact: '调大对慢请求惩罚更狠；调小更平滑但区分度下降。',
    risk: 'low',
  },
  'input.fetch_timeout_seconds': {
    title: '抓取超时',
    what: '联网抓取单个 URL 的等待上限。',
    impact: '调小更快失败但慢站点抓不到；调大会拖长整轮。',
    unit: '秒',
    risk: 'low',
  },
  'input.fetch_max_bytes': {
    title: '抓取体积上限',
    what: '单个网页最多读多少字节。',
    impact: '调大能读长文档但更占上下文；调小会截断正文。',
    unit: '字节',
    risk: 'low',
  },
  'sandbox.mode': {
    title: '执行模式',
    what: 'local / docker / opensandbox / auto 四种执行面。',
    impact: '换模式影响隔离强度与可用工具；改完需重启进程。',
    risk: 'high',
  },
  'gateway.sessions.max_entries': {
    title: '会话驻留上限',
    what: '进程内最多保留多少个活跃会话。',
    impact: '调小更省内存但冷启动更频繁；调大更顺滑但占内存。',
    unit: '个',
    risk: 'low',
  },
  'workspace.enabled': {
    title: '工作区开关',
    what: '是否启用工作区（本地目录 / 受管克隆）。',
    impact: '关闭后只能在本仓库工作；改完需重启进程。',
    risk: 'mid',
  },
};

/** 兜底文案：没写文档的键也要能读，不空白、不报错。 */
export function docFor(key: string, fallbackTitle?: string): ParamDoc {
  const known = PARAM_DOCS[key];
  if (known) return known;
  const leaf = key.split('.').pop() ?? key;
  return {
    title: fallbackTitle ?? leaf.replace(/_/g, ' '),
    what: `尚未写说明的参数（${key}）。取值与边界见右侧徽标。`,
    impact: '改动会影响运行行为；不确定时先保持默认，或去 PR 里看这个键的用法。',
  };
}
