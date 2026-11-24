/** 模型设置面板：输入栏齿轮按钮 + 贴着输入框的 Popover。
 *
 * 为什么是 Popover 而不是独立设置页：13 个参数放进弹窗已偏多，但做成整页后
 * 「改完要跳回来才能继续对话」，调参与验证之间的往返成本太高。弹窗贴着输入框，
 * 改完立刻发一条消息就能看出效果。
 *
 * 三个必须说清的点（也是这里所有「多余」代码的理由）：
 * 1. 两个保存按钮的区别：「保存到本会话」只改自己，「设为部署默认」影响所有会话；
 * 2. 每行右侧的来源徽标：`framework-default` 是淡色的「未配置」，暗示该确认一下；
 * 3. 推算值（压缩阈值、最坏尝试次数）直接用后端给的，前端不重复实现口径。
 */
import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import {
  ApiError,
  clearSessionModelConfig,
  patchModelConfig,
  patchSessionModelConfig,
} from '../api/client';
import type { ModelBound, ModelConfig, ModelPatch, ModelSource, ModelValues } from '../types';
import { formatTokens } from './ContextRing';

/** 来源徽标文案。`framework-default` 不是「默认值」，是「没人设过」。 */
export const SOURCE_LABEL: Record<ModelSource, string> = {
  session: '本会话',
  stored: '部署默认',
  yaml: '配置文件',
  env: '环境变量',
  'framework-default': '未配置',
};

/** 推理强度的可选项：与后端 BOUNDS 的 choices 一致，后端仍是唯一裁判。 */
export const EFFORTS = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh'];

/** 空字符串表示「用默认」：数字/枚举的清空语义统一走 null。 */
function toNumber(raw: string, integer: boolean): number | null {
  const text = raw.trim();
  if (!text) return null;
  const value = Number(text);
  if (!Number.isFinite(value)) return null;
  return integer ? Math.round(value) : value;
}

/** 把结构化 detail 翻成一句人话；400 要指出字段，409 要指出版本冲突。 */
export function errorText(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return error instanceof Error ? error.message : '保存失败';
  }
  const detail = error.detail as { code?: string; field?: string; reason?: string } | null;
  if (detail?.code === 'invalid_model_config') {
    return `${detail.field}：${detail.reason ?? '取值非法'}`;
  }
  if (detail?.code === 'config_version_conflict') {
    return '配置已被别处改过，请关闭面板重开再改一次';
  }
  return error.message;
}

/** 来源徽标：只读项与框架默认各用不同色调，一眼能分出「这行能不能改」。 */
function SourceBadge({ source }: { source: ModelSource }) {
  return <span className={`mc-src is-${source}`}>{SOURCE_LABEL[source] ?? source}</span>;
}

interface RowProps {
  label: string;
  source?: ModelSource;
  hint?: string;
  children: ReactNode;
}

/** 一行：左标签 + 中间控件 + 右来源。 */
function Row({ label, source, hint, children }: RowProps) {
  return (
    <div className="mc-row">
      <span className="mc-row-label">{label}</span>
      <span className="mc-row-control">{children}</span>
      {source ? <SourceBadge source={source} /> : <span />}
      {hint && <span className="mc-row-hint">{hint}</span>}
    </div>
  );
}

interface NumProps {
  value: number | null;
  bound?: ModelBound;
  integer?: boolean;
  onChange: (value: number | null) => void;
  disabled?: boolean;
  ariaLabel: string;
}

/** 数字输入：清空 = 用默认（null），不是 0。 */
function NumInput({ value, bound, integer, onChange, disabled, ariaLabel }: NumProps) {
  return (
    <input
      className="mc-input mono"
      type="number"
      aria-label={ariaLabel}
      value={value ?? ''}
      min={bound?.min}
      max={bound?.max}
      step={bound?.step ?? (integer ? 1 : 0.05)}
      placeholder="默认"
      disabled={disabled}
      onChange={(e) => onChange(toNumber(e.target.value, integer ?? false))}
    />
  );
}

/** 危险改动提示：不是拦路，是让人知道自己在改什么。 */
function dangersOf(draft: ModelPatch, base: ModelValues): string[] {
  const notes: string[] = [];
  if (typeof draft.model === 'string' && draft.model !== base.model) {
    notes.push('下一次模型调用即生效。若模型名无效，该次调用会失败且不会自动回滚到旧模型。');
  }
  if (draft.context_size !== undefined && draft.context_size !== base.context_size) {
    notes.push('压缩阈值会按当前触发比例重算，窗口变小可能立刻触发一次压缩。');
  }
  if (draft.max_iters !== undefined && (draft.max_iters ?? 0) > 50) {
    notes.push(
      `一次回复最多可能进行 ${draft.max_iters} 轮工具调用，耗时与 token 消耗相应放大。`,
    );
  }
  if (draft.model_max_retries !== undefined || draft.agent_max_retries !== undefined) {
    // 具体次数由后端的 derived 给，这里只说「会变」，不自己乘
    notes.push('重试次数变了：最坏情况下的模型请求次数会随之放大（保存后见推算值）。');
  }
  return notes;
}

