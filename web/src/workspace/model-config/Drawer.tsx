/** 模型设置抽屉：左树分类、右侧分组表单、底部保存范围。
 *
 * 为什么从贴输入框的 Popover 改成抽屉：13 个参数平铺在一个 420px 浮层里，
 * 「哪个值从哪来」和「这次改的是哪一层」两件事都被淹没。抽屉给左侧留出
 * 分类树（连接 / 采样 / 执行 / 压缩 / 高级），一次只显示一组，改完不用滚动找。
 *
 * 三个必须说清的点：
 * 1. 主按钮是「保存为全局默认」：落盘到 config/model.local.yaml，换会话仍在；
 *    「仅本次会话」降为次要，并在标题下常驻提示「换会话失效」——实测 30 个
 *    会话里只有 2 条会话覆盖、0 条全局默认，就是主次颠倒造成的。
 * 2. 每行徽标可点开四层链路（会话 / 文件 / 数据库 / yaml+env+框架），
 *    回答的是「这个 128000 到底谁设的」。
 * 3. 用原生 <dialog>：焦点、Esc、背景 inert 由浏览器负责，不手写 focus trap。
 * 4. 网关与模型是**联动的两级**：先选网关，它的 /models 才决定模型候选。但下拉
 *    只是提示 —— 网关的 /models 常不全，手填模型名是合法用法（手填时窗口按
 *    「未匹配」处理并给提示，不拦保存）。
 */
import { useQueryClient } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { useEffect, useRef, useState } from 'react';
import {
  clearSessionModelConfig,
  fetchModelLayers,
  patchModelConfig,
  patchSessionModelConfig,
} from '../../api/client';
import type { ModelConfig, ModelLayer, ModelPatch, ModelSource } from '../../types';
import { formatTokens } from '../ContextRing';
import { matchLabel, sourceLabel, statusLabel } from './capability';
import { DrawerFooter } from './DrawerFooter';
import { FieldRow } from './FieldRow';
import { GROUPS } from './groups';
import { GroupsNav } from './GroupsNav';
import { ModelCombobox } from './ModelCombobox';
import { ProviderSelect } from './ProviderSelect';
import { useCatalog } from './useCatalog';
import { boundError, dangersOf, errorText } from './validation';

interface Props {
  session: string;
  config: ModelConfig | undefined;
  /** 当前上下文占用：用于判断「改窗口会不会立刻压缩」。 */
  contextTokens: number | null;
  onChanged: () => void;
}

/** 只显示最后两段：`config/model.local.yaml` 比一整条 Windows 路径好认。 */
function shortPath(path: string | undefined): string {
  if (!path) return 'config/model.local.yaml';
  const parts = path.split(/[\\/]/);
  return parts.slice(-2).join('/');
}

