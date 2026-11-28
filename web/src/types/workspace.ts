/** 工作区（SPEC-0020）与后端 Pydantic 响应对齐的前端类型。 */

export type WorkspaceStatus = 'active' | 'archived';
export type WorkspaceSource = 'local' | 'git';

export interface WorkspaceSummary {
  id: string;
  name: string;
  owner: string;
  root_path: string;
  default_profile: string;
  status: WorkspaceStatus;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
  source: WorkspaceSource | '';
  managed: boolean;
  remote_url: string;
  branch: string;
  base_commit: string;
  detected_git: boolean;
  session_count?: number;
  long_term_count?: number;
}

export interface WorkspaceList {
  items: WorkspaceSummary[];
}

export interface WorkspaceControl {
  enabled: boolean;
  allow_local_import: boolean;
  allow_git_import: boolean;
  allowed_local_roots: string[];
  git_available: boolean;
  max_workspaces: number;
  default_profile: string;
}

export interface BrowseItem {
  name: string;
  path: string;
}

export interface BrowseResult {
  root: string;
  items: BrowseItem[];
  /** 上一个可进入的目录；已到白名单根时为 null（由后端判定，前端不拼路径）。 */
  parent?: string | null;
  /** 白名单根的 realpath，用于「一键跳转」。 */
  roots?: string[];
}

export interface LocalWorkspaceSource {
  kind: 'local';
  path: string;
}

export interface GitWorkspaceSource {
  kind: 'git';
  url: string;
  branch?: string;
  credential_ref?: string;
  depth?: number;
}

/** 固定 workspace scope 的长期记忆条目；只取展示需要的字段。 */
export interface WorkspaceMemoryItem {
  id: number;
  kind: string;
  text: string;
  hits: number;
  created_at: string;
}

export interface WorkspaceMemoryPage {
  workspace_id: string;
  scope: string;
  scope_id: string;
  items: WorkspaceMemoryItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface CreateWorkspaceRequest {
  name: string;
  default_profile?: string;
  source: LocalWorkspaceSource | GitWorkspaceSource;
}
