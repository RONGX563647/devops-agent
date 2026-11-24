/** 密钥 tab：列出保险箱里的凭据、受控查看明文、删除。
 *
 * 明文读回走单独端点且每次记 uses 留痕：工具输出捕获的凭据若只进不回，
 * 用户就永远看不到工具读到了什么；但读回是显式点击触发，不会随页面加载泄露。
 */
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { deleteSecret, fetchSecretValue, fetchSecrets } from '../api/client';
import type { SecretMeta } from '../types';
import { formatDateTime } from './time';

function SecretRow({
  item,
  onRemove,
  disabled,
}: {
  item: SecretMeta;
  onRemove: (name: string) => void;
  disabled: boolean;
}) {
  // 明文只存在本行的局部 state：切走 tab / 卸载即消失，不留全局缓存
  const [value, setValue] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const shown = value !== null;

  const toggle = async () => {
    if (shown) {
      setValue(null);
      return;
    }
    setLoading(true);
    try {
      const result = await fetchSecretValue(item.name);
      setValue(result.found ? result.value : '（已失效或解不开）');
    } catch {
      setValue('（读取失败）');
    } finally {
      setLoading(false);
    }
  };

  return (
    <tr>
      <td className="mono">{item.name}</td>
      <td className="mono">{shown ? <span className="secret-value mono">{value}</span> : item.hint}</td>
      <td>{item.source}</td>
      <td>{item.uses}</td>
      <td className="mono">{item.last_used_at ? formatDateTime(item.last_used_at) : '—'}</td>
      <td>
        <button className="ghost-btn" disabled={disabled || loading} onClick={() => void toggle()}>
          {shown ? '隐藏' : loading ? '读取…' : '查看'}
        </button>
        <button className="ghost-btn" disabled={disabled} onClick={() => onRemove(item.name)}>
          删除
        </button>
      </td>
    </tr>
  );
}

export function SecretsTab() {
  const queryClient = useQueryClient();
  const secrets = useQuery({ queryKey: ['secrets'], queryFn: fetchSecrets });
  const remove = useMutation({
    mutationFn: deleteSecret,
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['secrets'] }),
  });

  const items = secrets.data ?? [];
  return (
    <div className="panel secrets-panel">
      <h2>密钥保险箱</h2>
      <p className="hint">
        对话里出现的 API Key / Token，以及工具输出里读到的明文凭据，都会被识别、加密后存进这里，
        正文里只留 <code className="mono">{'{{secret:名字}}'}</code> 占位符。模型始终看不到明文；
        点「查看」可读取原文（每次读取都会记一次使用）。
        <code className="mono">source=output</code> 的条目来自工具输出捕获。
      </p>
      {secrets.isError && <p className="error-text">读取失败：密码库不可用。</p>}
      {!secrets.isError && items.length === 0 && (
        <p className="muted">
          保险箱是空的。粘贴一段带密钥的配置发出来，或让工具读到凭据，下一轮就会出现在这里。
        </p>
      )}
      {items.length > 0 && (
        <table className="data-table secrets-table">
          <thead>
            <tr>
              <th>名字</th>
              <th>提示 / 明文</th>
              <th>来源</th>
              <th>使用次数</th>
              <th>最近使用</th>
              <th aria-label="操作" />
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <SecretRow
                key={item.name}
                item={item}
                onRemove={(name) => remove.mutate(name)}
                disabled={remove.isPending}
              />
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
