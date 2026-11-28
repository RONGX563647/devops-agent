/** 右栏：工作区详情（代码位置、统计、生命周期操作）。
 *
 * 删除的语义按来源分开说：本地引用只注销登记，受管克隆会清理副本。
 * 这两句话必须放在确认框里，否则「删除」两个字没法预判后果。
 */

import { useState } from 'react';
import { Link } from 'react-router-dom';
import type { WorkspaceMemoryItem, WorkspaceSessionItem, WorkspaceSummary } from '../../types';
import { formatAgo, formatDateTime } from '../../workspace/time';
import { ArchiveIcon, CheckIcon, CopyIcon, GitIcon, FolderIcon, RestoreIcon, TrashIcon } from './icons';
import { MEMORY_KIND_LABEL, errorText, isGit, shortPath, sourceLabel } from './meta';

interface Props {
  item: WorkspaceSummary | undefined;
  loading: boolean;
  memories: WorkspaceMemoryItem[];
  memoriesLoading: boolean;
  /** 归属该工作区的最近会话（分页口径与左栏一致，最近运行优先）。 */
  sessions: WorkspaceSessionItem[];
  sessionsLoading: boolean;
  onArchive: () => Promise<void>;
  onRestore: () => Promise<void>;
  /** `force` 跳过脏文件 / 绑定会话的 409；只有在用户显式勾选后才传 true。 */
  onDelete: (force: boolean) => Promise<void>;
}

function DetailSkeleton() {
  return (
    <div className="wsp-detail" aria-busy="true">
      <div className="wsp-skel wsp-skel-title" />
      <div className="wsp-skel wsp-skel-line" />
      <div className="wsp-skel wsp-skel-card" />
    </div>
  );
}

