/**
 * 网关管理（SPEC-0022 §6）：列表 + 增删改 + 探活。
 *
 * 为什么仍写 `providers.yaml` 而不是 `config.db`：里面存的是凭据**引用**，
 * 这份文件要能进 code review、要在没有数据库的环境独立工作。界面只提供
 * 「带校验的写入口」，不改变真相归属。
 *
 * 三个刻意的取舍：
 * 1. 删除默认网关时要求选接任者 —— 否则会话会指向一个不存在的家。
 * 2. 探活只读不写：拉一次上游 `/models`，回状态与耗时。
 * 3. 保存后重排文件（注释按模板重建），页面明确告知 —— 不假装能保留手写注释。
 */
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  checkProvider,
  deleteProvider,
  fetchProviderDraft,
  fetchProviders,
} from '../../../api/client';
import type { CatalogKind, ProviderCheck, ProviderDraft, ProvidersPayloadVersioned } from '../../../types';
import { ProviderForm } from './ProviderForm';

interface Props {
  onDirtyChange: (dirty: boolean) => void;
}

const BLANK: ProviderDraft = {
  id: '',
  kind: 'openai_chat',
  label: '',
  base_url: '',
  api_key_ref: '',
  discover: 'models',
};

function codeText(code: string): string {
  switch (code) {
    case 'ok':
      return '可达';
    case 'unauthorized':
      return 'key 被拒';
    case 'not_configured':
      return '缺 key';
    case 'rate_limited':
      return '限流';
    case 'unsupported':
      return '不支持发现';
    case 'empty':
      return '列表为空';
    case 'unavailable':
      return '连不上';
    case 'malformed':
      return '返回格式异常';
    default:
      return code;
  }
}

