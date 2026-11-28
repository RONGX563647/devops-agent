/** 模型参数（SPEC-0001）：与后端 Pydantic 响应对齐的前端类型。 */

/** 一个参数值是从哪一层来的：会话覆盖 > 部署默认 > yaml > 环境变量 > 框架默认。
 *
 * 后三项来自能力合并（SPEC-0018）：它们不是「哪个人填的」，而是
 * 「这个数字是怎么查出来的」。渲染时一律走 `sourceLabel()`，
 * 后端将来新增来源也不会让界面崩（A44）。
 */
export type ModelSource =
  | 'session'
  | 'stored'
  | 'yaml'
  | 'env'
  | 'framework-default'
  /** 本地校准表 config/models[.local].yaml */
  | 'calibrated'
  /** 网关 /models 的声明 */
  | 'discovered'
  /** AgentScope 内置模型卡 */
  | 'card'
  /** 后端新增而本前端还不认识的来源：只用于渲染兜底，不会出现在打字处 */
  | (string & {});

/** 生效参数集：键与后端 `effective` 一一对应。 */
export interface ModelValues {
  /** 网关 id，空串表示「跟随注册表默认」。 */
  provider: string;
  model: string;
  context_size: number;
  /** 窗口是否跟随模型能力；false 时手填值不被覆盖。 */
  context_auto: boolean;
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
  /** 窗口这个数字的来源（calibrated / discovered / card / yaml / …）。 */
  context_source?: string;
  /** 来自内置卡时窗口可能滞后于新模型：提示「这个数字可能过期」。 */
  context_card_may_be_stale?: boolean;
}

/** 模型名与内置卡的匹配方式：`none` 表示没匹配上，窗口按未校准处理。 */
export type MatchKind = 'exact' | 'prefix' | 'normalized' | 'none';

/** 当前模型的能力从哪来（`GET /api/model-config` 的 `catalog.current`）。 */
export interface CatalogCurrent {
  matched_by: MatchKind | string;
  context_source: string;
  card_may_be_stale: boolean;
  status: string;
  deprecated_at: string | null;
  label: string;
}

/** `catalog` 段：这个模型的能力是从哪个网关的哪张卡查出来的。 */
export interface CatalogBlock {
  providers_configured: number;
  /** 已生效的网关 id（空 provider 由后端补成注册表默认项）。 */
  provider: string;
  current: CatalogCurrent;
}

/** `GET /api/providers` 的一项：脱敏后的网关。只有 host 与 key 指纹，没有 key。 */
export interface ProviderInfo {
  id: string;
  kind: string;
  label: string;
  base_url_host: string;
  discover: 'models' | 'manual' | 'off';
  has_key: boolean;
  /** sha256 前 8 位：判断「配置是不是被改过」，不可反推。 */
  key_fingerprint: string;
  /** 上次发现到的模型数；0 表示还没取过或取空了。 */
  models: number;
  last_check: { at: string; code: string } | null;
}

/** 一种可用的协议。 */
export interface CatalogKind {
  kind: string;
  family: string;
}

/** `GET /api/providers` 的响应。 */
export interface ProvidersPayload {
  default: string;
  anonymous: boolean;
  enabled: boolean;
  items: ProviderInfo[];
  kinds: CatalogKind[];
}

/** `GET /api/providers/{id}/models` 的一项。
 *
 * `context_size` 已经是**合并后**的值（网关声明 > 内置卡 > 兜底），
 * 面板直接显示它，不再自己挑来源。
 */
export interface ProviderModel {
  id: string;
  label: string;
  created_at: string | null;
  context_size: number | null;
  output_size: number | null;
  input_types: string[];
  status: string;
  /** upstream（网关声明） / manual（人工清单） / card（内置卡） */
  origin: string;
  /** 与内置卡的匹配方式：`none` 说明窗口是按未校准处理的。 */
  matched_by?: MatchKind | string;
}

