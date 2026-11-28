/**
 * 运行策略里的一个域（SPEC-0022 §4.2 / §9）。
 *
 * 相对旧实现的四处改动，都是针对「139 行同构输入框」这个根因：
 * 1. 分组可折叠（原生 `<details>`），默认只展开前两组 —— 记忆域 39 个键不再一次铺满。
 * 2. 组内搜索：按路径与中文名过滤，找不到时明确说「没有匹配」。
 * 3. 标签用中文名 + 原键名（见 `ConfigField`），来源用文字徽标。
 * 4. 操作条 sticky 在底部，改完不用滚到表单末尾找保存。
 */
import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  exportConfigYaml,
  fetchConfigDomain,
  patchConfigDomain,
  resetConfigDomain,
} from '../../api/client';
import type { ConfigBound, ConfigDomain, ConfigSource } from '../../types';
import { ConfigField } from './ConfigField';
import { docFor } from './docs';

const HOT_LABEL = {
  immediate: '改完即生效',
  next_turn: '下一轮生效',
  restart: '重启进程后生效',
} as const;

/** 分组名的中文：yaml 段名直接当标题仍然是「读列名」。 */
const GROUP_TITLES: Record<string, string> = {
  '': '常规',
  weights: '权重',
  efficiency_split: '效率拆分',
  source_weights: '来源权重',
  short_term: '短期记忆',
  episodic: '中期记忆',
  long_term: '长期记忆',
  budget: '预算',
  capture: '凭据捕获',
  redaction: '脱敏',
  stream: '流式',
  sessions: '会话',
  confirm: '确认协议',
  rules: '规则',
  risk: '风险词',
  docker: 'Docker',
  opensandbox: 'OpenSandbox',
  tokens: 'Token 口径',
};

function atPath(root: Record<string, unknown>, path: string): unknown {
  return path.split('.').reduce<unknown>((node, part) => {
    if (node && typeof node === 'object' && part in (node as Record<string, unknown>)) {
      return (node as Record<string, unknown>)[part];
    }
    return undefined;
  }, root);
}

function groupOf(path: string): string {
  return path.includes('.') ? path.split('.')[0] : '';
}

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

interface Props {
  domain: string;
  onDirtyChange: (dirty: boolean) => void;
}

