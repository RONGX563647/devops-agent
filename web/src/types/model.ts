/** 模型参数（SPEC-0001）：与后端 Pydantic 响应对齐的前端类型。 */

/** 一个参数值是从哪一层来的：会话覆盖 > 部署默认 > yaml > 环境变量 > 框架默认。 */
export type ModelSource = 'session' | 'stored' | 'yaml' | 'env' | 'framework-default';

/** 生效参数集：键与后端 `effective` 一一对应。 */
export interface ModelValues {
  model: string;
  context_size: number;
  temperature: number | null;
  max_tokens: number | null;
  top_p: number | null;
  reasoning_effort: string | null;
  thinking_enable: boolean | null;
  parallel_tool_calls: boolean;
  max_iters: number;
  model_max_retries: number;
  agent_max_retries: number;
  trigger_ratio: number;
  reserve_ratio: number;
  context_buffer_ratio: number;
}

/** 一层的参数覆盖：`null` = 删掉本层的覆盖、回落下层（与后端 apply_patch 同义）。
 *
 * 不能用 `Partial<ModelValues>`：那会把 `context_size` 这类非空字段收窄成
 * `number | undefined`，清空时只能写回继承值，「取消覆盖」就无从表达。
 */
export type ModelPatch = { [K in keyof ModelValues]?: ModelValues[K] | null };

/** 一个字段的取值区间：滑杆上下限与越界提示都来自它，前端不自己写死数字。 */
export interface ModelBound {
  kind: 'int' | 'float' | 'str' | 'bool' | 'enum';
  nullable: boolean;
  min?: number;
  max?: number;
  step?: number;
  choices?: string[];
}

/** 后端算好的推算值：前端不再乘一遍，口径只能有一处。 */
export interface ModelDerived {
  compression_threshold: number;
  worst_case_attempts: number;
  /** 上下文窗口还没被部署者确认过：面板要显示醒目提示。 */
  context_uncalibrated: boolean;
}

/** GET|PATCH /api/model-config 的响应。 */
export interface ModelConfig {
  effective: ModelValues;
  sources: Record<string, ModelSource>;
  bounds: Record<string, ModelBound>;
  editable: string[];
  /** 只读项 → 「在哪改」的说明；面板里禁用但可见。 */
  readonly: Record<string, string>;
  derived: ModelDerived;
  warnings: string[];
  version: number;
  /** 该会话的覆盖项（GET 带 session 时返回）。 */
  overrides?: Partial<ModelValues>;
  /** 改部署默认后的传播结果：多少会话已生效、多少等当前轮结束。 */
  affected?: { applied: number; deferred: number };
}

/** 会话级改动的响应：applied=false 表示没有活跃实例，下一轮才装配。 */
export interface SessionModelConfig extends ModelConfig {
  overrides: Partial<ModelValues>;
  applied?: boolean;
  effective_from?: string;
}
