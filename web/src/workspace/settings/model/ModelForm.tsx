/**
 * 模型参数表单（SPEC-0022 §7.1 中栏）：五组参数，一组一段。
 *
 * 与抽屉版的差别只有一个：**不再把说明塞在行尾**。行内只留一句 hint，
 * 长解释交给右栏（`DocPanel`）；行尾只保留来源徽标与重置。
 */
import type { ModelBound, ModelPatch, ModelSource, ModelValues, ProviderModel, ProvidersPayload } from '../../../types';
import { FieldRow } from './FieldRow';
import { GROUPS, type FieldSpec } from './groups';
import { ModelCombobox } from './ModelCombobox';
import type { ModelsState } from './useCatalog';

interface Props {
  shown: ModelValues;
  draft: ModelPatch;
  sources: Record<string, ModelSource>;
  bounds: Record<string, ModelBound>;
  activeField: string | null;
  providers: ProvidersPayload | null;
  providerId: string;
  models: ProviderModel[];
  catalogState: ModelsState;
  readonlyEntries: Record<string, string>;
  onPatch: (patch: ModelPatch) => void;
  onReset: (key: string) => void;
  onFocusField: (key: string) => void;
  onLoadModels: () => void;
  onRefreshModels: () => void;
  onPickProvider: (id: string) => void;
}

function ProviderPicker({
  providers,
  value,
  onChange,
}: {
  providers: ProvidersPayload | null;
  value: string;
  onChange: (id: string) => void;
}) {
  if (providers === null) return <span className="mc-readonly mono">正在读取网关…</span>;
  const items = providers.items;
  const fixed = providers.anonymous || items.length <= 1;
  return (
    <>
      <select
        className="mc-input"
        aria-label="网关"
        value={value || items[0]?.id || ''}
        disabled={fixed}
        onChange={(event) => onChange(event.target.value)}
      >
        {items.map((item) => (
          <option key={item.id} value={item.id}>
            {item.label}
            {item.has_key ? '' : '（缺 key）'}
          </option>
        ))}
      </select>
      <span className="mcd-row-hint">
        {fixed
          ? '只有匿名网关：地址与 key 来自环境变量；去「网关」页可新增'
          : `${items.length} 个网关 · 缺 key 的也能选中，但查不到能力卡`}
      </span>
    </>
  );
}

export function ModelForm(props: Props) {
  const {
    shown,
    draft,
    sources,
    bounds,
    activeField,
    providers,
    providerId,
    models,
    catalogState,
    readonlyEntries,
    onPatch,
    onReset,
    onFocusField,
    onLoadModels,
    onRefreshModels,
    onPickProvider,
  } = props;

  function controlFor(spec: FieldSpec) {
    if (spec.key === 'provider') {
      return <ProviderPicker providers={providers} value={providerId} onChange={onPickProvider} />;
    }
    if (spec.key === 'model') {
      return (
        <ModelCombobox
          value={typeof shown.model === 'string' ? shown.model : ''}
          providerId={providerId}
          models={models}
          loading={catalogState.loading}
          code={catalogState.code}
          total={catalogState.total}
          stale={catalogState.stale}
          onChange={(next) => onPatch({ model: next })}
          onLoad={onLoadModels}
          onRefresh={onRefreshModels}
        />
      );
    }
    return undefined;
  }

  return (
    <div className="mp-form">
      {GROUPS.map((group) => (
        <section className="mp-section" id={`mp-group-${group.id}`} key={group.id}>
          <header className="mp-section-head">
            <h3>{group.label}</h3>
            <p>{group.summary}</p>
          </header>
          {group.items.map((spec) => (
            <div id={`mp-field-${spec.key}`} key={spec.key}>
              <FieldRow
                spec={spec}
                value={shown[spec.key]}
                bound={bounds[spec.key]}
                source={draft[spec.key] !== undefined ? 'session' : sources[spec.key]}
                dirty={draft[spec.key] !== undefined}
                layers={null}
                chainOpen={false}
                highlight={activeField === spec.key}
                control={controlFor(spec)}
                onToggleChain={() => onFocusField(spec.key)}
                onPatch={onPatch}
                onReset={() => onReset(spec.key)}
              />
            </div>
          ))}
        </section>
      ))}

      <section className="mp-section">
        <header className="mp-section-head">
          <h3>只读</h3>
          <p>这些值不在这里改，改错会让服务连不上模型。</p>
        </header>
        {Object.entries(readonlyEntries).map(([key, note]) => (
          <div className="mp-readonly" key={key}>
            <code className="mono">{key}</code>
            <span>{note}</span>
          </div>
        ))}
      </section>
    </div>
  );
}
