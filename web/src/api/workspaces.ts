/** 工作区 API（SPEC-0020）：只做契约封装，状态与缓存交给调用页面。 */

import type {
  BrowseResult,
  CreateWorkspaceRequest,
  WorkspaceList,
  WorkspaceSummary,
} from '../types';
import { getJson, sendJson } from './http';

export function fetchWorkspaces(): Promise<WorkspaceList> {
  return getJson<WorkspaceList>('/api/workspaces');
}

export function fetchWorkspace(id: string): Promise<WorkspaceSummary> {
  return getJson<WorkspaceSummary>(`/api/workspaces/${encodeURIComponent(id)}`);
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
