/**
 * 网关表单（SPEC-0022 §6.2）：只收「变量名」的密钥引用。
 *
 * 校验分工：这里做形状提示（让人少打一次往返），服务端仍是唯一裁判 ——
 * 尤其是明文 key，服务端硬拦，前端提示只是提前告知。
 */
import { useState } from 'react';
import { ApiError } from '../../../api/client';
import { createProvider, updateProvider } from '../../../api/client';
import type { CatalogKind, ProviderDraft } from '../../../types';

const DISCOVER_LABEL: Record<string, string> = {
  models: '从网关 /models 自动发现',
  manual: '手写模型清单',
  off: '离线（只用内置卡）',
};

const PLAINTEXT = /^(sk-|ghp_|gho_|AKIA|xox[baprs]-)/;

interface Props {
  initial: ProviderDraft;
  isNew: boolean;
  version: string;
  kinds: CatalogKind[];
  onCancel: () => void;
  onSaved: (message: string) => void;
}

/** 服务端的 code → 一句人话。未识别的码不吞掉，原样带上。 */
function messageOf(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return error instanceof Error ? error.message : '保存失败';
  }
  const detail = error.detail as { code?: string; reason?: string; available?: string[] } | null;
  switch (detail?.code) {
    case 'plaintext_key':
      return '这里填环境变量名，不要粘贴密钥：把 key 写进部署环境，再在这里填变量名。';
    case 'providers_version_conflict':
      return '文件已被别处改过。刷新页面看到最新内容后再改。';
    case 'duplicate_id':
      return '这个 id 已经存在，换一个。';
    case 'invalid_default':
      return detail.reason ?? '默认网关必须存在于列表里。';
    case 'version_required':
      return '缺少版本号：刷新页面重试。';
    default:
      return detail?.reason ? `${detail.reason}` : error.message;
  }
}

export function ProviderForm({ initial, isNew, version, kinds, onCancel, onSaved }: Props) {
  const [draft, setDraft] = useState<ProviderDraft>(initial);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  function set<K extends keyof ProviderDraft>(key: K, value: ProviderDraft[K]) {
    setDraft((prev) => ({ ...prev, [key]: value }));
    setError('');
  }

  const plaintext = PLAINTEXT.test(draft.api_key_ref.trim());

  async function submit() {
    if (plaintext) {
      setError('这里填环境变量名，不要粘贴密钥。');
      return;
    }
    setBusy(true);
    setError('');
    try {
      const body = { provider: draft, expected_version: version };
      if (isNew) await createProvider(body);
      else await updateProvider(draft.id, body);
      onSaved(isNew ? `已新增网关 ${draft.id}` : `已更新网关 ${draft.id}`);
    } catch (err) {
      setError(messageOf(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="prov-form"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <h3 className="prov-form-title">{isNew ? '新增网关' : `编辑 ${draft.id}`}</h3>

      <div className="prov-grid">
        <label className="prov-field">
          <span>id</span>
          <input
            className="mc-input mono"
            value={draft.id}
            disabled={!isNew}
            required
            pattern="[a-z0-9][a-z0-9_-]{1,31}"
            title="小写字母/数字/下划线/中划线，2–32 字符；保存后不可改名"
            onChange={(event) => set('id', event.target.value)}
          />
          <small>{isNew ? '保存后不可改名：会话里存的是这个 id' : 'id 不可改：改名等于删了重建'}</small>
        </label>

        <label className="prov-field">
          <span>显示名</span>
          <input
            className="mc-input"
            value={draft.label}
            placeholder="例如 内网 vLLM"
            onChange={(event) => set('label', event.target.value)}
          />
        </label>

        <label className="prov-field">
          <span>协议</span>
          <select
            className="mc-input"
            value={draft.kind}
            onChange={(event) => set('kind', event.target.value)}
          >
            {kinds.length === 0 && <option value={draft.kind}>{draft.kind}</option>}
            {kinds.map((item) => (
              <option key={item.kind} value={item.kind}>
                {item.kind} · {item.family}
              </option>
            ))}
          </select>
        </label>

        <label className="prov-field">
          <span>模型来源</span>
          <select
            className="mc-input"
            value={draft.discover}
            onChange={(event) => set('discover', event.target.value)}
          >
            {Object.entries(DISCOVER_LABEL).map(([value, text]) => (
              <option key={value} value={value}>
                {text}
              </option>
            ))}
          </select>
        </label>

        <label className="prov-field prov-field-wide">
          <span>base_url</span>
          <input
            className="mc-input mono"
            value={draft.base_url}
            placeholder="https://…/v1；明文 http 只允许 127.0.0.1"
            onChange={(event) => set('base_url', event.target.value)}
          />
        </label>

        <label className="prov-field prov-field-wide">
          <span>key 的环境变量名</span>
          <input
            className={`mc-input mono${plaintext ? ' is-invalid' : ''}`}
            value={draft.api_key_ref}
            placeholder="例如 DASHSCOPE_API_KEY，或 none"
            onChange={(event) => set('api_key_ref', event.target.value)}
          />
          <small>
            只填变量名（服务端拼成 env:NAME）。不需要认证的网关填 <code>none</code>。
          </small>
        </label>
      </div>

      {error && (
        <p className="prov-error" role="alert">
          {error}
        </p>
      )}

      <div className="cfg-actions">
        <button type="button" className="ghost-btn" onClick={onCancel}>
          取消
        </button>
        <button type="submit" className="primary-btn" disabled={busy || !draft.id || !draft.kind}>
          {busy ? '保存中…' : '保存到 providers.yaml'}
        </button>
      </div>
    </form>
  );
}