/** 本地越界检查：只为提前禁用保存，服务端仍是唯一裁判。 */
export function boundError(
  values: ModelPatch,
  bounds: Record<string, ModelBound>,
): string {
  const table = values as unknown as Record<string, unknown>;
  for (const [key, bound] of Object.entries(bounds)) {
    const value = table[key];
    if (typeof value !== 'number') continue;
    if (bound.min !== undefined && value < bound.min) return `${key} 不能小于 ${bound.min}`;
    if (bound.max !== undefined && value > bound.max) return `${key} 不能大于 ${bound.max}`;
  }
  return '';
}

interface Props {
  session: string;
  config: ModelConfig | undefined;
  /** 当前上下文占用：用于判断「改窗口会不会立刻压缩」。 */
  contextTokens: number | null;
  onChanged: () => void;
}

export function ModelConfigPopover({ session, config, contextTokens, onChanged }: Props) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<ModelPatch>({});
  const [notice, setNotice] = useState('');
  const [ack, setAck] = useState(false);
  const [confirmDeploy, setConfirmDeploy] = useState(false);
  const [busy, setBusy] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const gearRef = useRef<HTMLButtonElement | null>(null);
  const hadOpen = useRef(false);
  const queryClient = useQueryClient();

  // 每次打开都从当前生效值开始：草稿跨会话/跨版本残留会让「改的是哪一份」说不清。
  // 重置必须发生在 updater 之外：updater 在 StrictMode 下会跑两次，不能有副作用。
  function toggleOpen() {
    if (!open) {
      setDraft({});
      setAck(false);
      setConfirmDeploy(false);
      setNotice('');
    }
    setOpen(!open);
  }

  // 打开期间：点外关闭、Esc 关闭，并把焦点送进面板 / 关闭时还给齿轮。
  // focus 不是 setState，放在 effect 里安全；setOpen 只出现在事件回调里。
  useEffect(() => {
    if (!open) {
      if (hadOpen.current) gearRef.current?.focus();
      hadOpen.current = false;
      return undefined;
    }
    hadOpen.current = true;
    panelRef.current?.focus();
    function onDocMouseDown(event: MouseEvent) {
      if (!wrapRef.current?.contains(event.target as Node)) setOpen(false);
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false);
    }
    document.addEventListener('mousedown', onDocMouseDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onDocMouseDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const base = config?.effective;
  const shown = base ? { ...base, ...draft } : null;
  const sources = { ...(config?.sources ?? {}) } as Record<string, ModelSource>;
  const overrides = config?.overrides ?? {};
  const edited = draft;

  /** 草稿里改过的项要显示成「本会话/部署默认」之前的样子：先标成待保存。 */
  function patch(change: ModelPatch) {
    setDraft((prev) => ({ ...prev, ...change }));
    setAck(false);
    setConfirmDeploy(false);
    setNotice('');
  }

  /** 保存前的模型名归一：去首尾空格，空串 = 删覆盖（null），不留一条空模型名。 */
  function normalizeModel(edit: ModelPatch): ModelPatch {
    if (!('model' in edit)) return edit;
    const text = (edit.model ?? '').trim();
    return { ...edit, model: text === '' ? null : text };
  }

  async function saveSession() {
    if (Object.keys(edited).length === 0) {
      setNotice('没有改动');
      return;
    }
    setBusy(true);
    try {
      // 后端 apply_patch 把 null 当删键，ModelPatch 已按这个语义声明类型
      const result = await patchSessionModelConfig(session, normalizeModel(edited));
      setNotice(`已保存到本会话 · ${result.effective_from ?? '下一轮生效'}`);
      setDraft({});
      await queryClient.invalidateQueries({ queryKey: ['model-config'] });
      onChanged();
    } catch (error) {
      setNotice(errorText(error));
    } finally {
      setBusy(false);
    }
  }

  async function saveDefaults() {
    setBusy(true);
    try {
      const result = await patchModelConfig(normalizeModel(edited), config?.version);
      const affected = result.affected ?? { applied: 0, deferred: 0 };
      setNotice(
        `已设为部署默认：${affected.applied} 个会话立即生效，${affected.deferred} 个等当前轮结束`,
      );
      setDraft({});
      setConfirmDeploy(false);
      await queryClient.invalidateQueries({ queryKey: ['model-config'] });
      onChanged();
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
      setNotice('已恢复为本会话继承的默认值');
      setDraft({});
      await queryClient.invalidateQueries({ queryKey: ['model-config'] });
      onChanged();
    } catch (error) {
      setNotice(errorText(error));
    } finally {
      setBusy(false);
    }
  }

  const dirty = Object.keys(edited).length > 0;
  const dangers = base ? dangersOf(edited, base) : [];
  const needsAck = dangers.length > 0 && !ack;
  const invalid = shown ? boundError(shown, config?.bounds ?? {}) : '';
  const retriesDirty =
    edited.model_max_retries !== undefined || edited.agent_max_retries !== undefined;
  const ratiosDirty =
    edited.trigger_ratio !== undefined ||
    edited.context_size !== undefined ||
    edited.reserve_ratio !== undefined;

  if (!open) {
    return (
      <div className="mc-wrap" ref={wrapRef}>
        <button
          ref={gearRef}
          type="button"
          className="mc-gear"
          aria-label="模型设置"
          aria-expanded={false}
          title="模型参数：会话级覆盖 / 部署默认"
          onClick={toggleOpen}
        >
          <GearIcon />
        </button>
      </div>
    );
  }

  return (
    <div className="mc-wrap" ref={wrapRef}>
      <button
        ref={gearRef}
        type="button"
        className="mc-gear is-open"
        aria-label="模型设置"
        aria-expanded
        onClick={toggleOpen}
      >
        <GearIcon />
      </button>
      <div className="mc-panel" role="dialog" aria-label="模型设置" tabIndex={-1} ref={panelRef}>
        <div className="mc-head">
          <span className="mc-title">模型设置</span>
          <span className="mc-badge mono">
            {Object.keys(overrides).length > 0 ? `本会话 ${Object.keys(overrides).length} 项` : '继承默认'}
          </span>
        </div>

        {!shown || !base ? (
          <div className="mc-empty mono">正在读取配置…</div>
        ) : (
          <div className="mc-body">
            <Row label="模型" source={edited.model !== undefined ? 'session' : sources.model} hint={config?.derived.context_uncalibrated ? '上下文窗口未校准' : undefined}>
              <input
                className="mc-input mono"
                aria-label="模型名"
                value={shown.model ?? ''}
                placeholder="未配置（走环境变量）"
                // 输入时保留原值：否则每敲一个空格就被 trim 掉，光标会跳
                onChange={(e) => patch({ model: e.target.value })}
                // 离开时才归一：去尾随空格；空串 = 删本层覆盖（null），不写空模型名
                onBlur={(e) => {
                  const text = e.target.value.trim();
                  const next = text === '' ? null : text;
                  if (next !== (shown.model ?? null)) patch({ model: next });
                }}
              />
            </Row>
            <Row label="上下文窗口" source={sources.context_size} hint="空 = 跟随下层">
              <NumInput
                ariaLabel="上下文窗口"
                value={shown.context_size}
                bound={config?.bounds.context_size}
                integer
                onChange={(value) => patch({ context_size: value })}
              />
            </Row>
            {config?.derived.context_uncalibrated && (
              <p className="mc-warn">
                {formatTokens(base.context_size)} 是框架默认值，没有经过确认。填成模型的真实窗口
                才能让压缩阈值和占用圆环准。
              </p>
            )}

            <div className="mc-section">采样</div>
            <Row label="温度" source={sources.temperature} hint="空 = 供应商默认">
              <NumInput
                ariaLabel="温度"
                value={shown.temperature}
                bound={config?.bounds.temperature}
                onChange={(value) => patch({ temperature: value })}
              />
            </Row>
            <Row label="top_p" source={sources.top_p} hint="空 = 供应商默认">
              <NumInput
                ariaLabel="top_p"
                value={shown.top_p}
                bound={config?.bounds.top_p}
                onChange={(value) => patch({ top_p: value })}
              />
            </Row>
            <Row label="最大输出" source={sources.max_tokens} hint="空 = 不限制">
              <NumInput
                ariaLabel="最大输出"
                value={shown.max_tokens}
                bound={config?.bounds.max_tokens}
                integer
                onChange={(value) => patch({ max_tokens: value })}
              />
            </Row>
            <Row label="推理强度" source={sources.reasoning_effort}>
              <select
                className="mc-input"
                aria-label="推理强度"
                value={shown.reasoning_effort ?? ''}
                onChange={(e) => patch({ reasoning_effort: e.target.value || null })}
              >
                <option value="">默认</option>
                {EFFORTS.map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </select>
            </Row>

            <div className="mc-section">迭代与重试</div>
            <Row label="迭代上限" source={sources.max_iters} hint="空 = 跟随下层">
              <NumInput
                ariaLabel="迭代上限"
                value={shown.max_iters}
                bound={config?.bounds.max_iters}
                integer
                onChange={(value) => patch({ max_iters: value })}
              />
            </Row>
            <Row label="模型重试" source={sources.model_max_retries} hint="HTTP 层 · 空 = 跟随下层">
              <NumInput
                ariaLabel="模型重试"
                value={shown.model_max_retries}
                bound={config?.bounds.model_max_retries}
                integer
                onChange={(value) => patch({ model_max_retries: value })}
              />
            </Row>
            <Row label="智能体重试" source={sources.agent_max_retries} hint="ReAct 层 · 空 = 跟随下层">
              <NumInput
                ariaLabel="智能体重试"
                value={shown.agent_max_retries}
                bound={config?.bounds.agent_max_retries}
                integer
                onChange={(value) => patch({ agent_max_retries: value })}
              />
            </Row>
            <p
              className={`mc-derived${
                (config?.derived.worst_case_attempts ?? 0) > 6 ? ' is-warn' : ''
              }`}
            >
              {`最坏 ${config?.derived.worst_case_attempts ?? 0} 次模型请求 = (1 + ${base.model_max_retries}) × (1 + ${base.agent_max_retries})`}
              {retriesDirty ? '（保存后重算）' : ''}
              <span className="mc-derived-src">后端算的</span>
            </p>

            <div className="mc-section">上下文压缩</div>
            <Row label="触发比例" source={sources.trigger_ratio} hint="空 = 跟随下层">
              <NumInput
                ariaLabel="触发比例"
                value={shown.trigger_ratio}
                bound={config?.bounds.trigger_ratio}
                onChange={(value) => patch({ trigger_ratio: value })}
              />
            </Row>
            <Row label="保留比例" source={sources.reserve_ratio} hint="空 = 跟随下层">
              <NumInput
                ariaLabel="保留比例"
                value={shown.reserve_ratio}
                bound={config?.bounds.reserve_ratio}
                onChange={(value) => patch({ reserve_ratio: value })}
              />
            </Row>
            <Row label="预警缓冲" source={sources.context_buffer_ratio} hint="空 = 跟随下层">
              <NumInput
                ariaLabel="预警缓冲"
                value={shown.context_buffer_ratio}
                bound={config?.bounds.context_buffer_ratio}
                onChange={(value) => patch({ context_buffer_ratio: value })}
              />
            </Row>
            <p className="mc-derived">
              {`阈值 ${formatTokens(config?.derived.compression_threshold ?? 0)} tokens`}
              {ratiosDirty && '（保存后重算）'}
              {!ratiosDirty &&
                contextTokens !== null &&
                (contextTokens >= (config?.derived.compression_threshold ?? 0)
                  ? ' · 当前占用已超过它，下一轮会立即压缩'
                  : ` · 当前占用 ${formatTokens(contextTokens)}，下一轮不会压缩`)}
              <span className="mc-derived-src">后端算的</span>
            </p>

            <div className="mc-section">只读</div>
            {Object.entries(config?.readonly ?? {}).map(([key, note]) => (
              <Row key={key} label={key}>
                <span className="mc-readonly mono">不可在界面修改</span>
                <span className="mc-row-hint">{note}</span>
              </Row>
            ))}

            {dangers.map((text) => (
              <p className="mc-warn" key={text}>
                {text}
              </p>
            ))}
            {dangers.length > 0 && (
              <label className="mc-ack">
                <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
                我了解上面的影响
              </label>
            )}
            {invalid && <p className="mc-warn">{invalid}</p>}
            {(config?.warnings ?? []).map((text) => (
              <p className="mc-warn" key={text}>
                {text}
              </p>
            ))}
          </div>
        )}

        {notice && (
          <p className="mc-notice" role="status">
            {notice}
          </p>
        )}

        {confirmDeploy ? (
          <div className="mc-confirm">
            <p>
              将影响所有会话的下一次模型调用。正在跑的会话要等当前轮结束才生效，
              不会打断它们的流。
            </p>
            <div className="mc-actions">
              <button className="ghost-btn" onClick={() => setConfirmDeploy(false)}>
                取消
              </button>
              <button className="danger-btn" disabled={busy} onClick={() => void saveDefaults()}>
                确认写入
              </button>
            </div>
          </div>
        ) : (
          <div className="mc-actions">
            <button className="ghost-btn" disabled={busy} onClick={() => void resetSession()}>
              恢复默认
            </button>
            <button
              className="ghost-btn"
              disabled={busy || !dirty || needsAck || Boolean(invalid)}
              onClick={() => setConfirmDeploy(true)}
            >
              设为部署默认
            </button>
            <button
              className="primary-btn"
              disabled={busy || !dirty || needsAck || Boolean(invalid)}
              onClick={() => void saveSession()}
            >
              保存到本会话
            </button>
          </div>
        )}
      </div>
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
