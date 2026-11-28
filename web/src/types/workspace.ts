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

export interface CreateWorkspaceRequest {
  name: string;
  default_profile?: string;
  source: LocalWorkspaceSource | GitWorkspaceSource;
}
