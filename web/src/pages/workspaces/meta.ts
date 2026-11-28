/** 工作区页的文案与纯函数：来源标签、路径压缩、领域错误 → 人话。
 *
 * 错误码是后端稳定契约（`devops_agent/workspace/errors.py`），文案只在前端维护，
 * 这样「目录不在白名单」这类判定不会被抄成第二份逻辑。
 */

import { ApiError } from '../../api/http';
import type { WorkspaceSummary } from '../../types';

/** 长期记忆的 kind 取值（`memory/summarize.py`）→ 中文标签。 */
export const MEMORY_KIND_LABEL: Record<string, string> = {
  fact: '事实',
  procedure: '流程',
  decision: '决策',
};

export function isGit(item: Pick<WorkspaceSummary, 'source'> | undefined): boolean {
  return item?.source === 'git';
}

/** 来源的短标签，用在列表徽标与详情小标题。 */
export function sourceLabel(item: Pick<WorkspaceSummary, 'source'> | undefined): string {
  if (isGit(item)) return 'Git 仓库';
  if (item?.source === 'worktree') return '本地工作树';
  if (item?.source === 'local') return '本地目录';
  return '来源未知';
}

/** 列表第二行：工作树/克隆带分支名，原地引用说明「不复制」。 */
export function sourceDetail(item: Pick<WorkspaceSummary, 'source' | 'branch'>): string {
  if (isGit(item)) return item.branch ? `受管克隆 · ${item.branch}` : '受管克隆';
  if (item.source === 'worktree') {
    return item.branch ? `工作树 · ${item.branch}` : '工作树';
  }
  return '原地引用';
}

/** 只留最后 `keep` 段：整条 Windows 路径在列表里读不出重点。 */
export function shortPath(path: string, keep = 2): string {
  const parts = String(path ?? '')
    .split(/[\\/]+/)
    .filter(Boolean);
  if (parts.length === 0) return path;
  return parts.slice(-keep).join('/');
}

/** 路径末段：本地目录名 / Git 仓库名，用来给「名称」预填。 */
export function basename(path: string): string {
  const parts = String(path ?? '')
    .split(/[\\/]+/)
    .filter(Boolean);
  return parts[parts.length - 1] ?? '';
}

/** `https://host/org/repo.git` 与 `git@host:org/repo.git` 都取 `repo`。 */
export function repoName(url: string): string {
  const text = String(url ?? '').trim().replace(/\/+$/, '');
  if (!text) return '';
  const tail = text.split(/[/:]/).filter(Boolean).pop() ?? '';
  return tail.replace(/\.git$/i, '');
}

const ERROR_TEXT: Record<string, string> = {
  workspace_disabled: '部署未开启工作区功能（enabled=false）',
  workspace_not_found: '工作区不存在，可能已被删除',
  workspace_name_taken: '同名工作区已存在，换个名字',
  workspace_path_taken: '该目录已被其他工作区引用，先去那边删掉',
  workspace_local_disabled: '部署未开启本地目录导入（allow_local_import=false）',
  workspace_git_disabled: '部署未开启 Git 导入（allow_git_import=false）',
  workspace_path_invalid: '目录不可用：不存在、没有权限，或不在白名单内',
  workspace_already_bound: '该会话已绑定到别的工作区',
  workspace_archived: '工作区已归档，恢复后才能继续使用',
  workspace_limit: '工作区数量已达部署上限，先归档或删除不用的',
  workspace_dirty: '受管克隆还有未提交改动：先提交或丢弃，或确认强制删除',
  workspace_in_use: '还有会话绑定这个工作区：先解绑，或确认强制删除',
  workspace_git_failed: '克隆失败：检查 URL、分支名与凭据',
  workspace_source_invalid: '来源参数不合法：Git 只支持 https / ssh / git@host:path；工作树需要 git 仓库',
  workspace_root_missing: '工作区的目录不在了（被删或盘符没挂上）：恢复目录，或删除这个工作区重新创建',
};

/** 结构化 detail 里的补充信息（脏文件、占用会话）也要露出来。 */
function extraOf(detail: Record<string, unknown>): string {
  const files = detail['changed_files'];
  if (Array.isArray(files) && files.length > 0) {
    const shown = files.slice(0, 5).join('、');
    return `（${shown}${files.length > 5 ? ` 等 ${files.length} 个文件` : ''}）`;
  }
  const sessions = detail['sessions'];
  if (Array.isArray(sessions) && sessions.length > 0) {
    return `（占用会话：${sessions.slice(0, 3).join('、')}）`;
  }
  return '';
}

/** 字符串 detail 就是错误码：认识的翻成人话，不认识的交给调用方回落。 */
function fromCode(code: string): string | null {
  return ERROR_TEXT[code] ?? null;
}

/** 结构化 detail：`{code,message,changed_files?,sessions?}`。 */
function fromObject(detail: Record<string, unknown>): string | null {
  const code = typeof detail['code'] === 'string' ? detail['code'] : '';
  const base = ERROR_TEXT[code] ?? (typeof detail['message'] === 'string' ? detail['message'] : '');
  return base ? `${base}${extraOf(detail)}` : null;
}

/** 任意异常 → 一句能指导下一步的中文。 */
export function errorText(exc: unknown): string {
  if (!(exc instanceof ApiError)) {
    return exc instanceof Error ? exc.message : '操作失败，请重试';
  }
  const detail = exc.detail;
  let message: string | null = null;
  if (typeof detail === 'string') message = fromCode(detail);
  else if (detail && typeof detail === 'object') {
    message = fromObject(detail as Record<string, unknown>);
  }
  if (message) return message;
  return exc.status === 404 ? '后端没有工作区路由：确认已升级并重启服务' : exc.message;
}
