/** 密钥 tab：列出保险箱里的凭据（只有元信息），并支持删除。
 *
 * 界面刻意不提供「查看明文」入口：接口层就没有读回明文的通道，
 * 这里再给一个按钮只会让人误以为能看。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { deleteSecret, fetchSecrets } from '../api/client';
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
  return (
    <tr>
      <td className="mono">{item.name}</td>
      <td className="mono">{item.hint}</td>
      <td>{item.source}</td>
      <td>{item.uses}</td>
      <td className="mono">{item.last_used_at ? formatDateTime(item.last_used_at) : '—'}</td>
      <td>
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
        对话里出现的 API Key / Token 会被识别、加密后存进这里，正文里只留{' '}
        <code className="mono">{'{{secret:名字}}'}</code> 占位符。模型始终看不到明文；
        只有在工具真正执行的那一刻，占位符才会在工具边界被换成真值，工具输出里的真值
        还会被抹回占位符。删除后遗留的占位符会原样透传，不再被注入。
      </p>
      {secrets.isError && <p className="error-text">读取失败：密码库不可用。</p>}
      {!secrets.isError && items.length === 0 && (
        <p className="muted">
          保险箱是空的。粘贴一段带密钥的配置发出来，下一轮就会出现在这里。
        </p>
      )}
      {items.length > 0 && (
        <table className="data-table secrets-table">
          <thead>
            <tr>
              <th>名字</th>
              <th>提示</th>
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
