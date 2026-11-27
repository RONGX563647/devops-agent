/** 网关下拉：先选网关，再选模型 —— 顺序错了会去错的 /models 里找模型。
 *
 * 三条刻意的取舍：
 * 1. **只显示 host**，不显示完整 URL：URL 上可能挂着 token 查询参数，而这一屏
 *    会被截屏、会被贴进工单。
 * 2. **缺 key 的网关仍可选中但标注**：选它是合法的（比如先配好地址，key 后补），
 *    只是选了之后能力卡查不出来 —— 说清楚比禁掉更好排查。
 * 3. **只有一个匿名网关时禁用下拉**：没有 providers.yaml 时不存在「换网关」这件事，
 *    给一个只有一个选项的下拉只会让人以为还有别的。
 */

import type { ProvidersPayload } from '../../types';

interface Props {
  providers: ProvidersPayload | null;
  /** 当前生效的网关 id。 */
  value: string;
  disabled?: boolean;
  onChange: (providerId: string) => void;
}

export function ProviderSelect({ providers, value, disabled, onChange }: Props) {
  if (providers === null) {
    return <span className="mc-readonly mono">正在读取网关…</span>;
  }
  const items = providers.items;
  const fixed = providers.anonymous || items.length <= 1;
  const current = items.find((item) => item.id === value) ?? items[0];

  return (
    <>
      <select
        className="mc-input"
        aria-label="网关"
        value={current?.id ?? ''}
        disabled={disabled || fixed}
        title={fixed ? '只有匿名网关：地址与 key 来自环境变量，换网关需要写 config/providers.yaml' : undefined}
        onChange={(event) => onChange(event.target.value)}
      >
        {items.map((item) => (
          <option key={item.id} value={item.id}>
            {item.label}
            {item.base_url_host ? ` · ${item.base_url_host}` : ''}
            {item.has_key ? '' : '（缺 key）'}
          </option>
        ))}
      </select>
      <span className="mcd-row-hint">
        {fixed
          ? '只有匿名网关（config/providers.yaml 为空），地址与 key 来自环境变量'
          : `${items.length} 个网关 · 缺 key 的选址仍可选中，但查不到能力卡`}
      </span>
    </>
  );
}
