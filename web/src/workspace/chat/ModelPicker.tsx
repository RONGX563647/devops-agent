/**
 * 会话内模型选择器（SPEC-0022 §5）：一个 chip，一份跨网关的模型列表。
 *
 * 为什么不是两级下拉：用户想的是「换个模型」，不是「换个网关再换模型」。
 * 网关是实现细节，只在读不到时以错误文案出现（「2 个网关的模型没取到」）。
 *
 * 三个刻意的细节：
 * 1. 用原生 `[popover]`（manual）而不是 `<dialog>`：轻层、不抢焦点、Esc 与外点
 *    由浏览器负责；不支持时退化成普通绝对定位面板（见 `usePopover`）。
 * 2. 选中即提交：一次 `PATCH` 同时带 `provider` 与 `model`，作用域固定 session ——
 *    「选模型」不该顺手改全局默认。
 * 3. 服务端可能拒绝（新网关没有这个模型名），错误里带 `available`，这里直接把
 *    候选列出来让他改，而不是只弹一句「保存失败」。
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { patchSessionModelConfig, fetchModels } from '../../api/client';
import { ApiError } from '../../api/http';
import type { AggregatedModel, ModelConfig, ModelsAggregate } from '../../types';
import { formatTokens } from '../ContextRing';

interface Props {
  session: string;
  config: ModelConfig | undefined;
  onChanged: () => void;
}

/** React 18 的类型里还没有 `popover` 属性；运行时是原生行为。 */
const POPOVER_ATTR = { popover: 'manual' } as unknown as Record<string, string>;

/**
 * 支持 `showPopover` 才敢挂 `popover` 属性。
 *
 * 不支持的浏览器（以及 jsdom）对 `[popover]` 的 UA 样式是
 * `display:none`，而 `:popover-open` 永远匹配不上 —— 属性挂着面板就永远是隐形的，
 * 我们的 `data-open` 兜底也救不回来。所以能力检测在**渲染前**做。
 */
const POPOVER_SUPPORTED =
  typeof HTMLElement !== 'undefined' &&
  typeof (HTMLElement.prototype as { showPopover?: unknown }).showPopover === 'function';

interface RejectInfo {
  message: string;
  available: string[];
}

function windowLabel(model: AggregatedModel): string {
  return model.context ? formatTokens(model.context) : '窗口未知';
}

