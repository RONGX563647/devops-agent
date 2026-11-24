/** 权限与挂起态：与后端 Pydantic 响应对齐的前端类型。 */

/** 权限档位标识：与 governance/profiles.py 的 key 一一对应。 */
export type PermissionKey = 'readonly' | 'workspace' | 'execute' | 'unrestricted' | 'auto';

/** 越界兜底动作：直接拒 / 弹确认 / deny_rules 硬拦。 */
export type PermissionFallback = 'deny' | 'ask' | 'hard_deny';

/** 一个可选档位的展示信息（GET /api/permissions）。 */
export interface PermissionMeta {
  key: PermissionKey;
  label: string;
  note: string;
  fallback: PermissionFallback;
  /** 是否挂了 Write/Edit —— 界面据此显示「可写」标记。 */
  write: boolean;
}

export interface PermissionCatalog {
  default: PermissionKey;
  available: PermissionMeta[];
}

/** 一条等待确认的调用。input 是服务端给的展示文本，不是可回传的命令。 */
export interface PendingCall {
  tool_call_id: string;
  name: string;
  input: string;
  risk: 'read' | 'exec' | 'write' | 'danger';
}

/** 确认作用域：只这次 / 精确这条 / 这个命令前缀。 */
export type ConfirmScope = 'once' | 'exact' | 'prefix';

export interface ConfirmDecision {
  tool_call_id: string;
  confirmed: boolean;
  scope: ConfirmScope;
}

/** 挂起信息：GET /run/{id}/pending 的返回。 */
export interface PendingState {
  run_id: number;
  status: 'parked';
  expires_at: string | null;
  tool_calls: PendingCall[];
}

/** 前端内存里的挂起态：挂在助手消息上，刷新后仍能重建卡片。 */
export interface AwaitingState {
  runId: number | null;
  calls: PendingCall[];
  expiresAt: string | null;
}
