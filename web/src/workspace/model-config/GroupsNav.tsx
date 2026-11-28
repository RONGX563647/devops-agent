/** 左侧分类树：一级是分组，二级是字段。
 *
 * 抽出来的原因只是行数：抽屉本体已经到 500 行的硬上限，而这段是纯展示 ——
 * 不取数、不管保存，只把「当前在哪一组、哪一项、哪些还没保存」画出来。
 */

import type { ModelPatch } from '../../types';
import { GROUPS } from './groups';

interface Props {
  active: string;
  expanded: Record<string, boolean>;
  /** 从左树点进来的字段：高亮叶子。 */
  activeField: string | null;
  draft: ModelPatch;
  onGroup: (groupId: string) => void;
  onField: (groupId: string, key: string) => void;
}

export function GroupsNav({ active, expanded, activeField, draft, onGroup, onField }: Props) {
  return (
    <nav className="mcd-tree" aria-label="参数分类">
      <ul className="mcd-tree-list">
        {GROUPS.map((entry) => {
          const pending = entry.items.filter((item) => draft[item.key] !== undefined).length;
          return (
            <li key={entry.id}>
              <button
                type="button"
                className={`mcd-tree-group${active === entry.id ? ' is-active' : ''}`}
                aria-expanded={expanded[entry.id] ?? false}
                onClick={() => onGroup(entry.id)}
              >
                <span className="mcd-tree-caret" aria-hidden="true">
                  {expanded[entry.id] ? '▾' : '▸'}
                </span>
                <span>{entry.label}</span>
                {pending > 0 && (
                  <span className="mcd-dot" aria-label={`${pending} 项未保存`}>
                    {pending}
                  </span>
                )}
              </button>
              {expanded[entry.id] && (
                <ul className="mcd-tree-children">
                  {entry.items.map((item) => (
                    <li key={item.key}>
                      <button
                        type="button"
                        className={`mcd-tree-leaf${
                          activeField === item.key ? ' is-active' : ''
                        }`}
                        aria-current={activeField === item.key ? 'true' : undefined}
                        onClick={() => onField(entry.id, item.key)}
                      >
                        {item.label}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