/** 打开时把 popover 提到 top layer；不支持就退回普通面板。 */
function usePopover(open: boolean, onClose: () => void) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const supported = typeof el.showPopover === 'function';
    if (open) {
      // 定位用触发器的矩形，不做 anchor 猜测：面板宽度固定 360，靠右时回退
      const rect = el.parentElement?.getBoundingClientRect();
      if (rect) {
        const left = Math.min(Math.max(8, rect.left), window.innerWidth - 368);
        setPos({ left, top: Math.max(8, window.innerHeight - rect.top + 8) });
      }
      if (supported) el.showPopover();
      else el.setAttribute('data-open', '');
    } else {
      if (supported && el.matches(':popover-open')) el.hidePopover();
      el.removeAttribute('data-open');
    }
    return undefined;
  }, [open]);

  useEffect(() => {
    if (!open) return undefined;
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  return { ref, pos };
}

export function ModelPicker({ session, config, onChanged }: Props) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<ModelsAggregate | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [manual, setManual] = useState(false);
  const [manualName, setManualName] = useState('');
  const [reject, setReject] = useState<RejectInfo | null>(null);
  const chipRef = useRef<HTMLButtonElement | null>(null);
  const { ref, pos } = usePopover(open, () => setOpen(false));

  const current = config?.effective;
  const providerId = current?.provider || config?.catalog?.provider || '';
  const providerLabel =
    data?.providers.find((item) => item.id === providerId)?.label || providerId || '默认网关';

  async function load(force: boolean) {
    setLoading(true);
    setError('');
    try {
      setData(await fetchModels({ refresh: force }));
    } catch {
      // 503 是「全部读不到」，与「没有模型」必须分开说
      setError('读不到模型列表：所有网关都没有响应。检查 key 与地址后再试。');
      setData(null);
    } finally {
      setLoading(false);
    }
  }

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next && !data) void load(false);
  }

  async function commit(provider: string, model: string) {
    setReject(null);
    try {
      await patchSessionModelConfig(session, { provider, model });
      onChanged();
      setOpen(false);
      chipRef.current?.focus();
    } catch (err) {
      const detail = err instanceof ApiError ? (err.detail as Record<string, unknown> | null) : null;
      if (detail && detail.code === 'model_not_in_provider') {
        setReject({
          message: `「${String(detail.model)}」不在这个网关上，换一个：`,
          available: (detail.available as string[] | undefined) ?? [],
        });
        return;
      }
      setReject({ message: '切换失败：服务端拒绝了这次修改', available: [] });
    }
  }

  const groups = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const items = (data?.items ?? []).filter(
      (item) => !needle || item.model.toLowerCase().includes(needle),
    );
    const map = new Map<string, AggregatedModel[]>();
    for (const item of items) {
      const key = item.provider_label || item.provider;
      map.set(key, [...(map.get(key) ?? []), item]);
    }
    return [...map.entries()];
  }, [data, query]);

  const failed = (data?.providers ?? []).filter((item) => item.code !== 'ok');

  return (
    <div className="mp-wrap">
      <button
        ref={chipRef}
        type="button"
        className={`mp-chip${open ? ' is-open' : ''}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label="选择模型"
        title="切换本次会话使用的模型"
        onClick={toggle}
      >
        <span className="mp-chip-label">模型</span>
        <span className="mp-chip-name">{current?.model || '未选择'}</span>
        {current?.context_size ? (
          <span className="mp-chip-meta">{formatTokens(current.context_size)}</span>
        ) : null}
        <span className="mp-chip-caret" aria-hidden="true">
          ▾
        </span>
      </button>

      {open && (
        <div
          ref={ref}
          className="mp-panel"
          role="dialog"
          aria-label="选择模型"
          {...(POPOVER_SUPPORTED ? POPOVER_ATTR : {})}
          style={pos ? { left: pos.left, bottom: pos.top } : undefined}
        >
          <div className="mp-head">
            <input
              autoFocus
              className="mp-search"
              aria-label="搜索模型"
              placeholder="搜索模型…"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            <button
              type="button"
              className="ghost-btn"
              disabled={loading}
              onClick={() => void load(true)}
            >
              {loading ? '刷新中…' : '刷新'}
            </button>
          </div>

          {error && <p className="mp-error">{error}</p>}
          {failed.length > 0 && (
            <p className="mp-warn">
              {failed.length} 个网关的模型没取到：
              {failed.map((item) => `${item.label} ${item.code}`).join('、')}
            </p>
          )}
          {reject && (
            <div className="mp-error" role="alert">
              <p>{reject.message}</p>
              {reject.available.length > 0 && (
                <p className="mp-available">
                  {reject.available.map((name) => (
                    <button
                      key={name}
                      type="button"
                      className="mp-available-item"
                      onClick={() => void commit(providerId, name)}
                    >
                      {name}
                    </button>
                  ))}
                </p>
              )}
            </div>
          )}

          <div className="mp-list" role="listbox" aria-label="可用模型">
            {loading && !data && <p className="mp-empty">正在向各网关取模型…</p>}
            {!loading && groups.length === 0 && !error && (
              <p className="mp-empty">没有匹配的模型。可以手动输入名字。</p>
            )}
            {groups.map(([label, items]) => (
              <section key={label} className="mp-group">
                <h3 className="mp-group-head">
                  <span>{label}</span>
                  <span className="mp-group-meta">{items.length} 个可用</span>
                </h3>
                {items.map((item) => {
                  const selected = item.provider === providerId && item.model === current?.model;
                  return (
                    <button
                      key={`${item.provider}:${item.model}`}
                      type="button"
                      role="option"
                      aria-selected={selected}
                      className={`mp-item${selected ? ' is-selected' : ''}`}
                      onClick={() => void commit(item.provider, item.model)}
                    >
                      <span className="mp-item-name">{item.model}</span>
                      <span className="mp-item-meta">{windowLabel(item)}</span>
                      {item.status === 'deprecated' && (
                        <span className="mp-item-badge">已弃用</span>
                      )}
                    </button>
                  );
                })}
              </section>
            ))}
          </div>

          <div className="mp-pick-foot">
            {manual ? (
              <div className="mp-manual">
                <input
                  className="mp-search mono"
                  aria-label="手动输入模型名"
                  placeholder="模型名，例如 qwen3-max"
                  value={manualName}
                  onChange={(event) => setManualName(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' && manualName.trim()) {
                      void commit(providerId, manualName.trim());
                    }
                  }}
                />
                <button
                  type="button"
                  className="ghost-btn"
                  disabled={!manualName.trim()}
                  onClick={() => void commit(providerId, manualName.trim())}
                >
                  使用
                </button>
              </div>
            ) : (
              <button type="button" className="mp-link" onClick={() => setManual(true)}>
                手动输入模型名…
              </button>
            )}
            <Link
              className="mp-link"
              to="/?tab=settings&section=model"
              onClick={() => setOpen(false)}
            >
              模型参数 ›
            </Link>
          </div>
        </div>
      )}
      <span className="mp-note" title={`当前网关：${providerLabel}`}>
        {providerLabel}
      </span>
    </div>
  );
}
