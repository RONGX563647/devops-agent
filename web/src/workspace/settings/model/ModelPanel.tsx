/**
 * 模型参数页（SPEC-0022 §7）：三栏 + 保存范围 + 预设。
 *
 * 与旧抽屉的四处变化：
 * 1. 从 `<dialog>` 搬进设置页：可以深链（`?tab=settings&section=model&field=top_p`）、
 *    可以边看右栏说明边改，不再遮住对话。
 * 2. 保存范围做成显式分段控件。「本次会话 / 全局默认」原来藏在两个按钮的主次里，
 *    实测 30 个会话只有 2 条会话覆盖 —— 说明主次被读反了。
 * 3. 右栏常驻说明 + 实时影响：数字来自 `POST /api/model-config/preview`（300ms 防抖）。
 * 4. 预设把参数组合翻译成意图，只填草稿、仍需手动保存。
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  clearSessionModelConfig,
  fetchModelConfig,
  fetchModelLayers,
  fetchProviders,
  patchModelConfig,
  patchSessionModelConfig,
  previewModelConfig,
} from '../../../api/client';
import type {
  ModelPatch,
  ModelPreview,
  ModelSource,
  ModelValues,
  SessionModelConfig,
} from '../../../types';
import { docFor } from '../docs';
import { PRESETS } from '../Tour';
import { DocPanel } from './DocPanel';
import { ModelForm } from './ModelForm';
import { GROUPS, labelOf } from './groups';
import { useCatalog } from './useCatalog';
import { boundError, dangersOf, errorText } from './validation';

type Scope = 'global' | 'session';

interface Props {
  session: string;
  initialField?: string | null;
  onDirtyChange: (dirty: boolean) => void;
}

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

export function ModelPanel({ session, initialField, onDirtyChange }: Props) {
  const queryClient = useQueryClient();
  const [scope, setScope] = useState<Scope>('global');
  const [draft, setDraft] = useState<ModelPatch>({});
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<ModelPreview | null>(null);
  // 危险改动（换模型 / 改窗口 / 放大重试）要人先看清后果再落盘。抽屉版有这个闸，
  // 迁移时不能顺手丢掉：它挡的不是误触，而是「不知道自己在改什么」。
  const [ack, setAck] = useState(false);
  const [activeField, setActiveField] = useState(initialField || 'context_size');
  const catalog = useCatalog();
  const pendingField = useRef<string | null>(initialField ?? null);

  const config = useQuery({
    queryKey: ['model-config', scope, scope === 'session' ? session : ''],
    queryFn: () => fetchModelConfig(scope === 'session' ? session : undefined),
  });
  const providers = useQuery({ queryKey: ['providers'], queryFn: fetchProviders });
  const layers = useQuery({
    queryKey: ['model-layers', scope, scope === 'session' ? session : ''],
    queryFn: () => fetchModelLayers(scope === 'session' ? session : undefined),
    enabled: Boolean(config.data),
  });

  const base = config.data?.effective;
  const shown = useMemo(() => {
    if (!base) return null;
    const next = { ...base } as Record<string, unknown>;
    for (const [key, value] of Object.entries(draft)) {
      if (value !== null) next[key] = value;
    }
    return next as unknown as ModelValues;
  }, [base, draft]);

  const dirtyKeys = useMemo(() => {
    if (!base) return [] as string[];
    const overrides = (config.data as SessionModelConfig | undefined)?.overrides ?? {};
    return Object.keys(draft).filter((key) => {
      const value = draft[key as keyof ModelPatch];
      if (value === null) return overrides[key as keyof ModelValues] !== undefined;
      return !same(value, base[key as keyof ModelValues]);
    });
  }, [draft, base, config.data]);

  useEffect(() => onDirtyChange(dirtyKeys.length > 0), [dirtyKeys.length, onDirtyChange]);

  // 预览：草稿变了才发，300ms 防抖；失败静默（右栏退回已保存的口径）。
  // 清空草稿时不在这里 setState —— 用下面的 `shownPreview` 派生，否则一次
  // 草稿清空会多一轮渲染（React 也不允许在 effect 里同步 setState）。
  useEffect(() => {
    if (!config.data || dirtyKeys.length === 0) return undefined;
    const sessionArg = scope === 'session' ? session : undefined;
    const timer = window.setTimeout(() => {
      previewModelConfig(draft, sessionArg)
        .then(setPreview)
        .catch(() => setPreview(null));
    }, 300);
    return () => window.clearTimeout(timer);
  }, [draft, dirtyKeys.length, scope, session, config.data]);

  // 深链定位：`field` 参数只消费一次，之后滚动跟随用户点击
  useEffect(() => {
    const key = pendingField.current;
    if (!key || !config.data) return;
    pendingField.current = null;
    const node = document.getElementById(`mp-field-${key}`);
    node?.scrollIntoView?.({ block: 'center' });
    setActiveField(key);
  }, [config.data]);

  const providerId =
    (draft.provider as string | undefined) ||
    config.data?.catalog?.provider ||
    base?.provider ||
    providers.data?.default ||
    '';

  function patch(change: ModelPatch) {
    setDraft((prev) => ({ ...prev, ...change }));
    // 草稿又变了：上一次的「我了解了」不再覆盖新的改动
    setAck(false);
    setNotice('');
  }

  function resetField(key: string) {
    setAck(false);
    const overrides = (config.data as SessionModelConfig | undefined)?.overrides ?? {};
    if (scope === 'session' && overrides[key as keyof ModelValues] !== undefined) {
      // 会话覆盖：发 null 才是「删掉这条覆盖」，不是写回继承值
      setDraft((prev) => ({ ...prev, [key]: null }));
      return;
    }
    setDraft((prev) => {
      const next = { ...prev };
      delete next[key as keyof ModelPatch];
      return next;
    });
  }

  async function save() {
    if (!config.data) return;
    if (blocked) {
      // 按钮已经是 disabled，这里只兜住键盘回车/程序化点击
      if (localError) setNotice(`先修正取值：${localError}`);
      return;
    }
    setBusy(true);
    try {
      if (scope === 'session') {
        if (!session) {
          setNotice('还没有会话：先发一条消息，再改本次会话的参数');
          return;
        }
        const result = await patchSessionModelConfig(session, draft);
        setNotice(`已保存到本次会话 · ${result.effective_from ?? '下一轮生效'}`);
      } else {
        const result = await patchModelConfig(draft, config.data.version);
        const affected = result.affected ?? { applied: 0, deferred: 0 };
        setNotice(
          `已写入全局默认 · ${affected.applied} 个会话立即生效，${affected.deferred} 个等当前轮结束`,
        );
      }
      setDraft({});
      setPreview(null);
      setAck(false);
      await queryClient.invalidateQueries({ queryKey: ['model-config'] });
      await queryClient.invalidateQueries({ queryKey: ['model-layers'] });
    } catch (error) {
      setNotice(errorText(error));
    } finally {
      setBusy(false);
    }
  }

  async function clearSession() {
    setBusy(true);
    try {
      await clearSessionModelConfig(session);
      setDraft({});
      setNotice('已清空本次会话的覆盖，回到全局默认');
      await queryClient.invalidateQueries({ queryKey: ['model-config'] });
    } catch (error) {
      setNotice(errorText(error));
    } finally {
      setBusy(false);
    }
  }

  function switchScope(next: Scope) {
    if (next === scope) return;
    if (dirtyKeys.length > 0 && !window.confirm('切换保存范围会丢弃当前草稿，继续？')) return;
    setDraft({});
    setPreview(null);
    setNotice('');
    setAck(false);
    setScope(next);
  }

  const sources = (config.data?.sources ?? {}) as Record<string, ModelSource>;
  // 没有草稿就没有「预览值」：上一次的预览结果留着也不能用
  const shownPreview = dirtyKeys.length > 0 ? preview : null;
  const fieldError = shownPreview?.errors.find((item) => item.field === activeField)?.reason;
  const activeDoc = docFor(activeField, labelOf(activeField));
  const dangers = base ? dangersOf(draft, base) : [];
  const localError = boundError(draft, config.data?.bounds ?? {});
  // 三种情况不许点保存：本地越界、危险项未确认、正在写
  const blocked = Boolean(localError) || (dangers.length > 0 && !ack) || busy;
  const changedByGroup = new Map<string, number>();
  for (const key of dirtyKeys) {
    const group = GROUPS.find((item) => item.items.some((spec) => spec.key === key));
    if (group) changedByGroup.set(group.id, (changedByGroup.get(group.id) ?? 0) + 1);
  }

  return (
    <section className="mp">
      <header className="mp-top">
        <div className="mp-title-block">
          <h2 className="mp-title">模型参数</h2>
          <p className="mp-sub">
            这一页决定「用哪个模型、一次能跑多远」。值落盘在 config/model.local.yaml。
          </p>
        </div>
        <div className="mp-scope" role="group" aria-label="保存范围">
          <button
            type="button"
            className={`mp-scope-btn${scope === 'session' ? ' is-active' : ''}`}
            aria-pressed={scope === 'session'}
            disabled={!session}
            onClick={() => switchScope('session')}
          >
            本次会话
          </button>
          <button
            type="button"
            className={`mp-scope-btn${scope === 'global' ? ' is-active' : ''}`}
            aria-pressed={scope === 'global'}
            onClick={() => switchScope('global')}
          >
            全局默认
          </button>
        </div>
      </header>

      {scope === 'session' && (
        <p className="mp-banner">
          仅当前会话生效，换会话即回到全局默认。
          {session ? `会话 ${session.slice(0, 12)}…` : '（还没有会话 id）'}
        </p>
      )}

      <div className="mp-presets" role="group" aria-label="场景预设">
        <span className="mp-presets-label">场景预设</span>
        {PRESETS.map((preset) => (
          <button
            key={preset.id}
            type="button"
            className="mp-preset"
            title={preset.summary}
            onClick={() => patch(preset.patch)}
          >
            {preset.label}
          </button>
        ))}
        <span className="mp-presets-note">只填草稿，仍需保存</span>
      </div>

      {config.isError ? (
        <div className="cfg-error" role="alert">
          <p>读取模型参数失败。</p>
          <button type="button" className="ghost-btn" onClick={() => void config.refetch()}>
            重试
          </button>
        </div>
      ) : !shown || !base ? (
        <div className="cfg-skeleton" aria-busy="true" aria-label="正在读取模型参数">
          <span /><span /><span /><span />
        </div>
      ) : (
        <div className="mp-body">
          <nav className="mp-nav" aria-label="参数分组">
            {GROUPS.map((group) => (
              <button
                key={group.id}
                type="button"
                className="mp-nav-item"
                onClick={() => {
                  document.getElementById(`mp-group-${group.id}`)?.scrollIntoView?.({ block: 'start' });
                  setActiveField(group.items[0]?.key ?? activeField);
                }}
              >
                {group.label}
                {changedByGroup.get(group.id) ? (
                  <span className="mp-nav-count">{changedByGroup.get(group.id)}</span>
                ) : null}
              </button>
            ))}
          </nav>

          <div className="mp-scroll">
            <ModelForm
              shown={shown}
              draft={draft}
              sources={sources}
              bounds={config.data?.bounds ?? {}}
              activeField={activeField}
              providers={providers.data ?? null}
              providerId={providerId}
              models={catalog.models}
              catalogState={catalog.state}
              readonlyEntries={config.data?.readonly ?? {}}
              onPatch={patch}
              onReset={resetField}
              onFocusField={setActiveField}
              onLoadModels={() => {
                if (providerId && catalog.state.code === '' && !catalog.state.loading) {
                  void catalog.loadModels(providerId, false);
                }
              }}
              onRefreshModels={() => void catalog.loadModels(providerId, true)}
              onPickProvider={(next) => {
                patch({ provider: next });
                catalog.resetModels();
              }}
            />
          </div>

          <DocPanel
            field={activeField}
            doc={activeDoc}
            layers={layers.data?.layers ?? null}
            values={shown}
            derived={shownPreview?.derived ?? config.data?.derived}
            catalog={config.data?.catalog}
            error={fieldError}
            live={dirtyKeys.includes(activeField)}
          />
        </div>
      )}

      <footer className="mp-foot">
        {(dangers.length > 0 || localError) && (
          <div className="mp-danger">
            {localError ? (
              <p className="mp-danger-line">{localError}（服务端还会再校验一次）</p>
            ) : (
              <>
                {dangers.map((line) => (
                  <p className="mp-danger-line" key={line}>
                    {line}
                  </p>
                ))}
                <label className="mp-danger-ack">
                  <input
                    type="checkbox"
                    checked={ack}
                    onChange={(event) => setAck(event.target.checked)}
                  />
                  <span>我了解上面的影响</span>
                </label>
              </>
            )}
          </div>
        )}
        {notice && <p className="cfg-notice">{notice}</p>}
        <div className="cfg-actions">
          <span className="cfg-count">
            {dirtyKeys.length > 0 ? `未保存 ${dirtyKeys.length} 项` : '没有改动'}
          </span>
          {scope === 'session' && (
            <button
              type="button"
              className="ghost-btn"
              disabled={busy || !session}
              onClick={() => void clearSession()}
            >
              清空本会话覆盖
            </button>
          )}
          <button
            type="button"
            className="ghost-btn"
            disabled={!dirtyKeys.length}
            onClick={() => setDraft({})}
          >
            放弃
          </button>
          <button
            type="button"
            className="primary-btn"
            disabled={!dirtyKeys.length || blocked}
            onClick={() => void save()}
          >
            {scope === 'session' ? '保存到本次会话' : '保存为全局默认'}
          </button>
        </div>
      </footer>
    </section>
  );
}
