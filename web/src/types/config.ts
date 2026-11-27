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

export interface ConfigIndex {
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

export interface ConfigDomain {
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
}

export interface ConfigAuditEntry {
  key: string;
  old: unknown;
  new: unknown;
  version: number;
  at: string;
  by: string;
}