export function ProvidersPanel({ onDirtyChange }: Props) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<{ draft: ProviderDraft; isNew: boolean } | null>(null);
  const [message, setMessage] = useState('');
  const [checks, setChecks] = useState<Record<string, ProviderCheck>>({});
  const [deleting, setDeleting] = useState<{ id: string; reassign: string } | null>(null);

  const providers = useQuery({ queryKey: ['providers'], queryFn: fetchProviders });
  const data = providers.data as ProvidersPayloadVersioned | undefined;
  const version = data?.version ?? '';
  const kinds: CatalogKind[] = data?.kinds ?? [];

  function markEditing(next: { draft: ProviderDraft; isNew: boolean } | null) {
    setEditing(next);
    onDirtyChange(next !== null);
  }

  function refresh() {
    void queryClient.invalidateQueries({ queryKey: ['providers'] });
    void queryClient.invalidateQueries({ queryKey: ['models'] });
  }

  const probe = useMutation({
    mutationFn: (id: string) => checkProvider(id),
    onSuccess: (result) => setChecks((prev) => ({ ...prev, [result.id]: result })),
  });

  const remove = useMutation({
    mutationFn: (payload: { id: string; reassign: string }) =>
      deleteProvider(payload.id, version, payload.reassign || undefined),
    onSuccess: (_result, payload) => {
      setMessage(`已删除网关 ${payload.id}`);
      setDeleting(null);
      refresh();
    },
    onError: (error: Error) => setMessage(`删除失败：${error.message}`),
  });

  async function edit(id: string) {
    try {
      markEditing({ draft: await fetchProviderDraft(id), isNew: false });
      setMessage('');
    } catch {
      setMessage('读取该网关的配置失败：文件可能刚被别处改动。');
    }
  }

  return (
    <section className="prov">
      <header className="prov-head">
        <div>
          <h2 className="prov-title">网关</h2>
          <p className="prov-sub">
            模型从这些网关取。保存后按模板重排 <code>config/providers.yaml</code>，
            手写注释会丢失；密钥只写变量名。
          </p>
        </div>
        <button
          type="button"
          className="primary-btn"
          disabled={editing !== null || !version}
          onClick={() => markEditing({ draft: { ...BLANK }, isNew: true })}
        >
          新增网关
        </button>
      </header>

      {message && <p className="cfg-notice">{message}</p>}
      {providers.isError && (
        <p className="cfg-warn">
          读取网关清单失败：<code>providers.yaml</code> 可能有语法或取值错误。
        </p>
      )}

      {editing && (
        <ProviderForm
          initial={editing.draft}
          isNew={editing.isNew}
          version={version}
          kinds={kinds}
          onCancel={() => markEditing(null)}
          onSaved={(text) => {
            setMessage(text);
            markEditing(null);
            refresh();
          }}
        />
      )}

      <ul className="prov-list">
        {(data?.items ?? []).map((item) => {
          const isDefault = item.id === data?.default;
          const check = checks[item.id];
          return (
            <li className="prov-row" key={item.id}>
              <div className="prov-row-main">
                <span className="prov-name">
                  {item.label}
                  {isDefault && <span className="prov-badge is-default">默认</span>}
                  {item.has_key ? (
                    <span className="prov-badge is-ok">已配 key</span>
                  ) : (
                    <span className="prov-badge is-warn">缺 key</span>
                  )}
                </span>
                <span className="prov-meta mono">
                  {item.id} · {item.kind} · {item.base_url_host || '未填地址'} · {item.discover}
                </span>
                <span className="prov-meta">
                  {item.models > 0 ? `${item.models} 个模型` : '还没有模型列表'}
                  {item.last_check ? ` · 上次 ${codeText(item.last_check.code)}` : ''}
                  {check
                    ? ` · 探活 ${codeText(check.code)} ${check.latency_ms}ms / ${check.total} 个`
                    : ''}
                </span>
              </div>
              <div className="prov-row-actions">
                <button
                  type="button"
                  className="ghost-btn"
                  disabled={probe.isPending}
                  onClick={() => probe.mutate(item.id)}
                >
                  探活
                </button>
                <button
                  type="button"
                  className="ghost-btn"
                  disabled={editing !== null}
                  onClick={() => void edit(item.id)}
                >
                  编辑
                </button>
                <button
                  type="button"
                  className="ghost-btn"
                  disabled={editing !== null}
                  onClick={() =>
                    setDeleting(
                      isDefault
                        ? {
                            id: item.id,
                            reassign:
                              data?.items.find((other) => other.id !== item.id)?.id ?? '',
                          }
                        : { id: item.id, reassign: '' },
                    )
                  }
                >
                  删除
                </button>
              </div>
              {deleting?.id === item.id && (
                <div className="prov-delete">
                  {isDefault ? (
                    <>
                      <span>这是默认网关，删除后默认项交给：</span>
                      <select
                        className="mc-input"
                        aria-label="接任的默认网关"
                        value={deleting.reassign}
                        onChange={(event) =>
                          setDeleting({ id: item.id, reassign: event.target.value })
                        }
                      >
                        {(data?.items ?? [])
                          .filter((other) => other.id !== item.id)
                          .map((other) => (
                            <option key={other.id} value={other.id}>
                              {other.label}
                            </option>
                          ))}
                      </select>
                    </>
                  ) : (
                    <span>确定删除 {item.id}？文件会立即重写。</span>
                  )}
                  <button
                    type="button"
                    className="ghost-btn"
                    onClick={() => setDeleting(null)}
                  >
                    取消
                  </button>
                  <button
                    type="button"
                    className="ghost-btn is-danger"
                    disabled={remove.isPending || (isDefault && !deleting.reassign)}
                    onClick={() => remove.mutate({ id: item.id, reassign: deleting.reassign })}
                  >
                    确认删除
                  </button>
                </div>
              )}
            </li>
          );
        })}
        {(data?.items ?? []).length === 0 && (
          <li className="prov-empty">
            还没有配置网关：当前走匿名 provider（地址与 key 来自环境变量）。
          </li>
        )}
      </ul>

      <p className="prov-foot">
        协议与凭据由 <code>config/providers.yaml</code> 决定；保存后本进程立即读到新表，
        运行中的会话在下次装配时使用新网关。
      </p>
    </section>
  );
}
