/**
 * 设置左树（SPEC-0022 §4.2）：两级，段标题不可点，叶子可点。
 *
 * 为什么分「模型与网关」和「运行策略」两段：前者决定「用什么模型」，
 * 后者决定「怎么跑」；混在一列里时，用户会把 `max_iters` 当成模型参数，
 * 而它其实是会话执行策略。
 */
import type { ConfigDomainSummary } from '../../types';

export interface SectionLeaf {
  key: string;
  title: string;
  meta: string;
  overrides?: number;
}

interface Props {
  domains: ConfigDomainSummary[];
  section: string;
  onSelect: (section: string) => void;
}

const MODEL_LEAVES: SectionLeaf[] = [
  { key: 'model', title: '模型参数', meta: '窗口 / 采样 / 重试' },
  { key: 'providers', title: '网关', meta: 'providers.yaml' },
];

function domainLeaf(item: ConfigDomainSummary): SectionLeaf {
  const meta = item.writable
    ? item.hot === 'immediate'
      ? '改完即生效'
      : item.hot === 'next_turn'
        ? '下一轮生效'
        : '需重启'
    : item.managed_by
      ? '入口在别处'
      : '只读';
  return { key: item.domain, title: item.title, meta, overrides: item.overrides };
}

export function SectionTree({ domains, section, onSelect }: Props) {
  const policy = domains.filter((item) => item.domain === 'policy');
  const rest = domains.filter((item) => item.domain !== 'policy' && item.domain !== 'model');

  const renderLeaf = (leaf: SectionLeaf) => (
    <li key={leaf.key}>
      <button
        type="button"
        className={`cfg-tree-item${leaf.key === section ? ' is-active' : ''}`}
        aria-current={leaf.key === section ? 'page' : undefined}
        onClick={() => onSelect(leaf.key)}
      >
        <span className="cfg-tree-title">{leaf.title}</span>
        <span className="cfg-tree-meta">{leaf.meta}</span>
        {leaf.overrides ? <span className="cfg-tree-count">覆盖 {leaf.overrides}</span> : null}
      </button>
    </li>
  );

  return (
    <nav className="cfg-tree" aria-label="设置分区">
      <p className="cfg-tree-group">模型与网关</p>
      <ul className="cfg-tree-list">{MODEL_LEAVES.map(renderLeaf)}</ul>

      <p className="cfg-tree-group">运行策略</p>
      <ul className="cfg-tree-list">{rest.map((item) => renderLeaf(domainLeaf(item)))}</ul>

      <p className="cfg-tree-group">安全边界</p>
      <ul className="cfg-tree-list">{policy.map((item) => renderLeaf(domainLeaf(item)))}</ul>
    </nav>
  );
}
