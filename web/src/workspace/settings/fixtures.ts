/** 设置页测试共用的数据形状。只有构造器，没有断言。 */
import type {
  ConfigDomain,
  ConfigIndex,
  ModelConfig,
  ModelLayers,
  ModelsAggregate,
  ProvidersPayloadVersioned,
} from '../../types';

export const CONFIG_INDEX: ConfigIndex = {
  mode: 'db',
  db_path: 'E:/repo/workspace/config.db',
  domains: [
    {
      domain: 'health',
      title: '健康分口径',
      writable: true,
      hot: 'immediate',
      note: '四类权重与阈值',
      managed_by: '',
      version: 1,
      overrides: 0,
      env_locked: [],
    },
    {
      domain: 'memory',
      title: '记忆策略',
      writable: true,
      hot: 'next_turn',
      note: '三层记忆',
      managed_by: '',
      version: 2,
      overrides: 1,
      env_locked: ['long_term.scope'],
    },
    {
      domain: 'model',
      title: '模型参数',
      writable: false,
      hot: 'immediate',
      note: '写入口在设置 › 模型参数',
      managed_by: 'model_settings',
      version: 3,
      overrides: 0,
      env_locked: [],
    },
    {
      domain: 'policy',
      title: '确认协议（只读）',
      writable: false,
      hot: 'restart',
      note: '高危正则只能改 git',
      managed_by: '',
      version: 1,
      overrides: 0,
      env_locked: [],
    },
  ],
};

export function domain(over: Partial<ConfigDomain> = {}): ConfigDomain {
  return {
    domain: 'health',
    title: '健康分口径',
    values: { weights: { execution: 0.4, quality: 0.6 }, latency_baseline_ms: 1200 },
    defaults: { weights: { execution: 0.4, quality: 0.6 }, latency_baseline_ms: 1200 },
    sources: { 'weights.execution': 'default', 'weights.quality': 'db', latency_baseline_ms: 'yaml' },
    bounds: { 'weights.execution': { min: 0, max: 1 } },
    editable: [],
    env_locked: [],
    writable: true,
    managed_by: '',
    hot: 'immediate',
    note: '四类权重与阈值',
    version: 1,
    drifted: [],
    mode: 'db',
    db_path: 'E:/repo/workspace/config.db',
    ...over,
  };
}

export function modelConfig(over: Partial<ModelConfig> = {}): ModelConfig {
  return {
    effective: {
      provider: 'dashscope',
      model: 'qwen3-max',
      context_size: 131072,
      context_auto: true,
      temperature: null,
      max_tokens: null,
      top_p: null,
      reasoning_effort: null,
      thinking_enable: null,
      parallel_tool_calls: true,
      max_iters: 12,
      model_max_retries: 3,
      agent_max_retries: 3,
      trigger_ratio: 0.8,
      reserve_ratio: 0.1,
      context_buffer_ratio: 0.2,
    },
    sources: {
      provider: 'stored',
      model: 'stored',
      context_size: 'discovered',
      max_iters: 'framework-default',
    },
    bounds: {
      context_size: { kind: 'int', nullable: false, min: 8192, max: 2000000 },
      max_iters: { kind: 'int', nullable: false, min: 1, max: 200 },
      temperature: { kind: 'float', nullable: true, min: 0, max: 2 },
      top_p: { kind: 'float', nullable: true, min: 0, max: 1 },
    },
    editable: ['provider', 'model', 'context_size', 'max_iters'],
    readonly: { api_key: '环境变量 DEVOPS_AGENT_API_KEY（重启生效）' },
    derived: {
      compression_threshold: 104858,
      worst_case_attempts: 16,
      context_uncalibrated: false,
      context_source: 'discovered',
      context_card_may_be_stale: false,
    },
    warnings: [],
    version: 7,
    file_path: 'E:/repo/config/model.local.yaml',
    catalog: {
      providers_configured: 2,
      provider: 'dashscope',
      current: {
        matched_by: 'exact',
        context_source: 'discovered',
        card_may_be_stale: false,
        status: 'stable',
        deprecated_at: null,
        label: 'Qwen3 Max',
      },
    },
    overrides: {},
    ...over,
  };
}

export function layers(): ModelLayers {
  return {
    version: 7,
    file_path: 'E:/repo/config/model.local.yaml',
    layers: [
      {
        id: 'session',
        label: '本次会话',
        writable: true,
        values: {},
      },
      {
        id: 'file',
        label: 'config/model.local.yaml',
        writable: true,
        values: { model: 'qwen3-max' },
      },
      {
        id: 'base',
        label: 'yaml / env / 框架默认',
        writable: false,
        values: { context_size: 131072 },
      },
    ],
    effective: modelConfig().effective,
    sources: {},
  };
}

export const PROVIDERS: ProvidersPayloadVersioned = {
  version: '1f3a9c2b7e04',
  default: 'dashscope',
  anonymous: false,
  enabled: true,
  kinds: [{ kind: 'openai_chat', family: 'openai' }, { kind: 'gemini_chat', family: 'gemini' }],
  items: [
    {
      id: 'dashscope',
      kind: 'openai_chat',
      label: '百炼',
      base_url_host: 'dashscope.aliyuncs.com',
      discover: 'models',
      has_key: true,
      key_fingerprint: '70c4551b',
      models: 53,
      last_check: { at: '2026-11-28T03:00:00Z', code: 'ok' },
    },
    {
      id: 'vllm',
      kind: 'openai_chat',
      label: '内网 vLLM',
      base_url_host: '127.0.0.1',
      discover: 'manual',
      has_key: false,
      key_fingerprint: '',
      models: 0,
      last_check: null,
    },
  ],
};

export const AGGREGATE: ModelsAggregate = {
  items: [
    {
      provider: 'dashscope',
      provider_label: '百炼',
      model: 'qwen3-max',
      label: 'Qwen3 Max',
      context: 262144,
      status: 'stable',
      capabilities: ['chat'],
      source: 'discovered',
    },
    {
      provider: 'dashscope',
      provider_label: '百炼',
      model: 'qwen-flash',
      label: 'Qwen Flash',
      context: 131072,
      status: 'stable',
      capabilities: ['chat'],
      source: 'discovered',
    },
    {
      provider: 'vllm',
      provider_label: '内网 vLLM',
      model: 'glm-4',
      label: 'GLM 4',
      context: null,
      status: 'stable',
      capabilities: ['chat'],
      source: 'manual',
    },
  ],
  providers: [
    { id: 'dashscope', label: '百炼', code: 'ok', total: 2, stale: false, missing_key: false },
    { id: 'vllm', label: '内网 vLLM', code: 'ok', total: 1, stale: false, missing_key: true },
  ],
  default: { provider: 'dashscope', model: 'qwen3-max' },
  truncated: false,
};
