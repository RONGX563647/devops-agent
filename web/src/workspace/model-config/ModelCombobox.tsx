/** 模型名输入：可手填，也可从网关的 /models 里挑。
 *
 * 为什么不是纯下拉：网关的 `/models` 经常不全（新模型还没进列表、聚合网关只列
 * 一部分），手填是**合法用法**，不是「高级选项」。所以这里是 input + datalist：
 * 打字仍然自由，列表只是提示。
 *
 * 为什么列表要懒加载而不是打开抽屉就取：打开抽屉只该读本地注册表；打上游是
 * 有代价的（可能 3s 超时、可能计费）。聚焦到这一行时才取，用户点了刷新才强制绕过缓存。
 */

import type { ProviderModel } from '../../types';
import { discoveryText, modelOptionLabel } from './capability';

interface Props {
  value: string;
  providerId: string;
  models: ProviderModel[];
  loading: boolean;
  /** 上次发现的状态码；`''` 表示还没取过。 */
  code: string;
  total: number;
  stale: boolean;
  disabled?: boolean;
  onChange: (model: string) => void;
  /** 首次聚焦：取一次缓存（新鲜的话后端不会打上游）。 */
  onLoad: () => void;
  /** 点刷新：强制绕过缓存。 */
  onRefresh: () => void;
}

export function ModelCombobox({
  value,
  providerId,
  models,
  loading,
  code,
  total,
  stale,
  disabled,
  onChange,
  onLoad,
  onRefresh,
}: Props) {
  const listId = `mc-models-${providerId || 'default'}`;
  return (
    <>
      <div className="mc-pick">
        <input
          className="mc-input mono"
          aria-label="模型名"
          list={listId}
          value={value}
          disabled={disabled}
          placeholder="可手填，例如 qwen3.8-max"
          onFocus={onLoad}
          onChange={(event) => onChange(event.target.value)}
          onBlur={(event) => onChange(event.target.value.trim())}
        />
        <button
          type="button"
          className="ghost-btn mc-pick-refresh"
          disabled={disabled || loading}
          title="绕过缓存，重新向网关要一次模型列表"
          onClick={onRefresh}
        >
          {loading ? '取…' : '刷新'}
        </button>
      </div>
      <datalist id={listId}>
        {models.map((item) => (
          <option key={item.id} value={item.id}>
            {modelOptionLabel(item)}
          </option>
        ))}
      </datalist>
      <span className="mcd-row-hint">
        {code === '' ? '聚焦这里会取一次模型列表；也可以直接手填' : discoveryText(code, total, stale)}
      </span>
    </>
  );
}
