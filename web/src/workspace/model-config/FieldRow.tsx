/** 一行参数：标签 + 控件 + 来源徽标（点开看四层链路）+ 重置。
 *
 * 三个刻意的细节：
 * 1. 徽标是按钮而不是装饰：用户第一次看到「配置文件」时最想知道的是
 *    「哪份文件、我能不能改」，所以点开要给出每一层的原值和是否可写。
 * 2. 重置只在草稿里改过该字段时出现，避免 13 行都是灰按钮的噪声。
 * 3. 数字输入清空 = null（回落下层），与后端 `apply_patch` 的语义一致。
 *
 * `control` 是给「网关 / 模型」这两个组合控件的出口：它们要拉注册表与模型列表，
 * 数据在 Drawer 手里；把整段控件传进来，这一行就只负责排版与来源徽标。
 */

import type { ReactNode } from 'react';
import type { ModelBound, ModelLayer, ModelPatch, ModelSource } from '../../types';
import type { FieldSpec } from './groups';
import { NumInput } from './NumInput';
import { SourceBadge } from './Row';
import { EFFORTS } from './validation';

interface FieldRowProps {
  spec: FieldSpec;
  value: unknown;
  bound?: ModelBound;
  source?: ModelSource;
  /** 草稿里改过这一项：显示重置按钮与「待保存」底色。 */
  dirty: boolean;
  /** 该字段的四层原值；null 表示还没加载。 */
  layers: ModelLayer[] | null;
  chainOpen: boolean;
  /** 从左树点进来时高亮这一行，避免「点了没反应」。 */
  highlight?: boolean;
  /** 组合控件：给了就不画内置输入框（网关 / 模型两行用它）。 */
  control?: ReactNode;
  onToggleChain: () => void;
  onPatch: (patch: ModelPatch) => void;
  onReset: () => void;
}

/** 空值显示成「—」：它表示「跟随下层」，不是 0 也不是空字符串。 */
function show(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'boolean') return value ? '开' : '关';
  if (typeof value === 'number') return value.toLocaleString('en-US');
  return String(value);
}

function Control({ spec, value, bound, onPatch }: Pick<FieldRowProps, 'spec' | 'value' | 'bound' | 'onPatch'>) {
  // `provider` / `model` 正常情况下由 Drawer 传入组合控件；走到这里说明没传，
  // 兜底成文本框而不是数字框 —— 至少不会把模型名变成一个滑杆。
  if (spec.kind === 'text' || spec.kind === 'provider' || spec.kind === 'model') {
    return (
      <input
        className="mc-input mono"
        aria-label={spec.label}
        value={typeof value === 'string' ? value : ''}
        placeholder="未配置（走环境变量）"
        // 输入时保留原值：否则每敲一个空格就被 trim，光标会跳
        onChange={(event) => onPatch({ [spec.key]: event.target.value } as ModelPatch)}
        onBlur={(event) => {
          const text = event.target.value.trim();
          if (text !== (value ?? '')) onPatch({ [spec.key]: text === '' ? null : text } as ModelPatch);
        }}
      />
    );
  }
  if (spec.kind === 'auto') {
    // 两个状态各有后果，所以不用「开/关」而直接说结论
    return (
      <select
        className="mc-input"
        aria-label={spec.label}
        value={value === false ? 'false' : 'true'}
        onChange={(event) => onPatch({ [spec.key]: event.target.value === 'true' } as ModelPatch)}
      >
        <option value="true">跟随模型能力</option>
        <option value="false">用手填值</option>
      </select>
    );
  }
  if (spec.kind === 'effort' || spec.kind === 'tristate') {
    const current = value === null || value === undefined ? '' : String(value);
    return (
      <select
        className="mc-input"
        aria-label={spec.label}
        value={current}
        onChange={(event) => {
          const next = event.target.value;
          const parsed = next === '' ? null : spec.kind === 'tristate' ? next === 'true' : next;
          onPatch({ [spec.key]: parsed } as ModelPatch);
        }}
      >
        <option value="">默认</option>
        {spec.kind === 'tristate' ? (
          <>
            <option value="true">开</option>
            <option value="false">关</option>
          </>
        ) : (
          EFFORTS.map((item) => (
            <option key={item} value={item}>
              {item}
            </option>
          ))
        )}
      </select>
    );
  }
  if (spec.kind === 'boolean') {
    return (
      <select
        className="mc-input"
        aria-label={spec.label}
        value={value === false ? 'false' : 'true'}
        onChange={(event) => onPatch({ [spec.key]: event.target.value === 'true' } as ModelPatch)}
      >
        <option value="true">开</option>
        <option value="false">关</option>
      </select>
    );
  }
  return (
    <NumInput
      ariaLabel={spec.label}
      value={typeof value === 'number' ? value : null}
      bound={bound}
      integer={spec.kind === 'int'}
      onChange={(next) => onPatch({ [spec.key]: next } as ModelPatch)}
    />
  );
}

export function FieldRow({
  spec,
  value,
  bound,
  source,
  dirty,
  layers,
  chainOpen,
  highlight,
  control,
  onToggleChain,
  onPatch,
  onReset,
}: FieldRowProps) {
  return (
    <div
      className={`mcd-row${dirty ? ' is-dirty' : ''}${highlight ? ' is-highlight' : ''}`}
    >
      <div className="mcd-row-head">
        <span className="mcd-row-label" id={`mcd-label-${spec.key}`}>
          {spec.label}
        </span>
        <span className="mcd-row-control">
          {control ?? <Control spec={spec} value={value} bound={bound} onPatch={onPatch} />}
        </span>
        {source ? (
          <button
            type="button"
            className="mcd-badge-btn"
            aria-expanded={chainOpen}
            aria-label={`${spec.label} 的来源`}
            title="查看这个值来自哪一层"
            onClick={onToggleChain}
          >
            <SourceBadge source={source} pending={dirty} />
            <span className="mcd-caret" aria-hidden="true">
              ▾
            </span>
          </button>
        ) : (
          <span />
        )}
        <button
          type="button"
          className="mcd-reset"
          aria-label={`重置${spec.label}为继承`}
          title="撤销本会话/草稿里的改动，回落到下层"
          hidden={!dirty}
          onClick={onReset}
        >
          ↺
        </button>
      </div>
      {spec.hint && <span className="mcd-row-hint">{spec.hint}</span>}
      {chainOpen && (
        <ul className="mcd-chain" aria-label={`${spec.label} 的来源链`}>
          {layers === null ? (
            <li className="mcd-chain-empty">正在读取各层…</li>
          ) : (
            layers.map((layer) => (
              <li key={layer.id} className={layer.values[spec.key] == null ? 'is-empty' : ''}>
                <span className="mcd-chain-label">
                  {layer.label}
                  {layer.writable ? '' : ' · 只读'}
                </span>
                <span className="mcd-chain-value mono">{show(layer.values[spec.key])}</span>
              </li>
            ))
          )}
        </ul>
      )}
    </div>
  );
}
