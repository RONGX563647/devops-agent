/** 工作区页内联图标：统一 16 视窗、1.4 描边、currentColor，不引图标库。 */

type IconProps = { size?: number };

function base(size: number) {
  return {
    width: size,
    height: size,
    viewBox: '0 0 16 16',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.4,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    'aria-hidden': true,
  };
}

export function FolderIcon({ size = 16 }: IconProps) {
  return (
    <svg {...base(size)}>
      <path d="M2.2 4.6c0-.6.5-1.1 1.1-1.1h2.9l1.3 1.5h5.2c.6 0 1.1.5 1.1 1.1v5.4c0 .6-.5 1.1-1.1 1.1H3.3c-.6 0-1.1-.5-1.1-1.1V4.6Z" />
    </svg>
  );
}

export function GitIcon({ size = 16 }: IconProps) {
  return (
    <svg {...base(size)}>
      <circle cx="4.6" cy="3.6" r="1.6" />
      <circle cx="4.6" cy="12.4" r="1.6" />
      <circle cx="11.4" cy="6.4" r="1.6" />
      <path d="M4.6 5.2v5.6M11.4 8v1.2c0 1.2-1 2.2-2.2 2.2H6.2" />
    </svg>
  );
}

export function PlusIcon({ size = 16 }: IconProps) {
  return (
    <svg {...base(size)}>
      <path d="M8 3.4v9.2M3.4 8h9.2" strokeWidth={1.7} />
    </svg>
  );
}

export function SearchIcon({ size = 15 }: IconProps) {
  return (
    <svg {...base(size)}>
      <circle cx="7" cy="7" r="4" />
      <path d="m10.2 10.2 3 3" />
    </svg>
  );
}

export function UpIcon({ size = 15 }: IconProps) {
  return (
    <svg {...base(size)}>
      <path d="M8 12.5v-9M4.2 7.2 8 3.4l3.8 3.8" />
    </svg>
  );
}

export function ChevronRightIcon({ size = 14 }: IconProps) {
  return (
    <svg {...base(size)}>
      <path d="m6 3.8 4.2 4.2L6 12.2" />
    </svg>
  );
}

export function CopyIcon({ size = 14 }: IconProps) {
  return (
    <svg {...base(size)}>
      <rect x="5.8" y="5.8" width="7" height="7" rx="1.4" />
      <path d="M10.2 3.6H4.7c-.6 0-1.1.5-1.1 1.1v5.5" />
    </svg>
  );
}

export function CheckIcon({ size = 14 }: IconProps) {
  return (
    <svg {...base(size)}>
      <path d="m3.6 8.4 2.7 2.7 6-6.4" />
    </svg>
  );
}

export function ArchiveIcon({ size = 15 }: IconProps) {
  return (
    <svg {...base(size)}>
      <rect x="2.4" y="3.2" width="11.2" height="3.2" rx="1" />
      <path d="M3.4 6.4v5.2c0 .7.5 1.2 1.2 1.2h6.8c.7 0 1.2-.5 1.2-1.2V6.4M6.4 9.2h3.2" />
    </svg>
  );
}

export function RestoreIcon({ size = 15 }: IconProps) {
  return (
    <svg {...base(size)}>
      <path d="M3.2 7.4A4.9 4.9 0 1 1 4.6 11" />
      <path d="M3 3.9v3.7h3.7" />
    </svg>
  );
}

export function TrashIcon({ size = 15 }: IconProps) {
  return (
    <svg {...base(size)}>
      <path d="M2.8 4.6h10.4M6.4 4.6V3.2h3.2v1.4M4.2 4.6l.6 7.6c.1.7.6 1.2 1.3 1.2h3.8c.7 0 1.2-.5 1.3-1.2l.6-7.6" />
    </svg>
  );
}

export function WorkspaceMark({ size = 15 }: IconProps) {
  return (
    <svg {...base(size)}>
      <path d="M3.2 5.5 8 2.8l4.8 2.7v5L8 13.2l-4.8-2.7v-5Z" />
      <path d="M8 8v5.2M8 8 3.4 5.6M8 8l4.6-2.4" opacity={0.7} />
    </svg>
  );
}
