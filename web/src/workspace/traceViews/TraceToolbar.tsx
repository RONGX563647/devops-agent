/** 轨迹工具栏：维度切换、仅看异常、搜索。
 *
 * 折叠/展开不在这里出按钮：点「轮次」自身就是全部折叠 ↔ 全部展开的开关，
 * 少两个按钮也少一处「按钮和胶囊状态不一致」的可能。
 */

import type { RefObject } from 'react';
import { MODE_HINTS, MODES, type TraceMode } from './modes';

export function TraceToolbar({
  layout,
  onSelectMode,
  onlyErrors,
  onToggleOnlyErrors,
  query,
  onQueryChange,
  searchRef,
}: {
  layout: TraceMode;
  onSelectMode: (mode: TraceMode) => void;
  onlyErrors: boolean;
  onToggleOnlyErrors: () => void;
  query: string;
  onQueryChange: (value: string) => void;
  searchRef: RefObject<HTMLInputElement>;
}) {
  return (
    <div className="trace-toolbar">
      <div className="trace-modes" role="tablist" aria-label="轨迹维度">
        {MODES.map((item) => (
          <button
            key={item.key}
            role="tab"
            aria-selected={layout === item.key}
            className={layout === item.key ? 'active' : ''}
            title={MODE_HINTS[item.key]}
            onClick={() => onSelectMode(item.key)}
          >
            {item.label}
          </button>
        ))}
      </div>
      <button
        type="button"
        className={`trace-filter${onlyErrors ? ' active' : ''}`}
        aria-pressed={onlyErrors}
        title="只列出调用异常的步骤（失败的工具、模型与异常行）"
        onClick={onToggleOnlyErrors}
      >
        仅看异常
      </button>
      <input
        ref={searchRef}
        className="session-search trace-search"
        type="search"
        value={query}
        onChange={(e) => onQueryChange(e.target.value)}
        placeholder="搜索步骤、工具、参数…（/）"
        aria-label="搜索轨迹"
      />
    </div>
  );
}
