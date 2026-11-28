/** 工作区（SPEC-0020）与后端 Pydantic 响应对齐的前端类型。 */

export type WorkspaceStatus = 'active' | 'archived';
// worktree = 本地仓库上开的工作树（主检出不动），见 workspace/creation.py
export type WorkspaceSource = 'local' | 'git' | 'worktree';

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

/** 落盘方式：`auto` = git 仓库开工作树、其余原地引用。 */
export type LocalLanding = 'auto' | 'worktree' | 'in_place';

export interface LocalWorkspaceSource {
  kind: 'local';
  path: string;
  /** 默认 `auto`：仓库开工作树，主检出不动。 */
  mode?: LocalLanding;
  /** 只对工作树生效：留空自动生成 `agent/<名字>-<后缀>`。 */
  branch?: string;
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

/** 一条「工作区下的会话」：标题是后端截断后的最早一条输入。 */
export interface WorkspaceSessionItem {
  session_id: string;
  title: string;
  /** 最近一次运行时间；刚绑定还没跑过时回落到 bound_at。 */
  last_at: string;
  run_count: number;
  failed_count: number;
  bound_at: string;
  bound_by: string;
  /** 非空表示已归档：前端据此置灰并在「显示已归档」时渲染。 */
  archived_at?: string;
  /** 归属工作区；空串表示未归属（老会话），此时它在部署默认目录里跑。 */
  workspace_id?: string;
  workspace_name?: string;
  /** 该会话的执行目录（工作区根）。 */
  root_path?: string;
  source?: string;
  branch?: string;
}

export interface WorkspaceSessionPage {
  workspace_id: string;
  items: WorkspaceSessionItem[];
  total: number;
  limit: number;
  offset: number;
}

/** 分组视图里的一栏：工作区本身 + 它最近的几条会话。 */
export interface WorkspaceSessionGroup extends WorkspaceSummary {
  session_count: number;
  sessions: WorkspaceSessionItem[];
}

/** 左栏一次拿全量分组；`unbound` 是没有归属行的老会话。 */
export interface WorkspaceSessionBoard {
  items: WorkspaceSessionGroup[];
  unbound: WorkspaceSessionItem[];
}
