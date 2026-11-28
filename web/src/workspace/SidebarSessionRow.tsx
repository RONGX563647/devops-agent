/** 侧栏的会话行与行内改名输入。 */
import { useEffect, useRef } from 'react';
import { formatAgoShort } from './time';
import type { SessionItem } from './sidebar-utils';
import { MoreIcon } from './SidebarIcons';

/** 行内改名：Enter / 失焦提交，Esc 取消；提交后自身卸载，不会重复触发。 */
export function RenameInput({
  value,
  label,
  onCommit,
  onCancel,
}: {
  value: string;
  label: string;
  onCommit: (next: string) => void;
  onCancel: () => void;
}) {
  const ref = useRef<HTMLInputElement>(null);
  const done = useRef(false);

  useEffect(() => {
    ref.current?.focus();
    ref.current?.select();
  }, []);

  function finish(commit: boolean) {
    if (done.current) return;
    done.current = true;
    const text = (ref.current?.value ?? '').trim();
    if (commit && text && text !== value) onCommit(text);
    else onCancel();
  }

  return (
    <input
      ref={ref}
      className="session-rename"
      defaultValue={value}
      aria-label={label}
      onKeyDown={(event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          finish(true);
        } else if (event.key === 'Escape') {
          event.preventDefault();
          finish(false);
        }
      }}
      onBlur={() => finish(true)}
    />
  );
}

/** 一行会话：标题 + 右对齐的相对时间；归档置灰，计数留在悬浮提示里。 */
export function SessionRow({
  item,
  active,
  renaming,
  menu,
  onSelect,
  onMenu,
  onRename,
  onRenameDone,
  onToggleArchive,
}: {
  item: SessionItem;
  active: boolean;
  renaming: boolean;
  menu: boolean;
  onSelect: () => void;
  onMenu: () => void;
  onRename: () => void;
  onRenameDone: (title: string | null) => void;
  onToggleArchive: () => void;
}) {
  const meta = [
    `${item.runCount} 次运行`,
    item.failedCount ? `${item.failedCount} 次失败` : '',
    item.archived ? '已归档' : '',
  ]
    .filter(Boolean)
    .join(' · ');

  return (
    <li className={`session-row${active ? ' is-active' : ''}${item.archived ? ' is-archived' : ''}`}>
      {renaming ? (
        <RenameInput
          value={item.title}
          label={`重命名会话「${item.title}」`}
          onCommit={(title) => onRenameDone(title)}
          onCancel={() => onRenameDone(null)}
        />
      ) : (
        <>
          <button
            className={`session-item${active ? ' active' : ''}${item.draft ? ' is-draft' : ''}`}
            onClick={onSelect}
            title={`${item.id}${meta ? ` · ${meta}` : ''}`}
          >
            <span className="session-name">{item.title}</span>
            <span className="session-time">
              {item.draft ? '未发送' : formatAgoShort(item.lastAt)}
            </span>
          </button>
          <button
            className="session-more"
            onClick={onMenu}
            aria-label={`「${item.title}」的更多操作`}
            aria-haspopup="menu"
            aria-expanded={menu}
          >
            <MoreIcon />
          </button>
          {menu && (
            <div className="session-menu" role="menu">
              <button role="menuitem" onClick={onRename}>
                重命名
              </button>
              <button role="menuitem" onClick={onToggleArchive}>
                {item.archived ? '恢复会话' : '归档会话'}
              </button>
            </div>
          )}
        </>
      )}
    </li>
  );
}