export function ModelConfigDrawer({ session, config, contextTokens, onChanged }: Props) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState('connection');
  const [expanded, setExpanded] = useState<Record<string, boolean>>({
    connection: true,
    sampling: false,
    runtime: false,
    compression: false,
    advanced: false,
  });
  const [activeField, setActiveField] = useState<string | null>(null);
  const [draft, setDraft] = useState<ModelPatch>({});
  const [layers, setLayers] = useState<ModelLayer[] | null>(null);
  const [chainField, setChainField] = useState<string | null>(null);
  const [notice, setNotice] = useState('');
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirmGlobal, setConfirmGlobal] = useState(false);
  const catalog = useCatalog();
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const queryClient = useQueryClient();

  const base = config?.effective;
  const shown = base ? { ...base, ...draft } : null;
  const sources = { ...(config?.sources ?? {}) } as Record<string, ModelSource>;
  const dirtyCount = Object.keys(draft).length;
  const overrideCount = Object.keys(config?.overrides ?? {}).length;
  // 生效网关：草稿 > 已生效 > 注册表默认。空串在后端等于「用注册表默认」，
  // 但界面需要一个具体值来查 /models，所以这里补齐。逐级可选：`config` 还没到时
  // 整段是 undefined，不能直接点下去（面板会比配置先渲染一帧）。
  const providerId =
    (draft.provider as string | undefined) ||
    config?.catalog?.provider ||
    base?.provider ||
    catalog.providers?.default ||
    '';

  // 原生 dialog 的开关只走 showModal/close：`open` 属性不会进 top layer，也没有 backdrop。
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      if (typeof dialog.showModal === 'function') dialog.showModal();
      else dialog.setAttribute('open', '');
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open]);

  // Esc 与「关闭」都要保护未保存改动：原生 cancel 可 preventDefault，不必自己抓键盘。
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return undefined;
    function onCancel(event: Event) {
      if (Object.keys(draft).length > 0) {
        event.preventDefault();
        setNotice('有未保存的改动：先「保存」，或点「恢复本会话」放弃');
      }
    }
    dialog.addEventListener('cancel', onCancel);
    return () => dialog.removeEventListener('cancel', onCancel);
  }, [draft]);

  function patch(change: ModelPatch) {
    setDraft((prev) => ({ ...prev, ...change }));
    setAck(false);
    setConfirmGlobal(false);
    setNotice('');
  }

  /** 撤销某一项：draft 里删键，同时把该字段从会话覆盖里移除（保存时生效）。 */
  function resetField(key: string) {
    setDraft((prev) => {
      const next = { ...prev };
      delete next[key as keyof ModelPatch];
      return next;
    });
    setNotice(`已撤销「${key}」的草稿改动；要清掉会话里已有的覆盖，点「恢复本会话」`);
  }

  /** 模型名归一：空串 = 删覆盖（null），不留一条空模型名。 */
  function normalize(edit: ModelPatch): ModelPatch {
    if (!('model' in edit)) return edit;
    const text = (edit.model ?? '').trim();
    return { ...edit, model: text === '' ? null : text };
  }

  /** 换网关：模型候选与列表状态必须一起清掉，否则会拿着 A 的列表去查 B。 */
  function pickProvider(next: string) {
    patch({ provider: next });
    catalog.resetModels();
  }

  async function refresh() {
    setLayers(null);
    await queryClient.invalidateQueries({ queryKey: ['model-config'] });
    onChanged();
  }

  async function saveSession() {
    if (dirtyCount === 0) {
      setNotice('没有改动');
      return;
    }
    setBusy(true);
    try {
      const result = await patchSessionModelConfig(session, normalize(draft));
      setDraft({});
      setNotice(`已保存到本会话 · ${result.effective_from ?? '下一轮生效'}`);
      await refresh();
    } catch (error) {
      setNotice(errorText(error));
    } finally {
      setBusy(false);
    }
  }

  async function saveGlobal() {
    setBusy(true);
    try {
      const result = await patchModelConfig(normalize(draft), config?.version);
      const affected = result.affected ?? { applied: 0, deferred: 0 };
      setDraft({});
      setConfirmGlobal(false);
      setNotice(
        `已写入 ${shortPath(result.file_path)}，换会话仍在 · ${affected.applied} 个会话立即生效，${affected.deferred} 个等当前轮结束`,
      );
      await refresh();
    } catch (error) {
      setNotice(errorText(error));
    } finally {
      setBusy(false);
    }
  }

  async function resetSession() {
    setBusy(true);
    try {
      await clearSessionModelConfig(session);
      setDraft({});
      setNotice('已清空本会话覆盖，回到部署默认');
      await refresh();
    } catch (error) {
      setNotice(errorText(error));
    } finally {
      setBusy(false);
    }
  }

  async function toggleChain(key: string) {
    if (chainField === key) {
      setChainField(null);
      return;
    }
    setChainField(key);
    if (layers !== null) return;
    try {
      const body = await fetchModelLayers(session);
      setLayers(body.layers);
    } catch {
      setChainField(null);
      setNotice('读取来源链失败：后端可能还是旧版本');
    }
  }

  function openPanel() {
    setDraft({});
    setNotice('');
    setAck(false);
    setConfirmGlobal(false);
    setLayers(null);
    setChainField(null);
    setActiveField(null);
    setActive('connection');
    catalog.reset();
    setOpen(true);
    void catalog.loadProviders();
  }

  function closePanel() {
    if (dirtyCount > 0) {
      setNotice('有未保存的改动：先「保存」，或点「恢复本会话」放弃');
      return;
    }
    setOpen(false);
  }

  const dangers = base ? dangersOf(draft, base) : [];
  const invalid = shown ? boundError(shown, config?.bounds ?? {}) : '';
  const group = GROUPS.find((item) => item.id === active) ?? GROUPS[0];

  /** 网关 / 模型两行用组合控件，其余行走 FieldRow 内置输入框。 */
  function controlFor(key: string): ReactNode {
    if (key === 'provider') {
      return (
        <ProviderSelect
          providers={catalog.providers}
          value={providerId}
          onChange={pickProvider}
        />
      );
    }
    if (key === 'model') {
      return (
        <ModelCombobox
          value={typeof shown?.model === 'string' ? shown.model : ''}
          providerId={providerId}
          models={catalog.models}
          loading={catalog.state.loading}
          code={catalog.state.code}
          total={catalog.state.total}
          stale={catalog.state.stale}
          onChange={(next) => patch({ model: next })}
          // 只取一次：code 已经是 ok/unauthorized 等结论时不再打上游
          onLoad={() => {
            if (catalog.state.code === '' && !catalog.state.loading) {
              void catalog.loadModels(providerId, false);
            }
          }}
          onRefresh={() => void catalog.loadModels(providerId, true)}
        />
      );
    }
    return undefined;
  }

  return (
    <div className="mc-wrap">
      <button
        type="button"
        className={`mc-gear${open ? ' is-open' : ''}`}
        aria-label="模型设置"
        aria-expanded={open}
        title="模型参数：全局默认 / 本次会话覆盖"
        onClick={openPanel}
      >
        <GearIcon />
      </button>

      <dialog
        ref={dialogRef}
        className="mcd"
        aria-labelledby="mcd-title"
        onClose={() => setOpen(false)}
      >
        <header className="mcd-head">
          <div>
            <h1 id="mcd-title" className="mcd-title">
              模型设置
            </h1>
            <p className="mcd-meta">
              部署默认 v{config?.version ?? 0}
              {overrideCount > 0
                ? ` · 本次会话覆盖 ${overrideCount} 项（换会话失效）`
                : ' · 本次会话无覆盖'}
            </p>
          </div>
          <button type="button" className="mcd-close" aria-label="关闭模型设置" onClick={closePanel}>
            ✕
          </button>
        </header>

        <div className="mcd-status">
          <span className="mcd-status-key">落盘</span>
          <code className="mcd-status-path" title={config?.file_path}>
            {shortPath(config?.file_path)}
          </code>
          <span className="mcd-status-sep">·</span>
          <span>改完点「保存为全局默认」才对所有会话生效</span>
          {dirtyCount > 0 && <span className="mcd-status-dirty">{dirtyCount} 项待保存</span>}
        </div>

        {!shown || !base ? (
          <div className="mcd-empty mono">正在读取配置…</div>
        ) : (
          <div className="mcd-body">
            <GroupsNav
              active={active}
              expanded={expanded}
              activeField={activeField}
              draft={draft}
              onGroup={(id) => {
                setActive(id);
                setExpanded((prev) => ({ ...prev, [id]: !prev[id] }));
              }}
              onField={(groupId, key) => {
                setActive(groupId);
                setActiveField(key);
              }}
            />

            <div className="mcd-pane">
              <section aria-labelledby={`mcd-h-${group.id}`}>
                <h2 id={`mcd-h-${group.id}`} className="mcd-pane-title">
                  {group.label}
                </h2>
                <p className="mcd-pane-summary">{group.summary}</p>

                {group.items.map((spec) => (
                  <FieldRow
                    key={spec.key}
                    spec={spec}
                    value={shown[spec.key]}
                    bound={config?.bounds[spec.key]}
                    source={draft[spec.key] !== undefined ? 'session' : sources[spec.key]}
                    dirty={draft[spec.key] !== undefined}
                    highlight={activeField === spec.key}
                    layers={chainField === spec.key ? layers : null}
                    chainOpen={chainField === spec.key}
                    control={controlFor(spec.key)}
                    onToggleChain={() => void toggleChain(spec.key)}
                    onPatch={patch}
                    onReset={() => resetField(spec.key)}
                  />
                ))}

                {group.id === 'connection' && catalog.error && (
                  <p className="mc-warn">{catalog.error}</p>
                )}

                {group.id === 'connection' && config?.catalog && (
                  <p className="mc-derived">
                    {`窗口 ${formatTokens(shown.context_size ?? base?.context_size ?? 0)} ← ${sourceLabel(
                      config.catalog.current.context_source,
                    )}`}
                    {` · ${matchLabel(config.catalog.current.matched_by)}`}
                    {` · ${statusLabel(config.catalog.current.status)}`}
                    {` · ${config.catalog.providers_configured} 个网关`}
                    <span className="mc-derived-src">后端查的</span>
                  </p>
                )}

                {group.id === 'connection' && config?.catalog?.current.card_may_be_stale && (
                  <p className="mc-warn">
                    这个窗口来自内置模型卡，可能滞后于网关的新版本。若网关 /models
                    给出了更大的数字，刷新模型列表后会改用网关声明值。
                  </p>
                )}

                {group.id === 'connection' && config?.derived.context_uncalibrated && (
                  <p className="mc-warn">
                    {formatTokens(base.context_size)} 是框架默认值，没有经过确认。填成模型的真实窗口
                    才能让压缩阈值和占用圆环准。
                  </p>
                )}

                {group.id === 'runtime' && (
                  <p
                    className={`mc-derived${
                      (config?.derived.worst_case_attempts ?? 0) > 6 ? ' is-warn' : ''
                    }`}
                  >
                    {`最坏 ${config?.derived.worst_case_attempts ?? 0} 次模型请求 = (1 + ${base.model_max_retries}) × (1 + ${base.agent_max_retries})`}
                    {(draft.model_max_retries !== undefined ||
                      draft.agent_max_retries !== undefined) &&
                      '（保存后重算）'}
                    <span className="mc-derived-src">后端算的</span>
                  </p>
                )}

                {group.id === 'compression' && (
                  <p className="mc-derived">
                    {`阈值 ${formatTokens(config?.derived.compression_threshold ?? 0)} tokens`}
                    {(draft.trigger_ratio !== undefined ||
                      draft.context_size !== undefined ||
                      draft.reserve_ratio !== undefined) &&
                      '（保存后重算）'}
                    {draft.trigger_ratio === undefined &&
                      draft.context_size === undefined &&
                      contextTokens !== null &&
                      (contextTokens >= (config?.derived.compression_threshold ?? 0)
                        ? ' · 当前占用已超过它，下一轮会立即压缩'
                        : ` · 当前占用 ${formatTokens(contextTokens)}，下一轮不会压缩`)}
                    <span className="mc-derived-src">后端算的</span>
                  </p>
                )}

                {group.id === 'advanced' &&
                  Object.entries(config?.readonly ?? {}).map(([key, note]) => (
                    <div className="mcd-row" key={key}>
                      <div className="mcd-row-head">
                        <span className="mcd-row-label mono">{key}</span>
                        <span className="mc-readonly">不可在界面修改</span>
                      </div>
                      <span className="mcd-row-hint">{note}</span>
                    </div>
                  ))}
              </section>
            </div>
          </div>
        )}

        <DrawerFooter
          notice={notice}
          dangers={dangers}
          warnings={config?.warnings ?? []}
          invalid={invalid}
          ack={ack}
          busy={busy}
          dirtyCount={dirtyCount}
          confirming={confirmGlobal}
          filePath={shortPath(config?.file_path)}
          onAck={setAck}
          onAskGlobal={() => setConfirmGlobal(true)}
          onCancelConfirm={() => setConfirmGlobal(false)}
          onSaveGlobal={() => void saveGlobal()}
          onResetSession={() => void resetSession()}
          onSaveSession={() => void saveSession()}
        />
      </dialog>
    </div>
  );
}

/** 齿轮：与权限选择器、联网按钮同一套 13px 图标风格。 */
function GearIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <circle cx="8" cy="8" r="2.2" stroke="currentColor" strokeWidth="1.3" />
      <path
        d="M8 1.6v1.7M8 12.7v1.7M1.6 8h1.7M12.7 8h1.7M3.5 3.5l1.2 1.2M11.3 11.3l1.2 1.2M12.5 3.5l-1.2 1.2M4.7 11.3l-1.2 1.2"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
      />
    </svg>
  );
}
