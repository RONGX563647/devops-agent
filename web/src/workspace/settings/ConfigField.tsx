/**
 * 配置中心的一行（SPEC-0022 §9）：中文名 + 原键名 + 控件 + 文字来源徽标。
 *
 * 三个刻意的细节：
 * 1. **标签是中文名**，键名降为 12px mono 次要色。原来的 `latency_baseline_ms`
 *    直接当标签，等于让用户读数据库列名。
 * 2. **来源用文字而不是 4 色小方块**：颜色要记，文字不用。「代码默认 / 配置库 /
 *    环境变量 / yaml 基线」直接写在行上，点开还能看链路。
 * 3. **只读用无边框文本 + 徽标**，而不是灰掉的输入框 —— 灰框看起来像坏了。
 */
import type { ConfigBound, ConfigSource } from '../../types';
import { docFor } from './docs';

export const SOURCE_LABEL: Record<string, string> = {
  env: '环境变量',
  db: '配置库',
  yaml: 'yaml 基线',
  default: '代码默认',
};

export function boundHint(bound: ConfigBound | undefined): string {
  if (!bound) return '';
  const parts: string[] = [];
  if (bound.min !== undefined) parts.push(`≥ ${bound.min}`);
  if (bound.min_exclusive !== undefined) parts.push(`> ${bound.min_exclusive}`);
  if (bound.max !== undefined) parts.push(`≤ ${bound.max}`);
  if (bound.max_exclusive !== undefined) parts.push(`< ${bound.max_exclusive}`);
  if (bound.options?.length) parts.push(`可选：${bound.options.join(' / ')}`);
  return parts.join(' · ');
}

interface Props {
  path: string;
  /** 文案查表用的键：运行策略是 `域.路径`，模型页就是字段名。 */
  docKey?: string;
  value: unknown;
  source: ConfigSource;
  bound?: ConfigBound;
  locked: boolean;
  readonly: boolean;
  dirty: boolean;
  onChange: (path: string, next: unknown) => void;
  onReset: (path: string) => void;
}

/** 控件按值类型选：布尔用开关，枚举用下拉，数组用逗号分隔文本。 */
function controlOf(
  path: string,
  value: unknown,
  bound: ConfigBound | undefined,
  disabled: boolean,
  onChange: Props['onChange'],
) {
  const common = { 'aria-label': path, disabled };
  if (bound?.options?.length) {
    return (
      <select
        {...common}
        className="mc-input"
        value={String(value ?? '')}
        onChange={(event) => onChange(path, event.target.value)}
      >
        {bound.options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    );
  }
  if (typeof value === 'boolean') {
    return (
      <label className="cfg-switch">
        <input
          {...common}
          type="checkbox"
          checked={value}
          onChange={(event) => onChange(path, event.target.checked)}
        />
        <span>{value ? '开' : '关'}</span>
      </label>
    );
  }
  if (Array.isArray(value)) {
    return (
      <input
        {...common}
        className="mc-input mono"
        value={value.join(', ')}
        onChange={(event) =>
          onChange(
            path,
            event.target.value
              .split(',')
              .map((item) => item.trim())
              .filter(Boolean)
              .map((item) => (/^\d+$/.test(item) ? Number(item) : item)),
          )
        }
      />
    );
  }
  return (
    <input
      {...common}
      className="mc-input"
      type={typeof value === 'number' ? 'number' : 'text'}
      value={value === undefined || value === null ? '' : String(value)}
      onChange={(event) =>
        onChange(path, typeof value === 'number' ? Number(event.target.value) : event.target.value)
      }
    />
  );
}

export function ConfigField({
  path,
  docKey,
  value,
  source,
  bound,
  locked,
  readonly,
  dirty,
  onChange,
  onReset,
}: Props) {
  const doc = docFor(docKey ?? path);
  const hint = boundHint(bound);
  const disabled = locked || readonly;

  return (
    <div className={`cfg-row${dirty ? ' is-dirty' : ''}`}>
      <div className="cfg-row-head">
        <span className="cfg-row-label" title={doc.impact}>
          {doc.title}
          <code className="cfg-row-key">{path}</code>
        </span>
        <span className="cfg-row-control">
          {readonly ? (
            <span className="cfg-readonly-value mono">
              {value === undefined || value === null ? '—' : String(value)}
            </span>
          ) : (
            controlOf(path, value, bound, disabled, onChange)
          )}
        </span>
        <span className={`mc-src is-${source}`}>{SOURCE_LABEL[source] ?? source}</span>
        {readonly ? (
          <span className="cfg-readonly-badge">只读</span>
        ) : (
          <button
            type="button"
            className="cfg-reset"
            aria-label={`重置 ${path}`}
            title="恢复默认（删掉这层的覆盖）"
            disabled={locked || (source === 'default' && !dirty)}
            onClick={() => onReset(path)}
          >
            ⟲
          </button>
        )}
      </div>
      <span className="cfg-row-hint">
        {doc.what}
        {hint ? ` · 范围 ${hint}` : ''}
        {locked ? ' · 已被环境变量锁定' : ''}
      </span>
    </div>
  );
}
