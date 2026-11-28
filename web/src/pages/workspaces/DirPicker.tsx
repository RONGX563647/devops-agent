/** 目录选择器：在白名单内逐级浏览，选中「当前这一层」。
 *
 * 为什么不是浏览器原生目录选择：网页拿不到宿主绝对路径（`webkitdirectory` 只给相对名），
 * 而工作区登记需要绝对路径。所以目录树由后端 `browse` 提供，白名单与上级边界也由它判定
 * —— 到白名单根就再也没有「上一级」按钮，不给必然 400 的操作。
 */

import { useEffect, useRef, useState } from 'react';
import { browseWorkspaceDirs } from '../../api/client';
import type { BrowseItem } from '../../types';
import { ChevronRightIcon, FolderIcon, UpIcon } from './icons';
import { errorText } from './meta';

interface Props {
  /** 首次进入的路径；空串回落到第一个白名单根。 */
  initial: string;
  /** 目录被选中（进入某一层）时同步给表单。 */
  onChange: (path: string) => void;
}

interface BrowseState {
  root: string;
  items: BrowseItem[];
  parent: string | null;
  roots: string[];
  loading: boolean;
  error: string;
}

export function DirPicker({ initial, onChange }: Props) {
  const [state, setState] = useState<BrowseState>({
    root: '',
    items: [],
    parent: null,
    roots: [],
    loading: true,
    error: '',
  });
  const [manual, setManual] = useState(false);
  const [draft, setDraft] = useState('');
  const initialRef = useRef(initial);
  const changeRef = useRef(onChange);
  // 请求序号：快速连点目录时只认最后一次响应，避免旧结果覆盖新目录。
  const seq = useRef(0);

  useEffect(() => {
    changeRef.current = onChange;
  }, [onChange]);

  useEffect(() => {
    let alive = true;
    browseWorkspaceDirs(initialRef.current)
      .then((result) => {
        if (!alive) return;
        const root = result.root;
        setState({
          root,
          items: result.items,
          parent: result.parent ?? null,
          roots: result.roots ?? [],
          loading: false,
          error: '',
        });
        changeRef.current(root);
      })
      .catch((exc: unknown) => {
        if (!alive) return;
        setState((prev) => ({ ...prev, loading: false, error: errorText(exc) }));
      });
    return () => {
      alive = false;
    };
  }, []);

  async function go(path: string) {
    const mine = ++seq.current;
    setState((prev) => ({ ...prev, loading: true, error: '' }));
    try {
      const result = await browseWorkspaceDirs(path);
      if (mine !== seq.current) return;
      setState({
        root: result.root,
        items: result.items,
        parent: result.parent ?? null,
        roots: result.roots ?? [],
        loading: false,
        error: '',
      });
      changeRef.current(result.root);
    } catch (exc) {
      if (mine !== seq.current) return;
      setState((prev) => ({ ...prev, loading: false, error: errorText(exc) }));
    }
  }

  const selected = state.root;

  return (
    <div className="wsp-picker">
      <div className="wsp-picker-bar">
        <button
          type="button"
          className="wsp-icon-btn"
          disabled={!state.parent}
          onClick={() => state.parent && void go(state.parent)}
          title={state.parent ? '上一级' : '已到白名单根'}
        >
          <UpIcon />
          <span className="sr-only">上一级</span>
        </button>
        <code className="wsp-picker-path" title={selected}>
          {selected || '加载中…'}
        </code>
        <button
          type="button"
          className="wsp-btn is-sm is-quiet"
          onClick={() => setManual((v) => !v)}
        >
          {manual ? '收起手填' : '手填路径'}
        </button>
      </div>

      {manual ? (
        <form
          className="wsp-picker-manual"
          onSubmit={(event) => {
            event.preventDefault();
            if (draft.trim()) void go(draft.trim());
          }}
        >
          <input
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="粘贴白名单内的绝对路径"
            spellCheck={false}
            aria-label="手动输入目录路径"
          />
          <button type="submit" className="wsp-btn is-sm" disabled={!draft.trim()}>
            跳转
          </button>
        </form>
      ) : null}

      {state.roots.length > 1 ? (
        <div className="wsp-roots">
          <span>白名单</span>
          {state.roots.map((root) => (
            <button
              key={root}
              type="button"
              className="wsp-root-chip"
              onClick={() => void go(root)}
              title={root}
            >
              {root}
            </button>
          ))}
        </div>
      ) : null}

      <ul className="wsp-picker-list" aria-label="子目录">
        {state.items.map((item) => (
          <li key={item.path}>
            <button type="button" className="wsp-picker-row" onClick={() => void go(item.path)}>
              <FolderIcon size={15} />
              <span>{item.name}</span>
              <ChevronRightIcon />
            </button>
          </li>
        ))}
        {!state.loading && !state.error && state.items.length === 0 ? (
          <li className="wsp-picker-empty">这一层没有子目录，可以直接选用当前目录。</li>
        ) : null}
        {state.loading ? <li className="wsp-picker-empty">读取目录…</li> : null}
        {state.error ? (
          <li className="wsp-picker-empty is-error" role="alert">
            {state.error}
          </li>
        ) : null}
      </ul>
    </div>
  );
}
