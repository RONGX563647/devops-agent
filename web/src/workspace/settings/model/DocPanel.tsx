/**
 * 模型参数页的右栏（SPEC-0022 §7.1）：常驻说明 + 实时影响 + 四层来源链。
 *
 * 「影响」必须说具体数字，否则就退化成「调大更贵」这种正确的废话。数字全部
 * 来自后端（`derived` / `catalog`），前端只做单位与句式包装 —— 口径只能有一处。
 */
import type { CatalogBlock, ModelDerived, ModelLayer, ModelValues } from '../../../types';
import { formatTokens } from '../../ContextRing';
import type { ParamDoc } from '../docs';

interface Props {
  field: string;
  doc: ParamDoc;
  layers: ModelLayer[] | null;
  values: Partial<ModelValues>;
  derived?: ModelDerived;
  catalog?: CatalogBlock;
  error?: string;
  /** 该字段在草稿里被改过：说明右栏数字是预览值。 */
  live: boolean;
}

const RISK_LABEL = { low: '低风险', mid: '中等风险', high: '高风险' } as const;

/** 哪些字段会改变「最坏请求数」：与后端 `worst_case_attempts` 的口径一致。 */
const ATTEMPT_FIELDS = new Set(['max_iters', 'model_max_retries', 'agent_max_retries']);
const THRESHOLD_FIELDS = new Set(['trigger_ratio', 'reserve_ratio', 'context_size']);

function impactLine(
  field: string,
  derived: ModelDerived | undefined,
  values: Partial<ModelValues>,
): string | null {
  if (!derived) return null;
  if (ATTEMPT_FIELDS.has(field)) {
    return `最坏情况下一次对话发 ${derived.worst_case_attempts} 次模型请求（含重试）。`;
  }
  if (THRESHOLD_FIELDS.has(field)) {
    return `按当前窗口，约占 ${formatTokens(derived.compression_threshold)} token 时开始压缩。`;
  }
  if (field === 'context_auto' || field === 'context_size') {
    const window = values.context_size ?? 0;
    return derived.context_uncalibrated
      ? `${formatTokens(window)} 是框架默认值，没有经过确认，建议按模型能力核对。`
      : `当前窗口 ${formatTokens(window)}，来源可点下方链路核对。`;
  }
  return null;
}

function chainOf(field: string, layers: ModelLayer[] | null) {
  if (!layers) return null;
  return layers.map((layer) => ({
    id: layer.id,
    label: layer.label,
    writable: layer.writable,
    value: layer.values[field as keyof ModelValues],
  }));
}

export function DocPanel({ field, doc, layers, values, derived, catalog, error, live }: Props) {
  const chain = chainOf(field, layers);
  const impact = impactLine(field, derived, values);

  return (
    <aside className="mp-doc" aria-label="参数说明">
      <header className="mp-doc-head">
        <h3 className="mp-doc-title">{doc.title}</h3>
        <code className="mp-doc-key">{field}</code>
        {doc.risk && (
          <span className={`mp-risk is-${doc.risk}`}>{RISK_LABEL[doc.risk]}</span>
        )}
      </header>

      <section className="mp-doc-section">
        <h4>是什么</h4>
        <p>{doc.what}</p>
      </section>

      <section className="mp-doc-section">
        <h4>改了会怎样</h4>
        <p>{doc.impact}</p>
        {impact && (
          <p className={`mp-doc-impact${live ? ' is-live' : ''}`}>
            {impact}
            {live && <span className="mp-doc-live-tag">预览值</span>}
          </p>
        )}
      </section>

      {error && (
        <section className="mp-doc-section">
          <h4>这一项不合法</h4>
          <p className="mp-doc-error">{error}</p>
        </section>
      )}

      {field === 'model' && catalog && (
        <section className="mp-doc-section">
          <h4>能力卡</h4>
          <p>
            {catalog.current.label || '未匹配到'} · 窗口{' '}
            {formatTokens(values.context_size ?? 0)} · 状态 {catalog.current.status}
          </p>
          <p className="mp-doc-note">
            匹配方式 {catalog.current.matched_by} · 窗口来源{' '}
            {catalog.current.context_source || '无'}
          </p>
        </section>
      )}

      <section className="mp-doc-section">
        <h4>谁在压着它</h4>
        {chain === null ? (
          <p className="mp-doc-note">正在读取各层…</p>
        ) : (
          <ol className="mp-chain">
            {chain.map((layer) => (
              <li key={layer.id} className={layer.value == null ? 'is-empty' : ''}>
                <span className="mp-chain-label">
                  {layer.label}
                  {layer.writable ? '' : ' · 只读'}
                </span>
                <span className="mp-chain-value mono">
                  {layer.value == null || layer.value === ''
                    ? '—'
                    : String(layer.value)}
                </span>
              </li>
            ))}
          </ol>
        )}
      </section>

      {doc.unit && <p className="mp-doc-unit">单位：{doc.unit}</p>}
    </aside>
  );
}