export function WorkspaceDetail({
  item,
  loading,
  memories,
  memoriesLoading,
  sessions: recent,
  sessionsLoading,
  onArchive,
  onRestore,
  onDelete,
}: Props) {
  const [copied, setCopied] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [force, setForce] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');

  if (loading || !item) return <DetailSkeleton />;

  const git = isGit(item);
  const archived = item.status === 'archived';
  const sessions = item.session_count ?? 0;
  const memoryCount = item.long_term_count ?? 0;

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setNotice('');
    try {
      await action();
    } catch (exc) {
      setNotice(errorText(exc));
    } finally {
      setBusy(false);
    }
  }

  async function copyPath() {
    try {
      await navigator.clipboard.writeText(item?.root_path ?? '');
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setNotice('复制失败：浏览器拒绝访问剪贴板，请手动选中路径');
    }
  }

  return (
    <div className="wsp-detail">
      <header className="wsp-detail-head">
        <div className="wsp-detail-title">
          <p className="wsp-crumb">
            工作区<span aria-hidden="true">/</span>
            {sourceLabel(item)}
          </p>
          <h1>
            {item.name}
            {archived ? <span className="wsp-pill is-warn">已归档</span> : null}
          </h1>
          <p className="wsp-lede">
            {git
              ? '从远端克隆到受管目录，删除工作区会连带清理副本。'
              : '原地引用已有目录，删除工作区只注销登记，不动文件。'}
          </p>
        </div>
        <div className="wsp-detail-actions">
          {archived ? (
            <button
              type="button"
              className="wsp-btn"
              disabled={busy}
              onClick={() => void run(onRestore)}
            >
              <RestoreIcon />
              恢复
            </button>
          ) : (
            <button
              type="button"
              className="wsp-btn"
              disabled={busy}
              onClick={() => void run(onArchive)}
            >
              <ArchiveIcon />
              归档
            </button>
          )}
          <button
            type="button"
            className="wsp-btn is-danger"
            disabled={busy}
            onClick={() => setConfirming(true)}
          >
            <TrashIcon />
            删除
          </button>
        </div>
      </header>

      {notice ? (
        <p className="wsp-alert" role="alert">
          {notice}
        </p>
      ) : null}

      <section className="wsp-card" aria-labelledby="wsp-code-title">
        <div className="wsp-card-head">
          <h2 id="wsp-code-title">代码位置</h2>
          <span className={`wsp-card-tag${git ? ' is-git' : ''}`}>
            {git ? <GitIcon size={13} /> : <FolderIcon size={13} />}
            {git ? '受管克隆' : item.source === 'worktree' ? '工作树' : '原地引用'}
          </span>
        </div>
        <div className="wsp-path">
          <code title={item.root_path}>{item.root_path}</code>
          <button type="button" className="wsp-icon-btn" onClick={() => void copyPath()}>
            {copied ? <CheckIcon /> : <CopyIcon />}
            <span className="sr-only">{copied ? '已复制' : '复制路径'}</span>
          </button>
        </div>
        <p className="wsp-field-hint">
          会话就在这个目录里执行：shell 的工作目录与文件工具的路径基准都是它。
          {item.source === 'worktree' ? '这是主仓库的一个工作树，你的检出不受影响。' : ''}
        </p>
        <dl className="wsp-dl">
          {git ? (
            <>
              <div>
                <dt>远端</dt>
                <dd className="wsp-mono">{item.remote_url || '—'}</dd>
              </div>
              <div>
                <dt>分支</dt>
                <dd className="wsp-mono">{item.branch || '默认分支'}</dd>
              </div>
              <div>
                <dt>基线提交</dt>
                <dd className="wsp-mono">{item.base_commit ? item.base_commit.slice(0, 10) : '—'}</dd>
              </div>
            </>
          ) : (
            <>
              <div>
                <dt>目录名</dt>
                <dd className="wsp-mono">{shortPath(item.root_path, 3)}</dd>
              </div>
              <div>
                <dt>Git 状态</dt>
                <dd>{item.detected_git ? `是 · ${item.branch || '未知分支'}` : '不是 Git 仓库'}</dd>
              </div>
            </>
          )}
          <div>
            <dt>登记时间</dt>
            <dd className="wsp-mono">{formatDateTime(item.created_at)}</dd>
          </div>
        </dl>
      </section>

      <div className="wsp-stats">
        <div className="wsp-stat">
          <span className="wsp-stat-value">{sessions}</span>
          <span className="wsp-stat-label">绑定会话</span>
        </div>
        <div className="wsp-stat">
          <span className="wsp-stat-value">{memoryCount}</span>
          <span className="wsp-stat-label">长期记忆</span>
        </div>
        <div className="wsp-stat">
          <span className="wsp-stat-value is-text">{item.default_profile || '—'}</span>
          <span className="wsp-stat-label">默认档位</span>
        </div>
      </div>

      <section className="wsp-card" aria-labelledby="wsp-memory-title">
        <div className="wsp-card-head">
          <h2 id="wsp-memory-title">长期记忆</h2>
          <span className="wsp-card-tag">
            {memoriesLoading
              ? '读取中…'
              : memories.length > 0
                ? `最近 ${memories.length} 条 / 共 ${memoryCount} 条`
                : `共 ${memoryCount} 条`}
          </span>
        </div>
        {memories.length > 0 ? (
          <ul className="wsp-memory">
            {memories.map((memory) => (
              <li key={memory.id}>
                <span className="wsp-memory-kind">{MEMORY_KIND_LABEL[memory.kind] ?? memory.kind}</span>
                <span className="wsp-memory-text" title={memory.text}>
                  {memory.text}
                </span>
                <span className="wsp-memory-time">{formatAgo(memory.created_at)}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="wsp-memory-empty">
            {memoriesLoading
              ? '正在读取这个工作区的长期记忆…'
              : '还没有长期记忆：对话里值得留下的结论会自动写进来，跨会话共享。'}
          </p>
        )}
      </section>

      <section className="wsp-card" aria-labelledby="wsp-sessions-title">
        <div className="wsp-card-head">
          <h2 id="wsp-sessions-title">最近会话</h2>
          <span className="wsp-card-tag">
            {sessionsLoading ? '读取中…' : `共 ${sessions} 个`}
          </span>
        </div>
        {recent.length > 0 ? (
          <ul className="wsp-sessions">
            {recent.map((session) => (
              <li key={session.session_id}>
                {/* 点进去直接续上这条会话：归属已落库，不需要再选工作区 */}
                <Link className="wsp-session-link" to={`/?session=${encodeURIComponent(session.session_id)}`}>
                  <span className="wsp-session-title" title={session.title}>
                    {session.title || '未命名会话'}
                  </span>
                  <span className="wsp-session-meta">
                    {session.run_count} 次运行
                    {session.failed_count ? ` · ${session.failed_count} 次失败` : ''}
                    {session.last_at ? ` · ${formatAgo(session.last_at)}` : ''}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <p className="wsp-memory-empty">
            {sessionsLoading
              ? '正在读取这个工作区的会话…'
              : '还没有会话：在左栏点这个工作区名，新开一条就会归到这里。'}
          </p>
        )}
      </section>

      <p className="wsp-foot-note">
        同一工作区的会话共享长期记忆，会话上下文仍各自隔离；工作区 ID
        <code>{item.id}</code>
      </p>

      {confirming ? (
        <div className="wsp-confirm" role="alertdialog" aria-labelledby="wsp-confirm-title">
          <h2 id="wsp-confirm-title">删除「{item.name}」？</h2>
          <p>
            {git
              ? '受管克隆会被一并清理；若还有未提交改动，会先返回脏文件列表而不是直接删。'
              : '只注销登记，目录与文件保持不动，随时可以重新引用。'}
            {sessions > 0 ? ` 当前有 ${sessions} 个会话绑定，绑定关系会一并解除。` : ''}
          </p>
          <label className="wsp-check">
            <input type="checkbox" checked={force} onChange={(event) => setForce(event.target.checked)} />
            强制删除：忽略未提交改动与绑定会话
          </label>
          <div className="wsp-confirm-actions">
            <button type="button" className="wsp-btn" onClick={() => setConfirming(false)}>
              取消
            </button>
            <button
              type="button"
              className="wsp-btn is-danger"
              disabled={busy}
              onClick={() => void run(async () => {
                await onDelete(force);
                setConfirming(false);
              })}
            >
              {busy ? '删除中…' : '确认删除'}
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
