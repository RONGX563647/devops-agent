/** 侧栏的一个工作区块：标题行（折叠 / 改名 / 新建 / 菜单）+ 会话列表。 */
import { ChevronIcon, FolderIcon, MoreIcon, PlusIcon } from './SidebarIcons';
import { RenameInput, SessionRow } from './SidebarSessionRow';
import type { SessionItem } from './sidebar-utils';
import type { WorkspaceGroupView } from './sidebar-workspaces';

interface Props {
  group: WorkspaceGroupView;
  active: boolean;
  collapsed: boolean;
  selected: string | null;
  menu: string | null;
  editing: string | null;
  menuKey: (kind: 'ws' | 'session', id: string) => string;
  onToggle: () => void;
  onNew: () => void;
  onMenu: (key: string | null) => void;
  onEdit: (key: string | null) => void;
  onSelect: (id: string | null) => void;
  onRenameWorkspace: (name: string) => void;
  onToggleWorkspaceArchive: () => void;
  onRenameSession: (id: string, title: string) => void;
  onToggleSessionArchive: (item: SessionItem) => void;
}

export function WorkspaceBlock({
  group,
  active,
  collapsed,
  selected,
  menu,
  editing,
  menuKey,
  onToggle,
  onNew,
  onMenu,
  onEdit,
  onSelect,
  onRenameWorkspace,
  onToggleWorkspaceArchive,
  onRenameSession,
  onToggleSessionArchive,
}: Props) {
  const wsKey = menuKey('ws', group.id);
  const renamingWs = editing === wsKey;
  const realSessions = group.sessions.filter((item) => !item.draft);
  const hidden = group.count - realSessions.length;

  return (
    <li className={`session-ws${group.archived ? ' is-archived' : ''}${active ? ' is-current' : ''}`}>
      <div className="session-ws-row">
        <button
          className="session-ws-head"
          onClick={onToggle}
          aria-expanded={!collapsed}
          title={group.rootPath || group.name}
        >
          <ChevronIcon open={!collapsed} />
          {renamingWs ? (
            <RenameInput
              value={group.name}
              label={`重命名工作区「${group.name}」`}
              onCommit={onRenameWorkspace}
              onCancel={() => onEdit(null)}
            />
          ) : (
            <>
              <span className="session-ws-icon">
                <FolderIcon />
              </span>
              <span className="session-ws-name">{group.name}</span>
              {group.archived && <span className="session-ws-tag">归档</span>}
            </>
          )}
        </button>
        <div className="session-ws-actions">
          <button
            className="session-more"
            onClick={onNew}
            aria-label={`在「${group.name}」中新建会话`}
            title="在这个工作区新建会话"
          >
            <PlusIcon />
          </button>
          <button
            className="session-more"
            onClick={() => onMenu(menu === wsKey ? null : wsKey)}
            aria-label={`「${group.name}」的更多操作`}
            aria-haspopup="menu"
            aria-expanded={menu === wsKey}
          >
            <MoreIcon />
          </button>
        </div>
        {menu === wsKey && (
          <div className="session-menu" role="menu">
            <button role="menuitem" onClick={onNew}>
              新建会话
            </button>
            <button
              role="menuitem"
              onClick={() => {
                onMenu(null);
                onEdit(wsKey);
              }}
            >
              重命名
            </button>
            <button role="menuitem" onClick={onToggleWorkspaceArchive}>
              {group.archived ? '恢复工作区' : '归档工作区'}
            </button>
          </div>
        )}
      </div>

      {!collapsed && (
        <>
          {group.sessions.length > 0 ? (
            <ul className="session-ws-sessions">
              {group.sessions.map((item) => (
                <SessionRow
                  key={item.id}
                  item={item}
                  active={selected === item.id}
                  renaming={editing === menuKey('session', item.id)}
                  menu={menu === menuKey('session', item.id)}
                  onSelect={() => onSelect(item.id)}
                  onMenu={() =>
                    onMenu(
                      menu === menuKey('session', item.id) ? null : menuKey('session', item.id),
                    )
                  }
                  onRename={() => {
                    onMenu(null);
                    onEdit(menuKey('session', item.id));
                  }}
                  onRenameDone={(title) => {
                    if (title) onRenameSession(item.id, title);
                    else onEdit(null);
                  }}
                  onToggleArchive={() => onToggleSessionArchive(item)}
                />
              ))}
            </ul>
          ) : (
            <p className="session-hint">还没有会话</p>
          )}
          {!group.archived && hidden > 0 && !menu && (
            <p className="session-hint">还有 {hidden} 条，用搜索查找</p>
          )}
        </>
      )}
    </li>
  );
}