/** `GET /api/providers/{id}/models` 的响应。失败也是 200：`code` 说明原因。 */
export interface ProviderModels {
  provider: string;
  fetched_at: string;
  /** 回的是旧缓存（上游此刻不可达）。 */
  stale: boolean;
  code: string;
  http_status: number | null;
  total: number;
  items: ProviderModel[];
}

/** `GET /api/providers/{id}/models/{model}` 的响应：单模型能力卡。 */
export interface ModelCapability {
  provider: string;
  model: string;
  capability: {
    context_size: number;
    output_size: number | null;
    input_types: string[];
    output_types: string[];
    status: string;
    label: string;
    parameter_schema: Record<string, unknown>;
    deprecated_at: string;
  };
  sources: Record<string, string>;
  matched_by: MatchKind | string;
  matched_card: string | null;
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
  /** 部署默认落盘的位置（config/model.local.yaml 的绝对路径）。 */
  file_path?: string;
  /** 该会话的覆盖项（GET 带 session 时返回）。 */
  overrides?: Partial<ModelValues>;
  /** 改部署默认后的传播结果：多少会话已生效、多少等当前轮结束。 */
  affected?: { applied: number; deferred: number };
  /** 能力目录：这个模型的窗口从哪来、注册表里配了几个网关。 */
  catalog?: CatalogBlock;
}

/** 一层参数：`writable=false` 的层只能改文件或环境变量。 */
export interface ModelLayer {
  id: 'session' | 'file' | 'stored' | 'base';
  label: string;
  writable: boolean;
  path?: string;
  values: Partial<ModelValues>;
}

/** GET /api/model-config/layers 的响应：每一层各自写了什么。 */
export interface ModelLayers {
  version: number;
  file_path: string;
  layers: ModelLayer[];
  effective: ModelValues;
  sources: Record<string, ModelSource>;
}

/** 会话级改动的响应：applied=false 表示没有活跃实例，下一轮才装配。 */
export interface SessionModelConfig extends ModelConfig {
  overrides: Partial<ModelValues>;
  applied?: boolean;
  effective_from?: string;
}

/** `GET /api/providers` 的响应里新增的内容版本：写网关时原样带回做乐观锁。 */
export interface ProvidersPayloadVersioned extends ProvidersPayload {
  version: string;
}

/** 网关编辑表单的一份完整配置（`GET /api/providers/{id}/draft`）。 */
export interface ProviderDraft {
  id: string;
  kind: string;
  label: string;
  base_url: string;
  /** 环境变量名或 `none`；服务端拼成 `env:NAME` 写盘。 */
  api_key_ref: string;
  discover: 'models' | 'manual' | 'off' | string;
}

/** `GET /api/models` 的一项：跨网关后的模型。 */
export interface AggregatedModel {
  provider: string;
  provider_label: string;
  model: string;
  label: string;
  context: number | null;
  status: string;
  capabilities: string[];
  source: string;
}

/** 聚合响应里的逐网关状态：`code !== 'ok'` 时组头要说明原因。 */
export interface AggregateProvider {
  id: string;
  label: string;
  code: string;
  total: number;
  stale: boolean;
  missing_key: boolean;
}

export interface ModelsAggregate {
  items: AggregatedModel[];
  providers: AggregateProvider[];
  default: { provider: string; model: string };
  truncated: boolean;
}

/** 字段级错误：预览用 200 回，前端按 `field` 高亮那一行。 */
export interface ModelPreviewError {
  field: string;
  reason: string;
}

/** `POST /api/model-config/preview`：草稿的生效值 + 派生值 + 字段级错误。 */
export interface ModelPreview extends ModelConfig {
  errors: ModelPreviewError[];
}

/** `POST /api/providers/{id}/check`：探活结果，不写文件。 */
export interface ProviderCheck {
  id: string;
  code: string;
  http_status: number | null;
  total: number;
  latency_ms: number;
}
