/** 工作区 API（SPEC-0020）：只做契约封装，状态与缓存交给调用页面。 */

import type {
  BrowseResult,
  CreateWorkspaceRequest,
  WorkspaceControl,
  WorkspaceList,
  WorkspaceMemoryPage,
  WorkspaceStatus,
  WorkspaceSummary,
} from '../types';
import { getJson, sendJson } from './http';

/** `status` 为空表示活跃 + 已归档都返回；列表页按「活跃 / 已归档」分栏取。 */
export function fetchWorkspaces(status?: WorkspaceStatus): Promise<WorkspaceList> {
  const suffix = status ? `?status=${status}` : '';
  return getJson<WorkspaceList>(`/api/workspaces${suffix}`);
}

/** 能力自述：来源是否可用、白名单根、配额；界面据此禁用不可用入口。 */
export function fetchWorkspaceControl(): Promise<WorkspaceControl> {
  return getJson<WorkspaceControl>('/api/workspaces/control');
}

export function fetchWorkspace(id: string): Promise<WorkspaceSummary> {
  return getJson<WorkspaceSummary>(`/api/workspaces/${encodeURIComponent(id)}`);
}

/** 工作区固定 scope 的长期记忆（跨会话共享的那一份）。 */
export function fetchWorkspaceMemory(id: string, limit = 5): Promise<WorkspaceMemoryPage> {
  return getJson<WorkspaceMemoryPage>(
    `/api/workspaces/${encodeURIComponent(id)}/memory/long-term?limit=${limit}`,
  );
}

export function createWorkspace(req: CreateWorkspaceRequest): Promise<WorkspaceSummary> {
  return sendJson<WorkspaceSummary>('/api/workspaces', 'POST', req);
}

export function browseWorkspaceDirs(path = ''): Promise<BrowseResult> {
  const suffix = path ? `?path=${encodeURIComponent(path)}` : '';
  return getJson<BrowseResult>(`/api/workspaces/browse${suffix}`);
}

export function archiveWorkspace(id: string): Promise<WorkspaceSummary> {
  return sendJson<WorkspaceSummary>(
    `/api/workspaces/${encodeURIComponent(id)}/archive`,
    'POST',
  );
}

/** 取消归档：归档只停新绑定与新 run，长期记忆仍保留，恢复后原样可用。 */
export function restoreWorkspace(id: string): Promise<WorkspaceSummary> {
  return sendJson<WorkspaceSummary>(`/api/workspaces/${encodeURIComponent(id)}/restore`, 'POST');
}

export async function deleteWorkspace(id: string, force = false): Promise<void> {
  const suffix = force ? '?force=true' : '';
  await sendJson<unknown>(
    `/api/workspaces/${encodeURIComponent(id)}${suffix}`,
    'DELETE',
  );
}

export function bindSessionWorkspace(
  sessionId: string,
  workspaceId: string,
): Promise<{ session_id: string; workspace_id: string }> {
  return sendJson<{ session_id: string; workspace_id: string }>(
    `/api/session/${encodeURIComponent(sessionId)}/workspace`,
    'POST',
    { workspace_id: workspaceId },
  );
}
