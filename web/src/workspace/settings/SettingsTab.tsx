/**
 * 设置 tab：配置中心的索引 + 就地编辑（ADR-0005）。
 *
 * 为什么不做成一个大表单：键分散在 health / input / memory / gateway 四个域，
 * 每组有自己的边界与生效时机。左侧列域、右侧一次只渲染一个域，比把 60 多行
 * 平铺好读；「值从哪来」用来源徽标直接画在行上，不用点开才知道是 yaml 还是库。
 *
 * 写入只发改动项（`null` = 删掉覆盖回落到下层），主按钮是保存，恢复默认单独放：
 * 保存和「清空全部」共用一个按钮，是最容易误操作的设计。
 */
import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  exportConfigYaml,
  fetchConfigDomain,
  fetchConfigIndex,
  patchConfigDomain,
  resetConfigDomain,
} from '../../api/client';
import type {
  ConfigBound,
  ConfigDomain,
  ConfigDomainSummary,
  ConfigHot,
  ConfigSource,
} from '../../types';

const HOT_LABEL: Record<ConfigHot, string> = {
  immediate: '改完即生效',
  next_turn: '下一轮生效',
  restart: '重启进程后生效',
};

const SOURCE_LABEL: Record<ConfigSource, string> = {
  env: '环境变量',
  db: '配置库',
  yaml: 'yaml 基线',
  default: '代码默认',
};

/** 点号路径取值：`values.weights.execution`。 */
function atPath(root: Record<string, unknown>, path: string): unknown {
  return path.split('.').reduce<unknown>((node, part) => {
    if (node && typeof node === 'object' && part in (node as Record<string, unknown>)) {
      return (node as Record<string, unknown>)[part];
    }
    return undefined;
  }, root);
}

/** 分组：按第一段路径聚在一起，`weights.execution` 归到 `weights`。 */
function groupOf(path: string): string {
  return path.includes('.') ? path.split('.')[0] : '通用';
}

function leafOf(path: string): string {
  const parts = path.split('.');
  return parts[parts.length - 1];
}

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

function boundHint(bound: ConfigBound | undefined): string {
  if (!bound) return '';
  const parts: string[] = [];
  if (bound.min !== undefined) parts.push(`≥ ${bound.min}`);
  if (bound.min_exclusive !== undefined) parts.push(`> ${bound.min_exclusive}`);
  if (bound.max !== undefined) parts.push(`≤ ${bound.max}`);
  if (bound.max_exclusive !== undefined) parts.push(`< ${bound.max_exclusive}`);
  return parts.join(' · ');
}

