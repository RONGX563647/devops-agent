/** 左栏图标：全部 16 网格细描边，尺寸与全站图标一致。
 *
 * 单独成文件是为了让面板组件留在行数棘轮内，也避免每个图标重写一遍 viewBox。
 */

export function SearchIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <circle cx="7" cy="7" r="3.9" stroke="currentColor" strokeWidth="1.4" />
      <path d="m10.2 10.2 2.6 2.6" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}

export function SlidersIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M2.6 4.6h10.8M2.6 11.4h10.8"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
      <circle cx="6.2" cy="4.6" r="1.5" fill="none" stroke="currentColor" strokeWidth="1.3" />
      <circle cx="10.4" cy="11.4" r="1.5" fill="none" stroke="currentColor" strokeWidth="1.3" />
    </svg>
  );
}

export function FolderPlusIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M2.2 4.7c0-.6.5-1.1 1.1-1.1h2.5l1.3 1.5h5.5c.6 0 1.1.5 1.1 1.1v5.5c0 .6-.5 1.1-1.1 1.1H3.3c-.6 0-1.1-.5-1.1-1.1V4.7Z"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
      <path
        d="M8 7.3v3.2M6.4 8.9h3.2"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
      />
    </svg>
  );
}

export function FolderIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M2.2 4.4c0-.6.5-1.1 1.1-1.1h2.7l1.3 1.5h5.4c.6 0 1.1.5 1.1 1.1v5.7c0 .6-.5 1.1-1.1 1.1H3.3c-.6 0-1.1-.5-1.1-1.1V4.4Z"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg
      className={`session-chevron${open ? ' is-open' : ''}`}
      width="12"
      height="12"
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden="true"
    >
      <path
        d="m6 4 4 4-4 4"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function PlusIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M8 3.4v9.2M3.4 8h9.2" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  );
}

export function MoreIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
      <circle cx="3.6" cy="8" r="1.15" />
      <circle cx="8" cy="8" r="1.15" />
      <circle cx="12.4" cy="8" r="1.15" />
    </svg>
  );
}
