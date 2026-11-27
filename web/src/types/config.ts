/** 配置中心（ADR-0005）：域清单、快照、边界。

`values` 是嵌套对象，但界面按点号路径逐行编辑；`sources` 与 `bounds` 也用同一套
路径作键，三者对齐才能画出「这一行的值从哪来、能填多少」。
*/

/** 某一行的值来自哪一层；`env` 的键在界面上锁定（写了也赢不了容器）。 */
export type ConfigSource = 'env' | 'db' | 'yaml' | 'default';

/** 生效时机：面板如实显示，不谎报热更新。 */
export type ConfigHot = 'immediate' | 'next_turn' | 'restart';

export interface ConfigDomainSummary {
  domain: string;
  title: string;
  writable: boolean;
  hot: ConfigHot;
  note: string;
  /** 非空表示写入口在别处（如模型抽屉），本页只读。 */
  managed_by: string;
  version: number;
  overrides: number;
  env_locked: string[];
}

/** 配置库健康：读不到覆盖时所有域静默回落代码默认值，界面必须能说出来。 */
export interface ConfigHealth {
  /** db / file；环境变量拼错时 /health 报 invalid */
  mode?: string;
  /** 最近一次读库失败；正常时为空串。缺省视为「没坏」（兼容旧服务端）。 */
  db_error?: string;
  degraded?: boolean;
  /** /health 用的错误原文（与 db_error 同义，仅命名不同） */
  error?: string;
}

export interface ConfigIndex extends ConfigHealth {
  mode: string;
  db_path: string;
  domains: ConfigDomainSummary[];
}

/** 单个键的边界：数值区间（闭/开）与枚举选项。 */
export interface ConfigBound {
  min?: number;
  max?: number;
  min_exclusive?: number;
  max_exclusive?: number;
  options?: string[];
}

export interface ConfigDomain extends ConfigHealth {
  domain: string;
  title: string;
  values: Record<string, unknown>;
  defaults: Record<string, unknown>;
  sources: Record<string, ConfigSource>;
  bounds: Record<string, ConfigBound>;
  editable: string[];
  env_locked: string[];
  writable: boolean;
  managed_by: string;
  hot: ConfigHot;
  note: string;
  version: number;
  /** 代码默认升级过、但旧覆盖仍生效的键。 */
  drifted: string[];
  mode: string;
  db_path: string;
  /** 本次写入是否真的落库（写成与基线相同的值时为 false） */
  persisted?: boolean;
}

export interface ConfigAuditEntry {
  key: string;
  old: unknown;
  new: unknown;
  version: number;
  at: string;
  by: string;
}