/** 一行：标签 + 控件 + 来源 + 重置。只读域或被 env 锁定的键不给输入框。 */
function FieldRow({
  path,
  value,
  source,
  bound,
  locked,
  dirty,
  onChange,
  onReset,
}: {
  path: string;
  value: unknown;
  source: ConfigSource;
  bound?: ConfigBound;
  locked: boolean;
  dirty: boolean;
  onChange: (path: string, next: unknown) => void;
  onReset: (path: string) => void;
}) {
  const label = leafOf(path);
  const hint = boundHint(bound);
  const disabled = locked;

  let control;
  if (bound?.options) {
    control = (
      <select
        aria-label={path}
        className="mc-input"
        disabled={disabled}
        value={String(value ?? '')}
        onChange={(e) => onChange(path, e.target.value)}
      >
        {bound.options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    );
  } else if (typeof value === 'boolean') {
    control = (
      <input
        aria-label={path}
        type="checkbox"
        checked={Boolean(value)}
        disabled={disabled}
        onChange={(e) => onChange(path, e.target.checked)}
      />
    );
  } else if (Array.isArray(value)) {
    control = (
      <input
        aria-label={path}
        className="mc-input"
        disabled={disabled}
        value={value.join(', ')}
        onChange={(e) =>
          onChange(
            path,
            e.target.value
              .split(',')
              .map((item) => item.trim())
              .filter(Boolean)
              .map((item) => (/^\d+$/.test(item) ? Number(item) : item)),
          )
        }
      />
    );
  } else {
    control = (
      <input
        aria-label={path}
        className="mc-input"
        type={typeof value === 'number' ? 'number' : 'text'}
        disabled={disabled}
        value={value === undefined || value === null ? '' : String(value)}
        onChange={(e) => onChange(path, typeof value === 'number' ? Number(e.target.value) : e.target.value)}
      />
    );
  }

  return (
    <div className={`cfg-row${dirty ? ' is-dirty' : ''}`}>
      <div className="cfg-row-head">
        <span className="cfg-row-label" title={path}>
          {label}
        </span>
        <span className="cfg-row-control">{control}</span>
        <span className={`mc-src is-${source}`}>{SOURCE_LABEL[source]}</span>
        <button
          type="button"
          className="cfg-reset"
          aria-label={`重置 ${path}`}
          title="恢复默认（删掉这层的覆盖）"
          disabled={!locked && source === 'default' && !dirty}
          onClick={() => onReset(path)}
        >
          ⟲
        </button>
      </div>
      {hint && <span className="cfg-row-hint">{hint}</span>}
    </div>
  );
}

/** 域清单：左侧树，标出覆盖条数与是否只读。 */
function DomainList({
  domains,
  selected,
  onSelect,
}: {
  domains: ConfigDomainSummary[];
  selected: string;
  onSelect: (domain: string) => void;
}) {
  return (
    <ul className="cfg-tree">
      {domains.map((item) => (
        <li key={item.domain}>
          <button
            type="button"
            className={`cfg-tree-item${item.domain === selected ? ' is-active' : ''}`}
            onClick={() => onSelect(item.domain)}
          >
            <span className="cfg-tree-title">{item.title}</span>
            <span className="cfg-tree-meta">
              {item.writable ? HOT_LABEL[item.hot] : item.managed_by ? '去对应位置改' : '只读'}
              {item.overrides > 0 ? ` · 覆盖 ${item.overrides}` : ''}
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}

/** 域编辑器：分组行 + 底部保存 / 恢复全部。 */
function DomainEditor({ data }: { data: ConfigDomain }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [notice, setNotice] = useState<string | null>(null);

  const dirtyKeys = useMemo(
    () => Object.keys(draft).filter((key) => !same(draft[key], atPath(data.values, key))),
    [draft, data.values],
  );

  const save = useMutation({
    mutationFn: () => {
      const patch: Record<string, unknown> = {};
      for (const key of dirtyKeys) patch[key] = draft[key];
      return patchConfigDomain(data.domain, patch, data.version);
    },
    onSuccess: () => {
      setDraft({});
      setNotice('已保存到配置库');
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
    const map = new Map<string, string[]>();
    for (const path of Object.keys(data.sources)) {
      const group = groupOf(path);
      map.set(group, [...(map.get(group) ?? []), path]);
    }
    return [...map.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [data.sources]);

  const locked = new Set(data.env_locked);
  const overrides = Object.values(data.sources).filter((item) => item === 'db').length;
  const editable = data.writable ? data.note : `本域只读：${data.note}`;

  return (
    <section className="cfg-editor">
      <header className="cfg-editor-head">
        <div>
          <h2 className="cfg-editor-title">{data.title}</h2>
          <p className="cfg-editor-note">{editable}</p>
        </div>
        <span className={`cfg-hot is-${data.hot}`}>{HOT_LABEL[data.hot]}</span>
      </header>

      {data.drifted.length > 0 && (
        <p className="cfg-warn">
          代码默认值已升级，但仍有覆盖压在上面：<code className="mono">{data.drifted.join('、')}</code>。
          确认后点右侧 ⟲ 恢复默认。
        </p>
      )}

      <div className="cfg-groups">
        {groups.map(([group, paths]) => (
          <div className="cfg-group" key={group}>
            <h3 className="cfg-group-title">{group}</h3>
            {paths.map((path) => (
              <FieldRow
                key={path}
                path={path}
                value={atPath(data.values, path)}
                source={data.sources[path] ?? 'default'}
                bound={data.bounds[path]}
                locked={locked.has(path)}
                dirty={dirtyKeys.includes(path)}
                onChange={(key, next) => setDraft((prev) => ({ ...prev, [key]: next }))}
                onReset={(key) => setDraft((prev) => ({ ...prev, [key]: null }))}
              />
            ))}
          </div>
        ))}
      </div>

      <footer className="cfg-foot">
        {notice && <p className="cfg-notice">{notice}</p>}
        <div className="cfg-actions">
          <span className="cfg-count">{dirtyKeys.length > 0 ? `未保存 ${dirtyKeys.length} 项` : '没有改动'}</span>
          <button type="button" className="ghost-btn" onClick={() => setDraft({})} disabled={!dirtyKeys.length}>
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
            disabled={!data.writable || overrides === 0}
          >
            恢复全部
          </button>
          <button
            type="button"
            className="primary-btn"
            onClick={() => save.mutate()}
            disabled={!data.writable || dirtyKeys.length === 0 || save.isPending}
          >
            保存
          </button>
        </div>
      </footer>
    </section>
  );
}

export function SettingsTab({ initialDomain }: { initialDomain?: string | null }) {
  const index = useQuery({ queryKey: ['config', 'index'], queryFn: fetchConfigIndex });
  const domains = index.data?.domains ?? [];
  // 初始域只取一次：URL 变化由父级用 key 重挂（见 Workspace），
  // 在 effect 里 setState 会触发级联渲染，也会把用户刚选的域打回去。
  const [selected, setSelected] = useState<string>(initialDomain ?? 'health');

  const domain = useQuery({
    queryKey: ['config', 'domain', selected],
    queryFn: () => fetchConfigDomain(selected),
    enabled: Boolean(selected),
  });

  return (
    <div className="panel cfg-panel">
      <div className="cfg-header">
        <div>
          <h2>配置中心</h2>
          <p className="hint">
            部署策略集中存在配置库里（只存与基线不同的键），yaml 仍是可评审的仓库基线。
            环境变量注入的键在这里锁定 —— 写了也赢不过容器。
          </p>
        </div>
        {index.data && (
          <p className="cfg-storage mono">
            {index.data.mode === 'file' ? 'file 模式（不读写配置库）' : index.data.db_path}
          </p>
        )}
      </div>

      {index.isError && <p className="error-text">读取配置域失败：后端可能还是旧版本。</p>}
      <div className="cfg-body">
        <DomainList domains={domains} selected={selected} onSelect={setSelected} />
        <div className="cfg-main">
          {domain.isError && <p className="error-text">读取 {selected} 失败。</p>}
          {domain.data && (
            // key 带上版本：换域或别人改过之后重挂，草稿不会跨版本残留
            <DomainEditor key={`${domain.data.domain}:${domain.data.version}`} data={domain.data} />
          )}
        </div>
      </div>
    </div>
  );
}
