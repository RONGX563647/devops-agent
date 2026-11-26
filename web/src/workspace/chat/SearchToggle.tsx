/** 联网开关：能力探测与禁用态都在这里，ChatTab 只拿「开/关」一个布尔。
 *
 * 为什么单独成型：`/api/tools/search` 的可用性决定按钮能不能点，这段异步状态
 * 与 title 文案放在容器里会把 ChatTab 撑过 500 行，也和「发送/滚动/附件」无关。
 */
import { useQuery } from '@tanstack/react-query';
import { fetchSearchToolStatus } from '../../api/client';
import { GlobeIcon } from './icons';

interface Props {
  on: boolean;
  /** 本轮正在跑：开关只能在两轮之间切，跑到一半改会让前后半段不一致。 */
  running: boolean;
  onToggle: () => void;
}

export function SearchToggle({ on, running, onToggle }: Props) {
  // 检索后端是部署期能力，中途不会变：staleTime=Infinity 避免每次挂载都闪一次禁用态。
  const query = useQuery({
    queryKey: ['tools-search'],
    queryFn: fetchSearchToolStatus,
    staleTime: Infinity,
    retry: 1,
  });
  const ready = query.data?.configured ?? false;
  const title = ready
    ? '联网检索：允许本轮调用 web_search，由模型判断是否需要检索'
    : `联网不可用：${query.data?.reason ?? '正在探测检索后端'}`;

  return (
    <button
      type="button"
      className={`search-toggle${on ? ' is-on' : ''}`}
      aria-pressed={on}
      aria-label="联网搜索"
      title={title}
      onClick={onToggle}
      disabled={running || !ready}
    >
      <GlobeIcon />
      <span>联网</span>
    </button>
  );
}
