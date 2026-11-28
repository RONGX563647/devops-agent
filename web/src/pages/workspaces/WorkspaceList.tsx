/** 左栏：工作区列表（搜索、活跃/归档分栏、新建入口）。
 *
 * 行的第二行只放「来源 + 会话数」两件事：`root_path` 太长，压到详情页去展示。
 */

import { Link } from 'react-router-dom';
import type { WorkspaceStatus, WorkspaceSummary } from '../../types';
import { formatAgo } from '../../workspace/time';
import { FolderIcon, GitIcon, PlusIcon, SearchIcon } from './icons';
import { isGit, sourceDetail, sourceLabel } from './meta';

interface Props {
  items: WorkspaceSummary[];
  activeId: string | null;
  onSelect: (id: string) => void;
  loading: boolean;
  failed: boolean;
  query: string;
  onQuery: (value: string) => void;
  filter: WorkspaceStatus;
  onFilter: (value: WorkspaceStatus) => void;
  counts: { active: number; archived: number };
  quota: { used: number; max: number } | null;
  onCreate: () => void;
}

function Row({
  item,
  active,
  onSelect,
}: {
  item: WorkspaceSummary;
  active: boolean;
  onSelect: (id: string) => void;
}) {
  const archived = item.status === 'archived';
  const sessions = item.session_count ?? 0;
  return (
    <button
      type="button"
      className={`wsp-item${active ? ' is-active' : ''}${archived ? ' is-archived' : ''}`}
      onClick={() => onSelect(item.id)}
      aria-current={active ? 'true' : undefined}
      title={`${item.name} · ${sourceLabel(item)} · ${sessions} 个会话`}
    >
      <span className={`wsp-item-icon${isGit(item) ? ' is-git' : ''}`}>
        {isGit(item) ? <GitIcon /> : <FolderIcon />}
      </span>
      <span className="wsp-item-body">
        <span className="wsp-item-name">
          {item.name}
          {archived ? <span className="wsp-pill">已归档</span> : null}
        </span>
        <span className="wsp-item-meta">
          {sourceDetail(item)} · {sessions} 个会话
        </span>
      </span>
      <span className="wsp-item-time">{formatAgo(item.updated_at)}</span>
    </button>
  );
}

export function WorkspaceList({
  items,
  activeId,
  onSelect,
  loading,
  failed,
  query,
  onQuery,
  filter,
  onFilter,
  counts,
  quota,
  onCreate,
}: Props) {
  return (
    <aside className="wsp-rail" aria-label="工作区列表">
      <div className="wsp-rail-head">
        <h2 className="wsp-rail-title">
          工作区
          <span className="wsp-rail-count">{counts.active + counts.archived}</span>
        </h2>
        <button type="button" className="wsp-icon-btn" onClick={onCreate} title="新建工作区">
          <PlusIcon />
          <span className="sr-only">新建工作区</span>
        </button>
      </div>

      <label className="wsp-search">
        <SearchIcon />
        <input
          type="search"
          value={query}
          onChange={(event) => onQuery(event.target.value)}
          placeholder="搜索名称或路径"
          aria-label="搜索工作区"
        />
      </label>

      <div className="wsp-seg" role="tablist" aria-label="工作区状态">
        {(
          [
            ['active', '活跃', counts.active],
            ['archived', '已归档', counts.archived],
          ] as const
        ).map(([value, label, count]) => (
          <button
            key={value}
            type="button"
            role="tab"
            aria-selected={filter === value}
            className={`wsp-seg-btn${filter === value ? ' is-active' : ''}`}
            onClick={() => onFilter(value)}
          >
            {label}
            <span className="wsp-seg-count">{count}</span>
          </button>
        ))}
      </div>

      <div className="wsp-list">
        {loading ? <p className="wsp-rail-note">加载中…</p> : null}
        {failed ? <p className="wsp-rail-note is-error">加载失败：确认后端已重启并开启工作区</p> : null}
        {!loading && !failed && items.length === 0 ? (
          <p className="wsp-rail-note">
            {query.trim()
              ? `没有匹配「${query.trim()}」的工作区`
              : filter === 'archived'
                ? '还没有归档的工作区'
                : counts.archived > 0
                  ? `没有活跃的工作区，「已归档」里还有 ${counts.archived} 个`
                  : '还没有工作区，点右上角 + 新建'}
          </p>
        ) : null}
        {items.map((item) => (
          <Row key={item.id} item={item} active={item.id === activeId} onSelect={onSelect} />
        ))}
      </div>

      <div className="wsp-rail-foot">
        <span>
          配额 {quota ? `${quota.used} / ${quota.max}` : '—'}
        </span>
        <Link to="/?tab=settings&domain=workspace" className="wsp-rail-foot-link">
          白名单设置
        </Link>
      </div>
    </aside>
  );
}