function EditorBody({ data, onDirtyChange }: { data: ConfigDomain; onDirtyChange: Props['onDirtyChange'] }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [notice, setNotice] = useState('');
  const [query, setQuery] = useState('');

  const dirtyKeys = useMemo(
    () => Object.keys(draft).filter((key) => !same(draft[key], atPath(data.values, key))),
    [draft, data.values],
  );

  useEffect(() => onDirtyChange(dirtyKeys.length > 0), [dirtyKeys.length, onDirtyChange]);

  const save = useMutation({
    mutationFn: () => {
      const patch: Record<string, unknown> = {};
      for (const key of dirtyKeys) patch[key] = draft[key];
      return patchConfigDomain(data.domain, patch, data.version);
    },
    onSuccess: (result) => {
      setDraft({});
      setNotice(result.persisted === false ? '与基线相同，没有写入（值未被改动）' : '已保存');
      void queryClient.invalidateQueries({ queryKey: ['config'] });
    },
    onError: (error: Error) => setNotice(`保存失败：${error.message}`),
  });

  const resetAll = useMutation({
    mutationFn: () => resetConfigDomain(data.domain, { all: true }),
    onSuccess: () => {
      setDraft({});
      setNotice('已恢复为基线值');
      void queryClient.invalidateQueries({ queryKey: ['config'] });
    },
    onError: (error: Error) => setNotice(`恢复失败：${error.message}`),
  });

  const exportYaml = useMutation({
    mutationFn: () => exportConfigYaml(data.domain),
    onSuccess: (text) => setNotice(`已导出：\n${text.slice(0, 400)}`),
    onError: (error: Error) => setNotice(`导出失败：${error.message}`),
  });

  const groups = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const map = new Map<string, string[]>();
    for (const path of Object.keys(data.sources)) {
      const doc = docFor(`${data.domain}.${path}`);
      if (needle && !path.toLowerCase().includes(needle) && !doc.title.toLowerCase().includes(needle)) {
        continue;
      }
      const group = groupOf(path);
      map.set(group, [...(map.get(group) ?? []), path]);
    }
    return [...map.entries()].sort(([a], [b]) => (a === '' ? -1 : a.localeCompare(b)));
  }, [data.sources, data.domain, query]);

  const locked = new Set(data.env_locked);
  const overrides = Object.values(data.sources).filter((item) => item === 'db').length;
  const readonly = !data.writable;

  return (
    <section className="cfg-editor">
      <header className="cfg-editor-head">
        <div>
          <h2 className="cfg-editor-title">{data.title}</h2>
          <p className="cfg-editor-note">
            {readonly ? `本域只读：${data.note}` : data.note}
          </p>
        </div>
        <span className={`cfg-hot is-${data.hot}`}>{HOT_LABEL[data.hot]}</span>
      </header>

      {data.drifted.length > 0 && (
        <p className="cfg-warn">
          代码默认值已升级，但仍有覆盖压在上面：
          <code className="mono">{data.drifted.join('、')}</code>。确认后点右侧 ⟲ 恢复默认。
        </p>
      )}

      <div className="cfg-toolbar">
        <input
          className="mc-input cfg-search"
          aria-label="搜索参数"
          placeholder="搜索参数名或说明…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <span className="cfg-count">{Object.keys(data.sources).length} 个参数</span>
      </div>

      <div className="cfg-groups">
        {groups.length === 0 && <p className="cfg-empty">没有匹配的参数。</p>}
        {groups.map(([group, paths], index) => (
          <details className="cfg-group" key={group || 'default'} open={index < 2 || Boolean(query)}>
            <summary className="cfg-group-title">
              {GROUP_TITLES[group] ?? group}
              <span className="cfg-group-count">{paths.length}</span>
            </summary>
            {paths.map((path) => (
              <ConfigField
                key={path}
                path={path}
                docKey={`${data.domain}.${path}`}
                value={atPath(data.values, path)}
                source={(data.sources[path] ?? 'default') as ConfigSource}
                bound={data.bounds[path] as ConfigBound | undefined}
                locked={locked.has(path)}
                readonly={readonly}
                dirty={dirtyKeys.includes(path)}
                onChange={(key, next) => setDraft((prev) => ({ ...prev, [key]: next }))}
                onReset={(key) => setDraft((prev) => ({ ...prev, [key]: null }))}
              />
            ))}
          </details>
        ))}
      </div>

      <footer className="cfg-foot">
        {notice && <p className="cfg-notice">{notice}</p>}
        <div className="cfg-actions">
          <span className="cfg-count">
            {dirtyKeys.length > 0 ? `未保存 ${dirtyKeys.length} 项` : '没有改动'}
          </span>
          <button
            type="button"
            className="ghost-btn"
            onClick={() => setDraft({})}
            disabled={!dirtyKeys.length}
          >
            放弃
          </button>
          <button
            type="button"
            className="ghost-btn"
            onClick={() => exportYaml.mutate()}
            disabled={overrides === 0}
          >
            导出 yaml
          </button>
          <button
            type="button"
            className="ghost-btn"
            onClick={() => resetAll.mutate()}
            disabled={readonly || overrides === 0}
          >
            恢复全部
          </button>
          <button
            type="button"
            className="primary-btn"
            onClick={() => save.mutate()}
            disabled={readonly || dirtyKeys.length === 0 || save.isPending}
          >
            保存
          </button>
        </div>
      </footer>
    </section>
  );
}

export function DomainEditor({ domain, onDirtyChange }: Props) {
  const queryClient = useQueryClient();
  const { data, isError, refetch, isFetching } = useQuery({
    queryKey: ['config', 'domain', domain],
    queryFn: () => fetchConfigDomain(domain),
  });

  if (isError) {
    return (
      <div className="cfg-error" role="alert">
        <p>读取「{domain}」失败。</p>
        <button
          type="button"
          className="ghost-btn"
          disabled={isFetching}
          onClick={() => {
            void queryClient.invalidateQueries({ queryKey: ['config', 'domain', domain] });
            void refetch();
          }}
        >
          {isFetching ? '重试中…' : '重试'}
        </button>
      </div>
    );
  }
  if (!data) {
    // 骨架屏：整块空白会被读成「这个域没有参数」
    return (
      <div className="cfg-skeleton" aria-busy="true" aria-label="正在读取配置">
        <span /><span /><span /><span />
      </div>
    );
  }
  // key 带版本：换域或别人改过之后重挂，草稿不会跨版本残留
  return (
    <EditorBody key={`${data.domain}:${data.version}`} data={data} onDirtyChange={onDirtyChange} />
  );
}
