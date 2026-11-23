import type { Run } from '../types';

export function StatusBadge({ status }: { status: Run['status'] }) {
  const label = { success: '成功', failed: '失败', running: '进行中' }[status];
  return <span className={`badge badge-${status}`}>{label}</span>;
}
