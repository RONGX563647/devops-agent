/**
 * 工作区管理页（SPEC-0020 §10.2 的最小实现）：列出、创建、归档、删除。
 *
 * 表单遵循原生约束：`required` / `maxlength` / `type="url"` 交给浏览器，
 * 提交失败时把焦点留在首个非法字段；服务端错误经 aria-live 播报。
 */

import { useState, type FormEvent } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  archiveWorkspace,
  browseWorkspaceDirs,
  createWorkspace,
  deleteWorkspace,
  fetchWorkspaces,
} from '../api/client';
import type { CreateWorkspaceRequest, WorkspaceSummary } from '../types';

type SourceKind = 'local' | 'git';

export function Workspaces() {
  const [kind, setKind] = useState<SourceKind>('local');
  const [name, setName] = useState('');
  const [path, setPath] = useState('');
  const [url, setUrl] = useState('');
  const [branch, setBranch] = useState('');
  const [credential, setCredential] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [browseHint, setBrowseHint] = useState('');

  const workspaces = useQuery({
    queryKey: ['workspaces'],
    queryFn: fetchWorkspaces,
    refetchInterval: 10_000,
    retry: 0,
  });

  async function onBrowse() {
    setBrowseHint('');
    try {
      const result = await browseWorkspaceDirs(path.trim());
      setBrowseHint(
        result.items.length
          ? `可选目录：${result.items.map((item) => item.path).join('、')}`
          : `${result.root} 下没有子目录`,
      );
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '目录浏览失败');
    }
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError('');
    setBusy(true);
    const request: CreateWorkspaceRequest = {
      name: name.trim(),
      source:
        kind === 'local'
          ? { kind: 'local', path: path.trim() }
          : {
              kind: 'git',
              url: url.trim(),
              branch: branch.trim() || undefined,
              credential_ref: credential.trim() || undefined,
            },
    };
    try {
      await createWorkspace(request);
      setName('');
      setPath('');
      setUrl('');
      setBranch('');
      setCredential('');
      await workspaces.refetch();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '创建失败');
    } finally {
      setBusy(false);
    }
  }

  async function onArchive(id: string) {
    setError('');
    try {
      await archiveWorkspace(id);
      await workspaces.refetch();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '归档失败');
    }
  }

  async function onDelete(id: string) {
    setError('');
    try {
      await deleteWorkspace(id, true);
      await workspaces.refetch();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '删除失败');
    }
  }

  return (
    <div className="page">
      <h1>工作区</h1>
      <p className="subtitle">一个工作区对应一个代码库；同一工作区共享长期记忆，会话上下文仍各自隔离。</p>

      <form aria-labelledby="create-workspace-title" onSubmit={onSubmit}>
        <h2 id="create-workspace-title">新建工作区</h2>
        <p>
          <label htmlFor="ws-name">名称</label>
          <input
            id="ws-name"
            name="name"
            required
            maxLength={120}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </p>

        <fieldset>
          <legend>来源</legend>
          <label>
            <input
              type="radio"
              name="source"
              value="local"
              checked={kind === 'local'}
              onChange={() => setKind('local')}
            />
            引用本地目录（原地引用，删除工作区不会删除目录）
          </label>
          <label>
            <input
              type="radio"
              name="source"
              value="git"
              checked={kind === 'git'}
              onChange={() => setKind('git')}
            />
            从 Git 克隆到受管目录
          </label>
        </fieldset>

        {kind === 'local' ? (
          <p>
            <label htmlFor="ws-path">本地目录</label>
            <input
              id="ws-path"
              name="path"
              required
              autoComplete="off"
              spellCheck={false}
              value={path}
              onChange={(e) => setPath(e.target.value)}
              aria-describedby="ws-path-hint"
            />
            <span id="ws-path-hint">必须是部署白名单内的目录。</span>
            <button type="button" onClick={onBrowse}>
              浏览子目录
            </button>
          </p>
        ) : (
          <>
            <p>
              <label htmlFor="ws-url">Git 远端</label>
              <input
                id="ws-url"
                name="url"
                type="url"
                required
                placeholder="https://example.com/repo.git"
                spellCheck={false}
                value={url}
                onChange={(e) => setUrl(e.target.value)}
              />
            </p>
            <p>
              <label htmlFor="ws-branch">分支（可选）</label>
              <input
                id="ws-branch"
                name="branch"
                spellCheck={false}
                value={branch}
                onChange={(e) => setBranch(e.target.value)}
              />
            </p>
            <p>
              <label htmlFor="ws-credential">凭据名（可选）</label>
              <input
                id="ws-credential"
                name="credential_ref"
                autoComplete="off"
                spellCheck={false}
                value={credential}
                onChange={(e) => setCredential(e.target.value)}
              />
            </p>
          </>
        )}

        <button type="submit" disabled={busy}>
          {busy ? '创建中…' : '创建工作区'}
        </button>
        <p role="status" aria-live="polite">
          {browseHint}
        </p>
        {error ? (
          <p role="alert" aria-live="assertive">
            {error}
          </p>
        ) : null}
      </form>

      <h2>已登记工作区</h2>
      {workspaces.isLoading ? <p>加载中…</p> : null}
      {workspaces.isError ? <p role="alert">加载失败，请确认部署已开启工作区功能。</p> : null}
      <table>
        <thead>
          <tr>
            <th scope="col">名称</th>
            <th scope="col">状态</th>
            <th scope="col">来源</th>
            <th scope="col">目录</th>
            <th scope="col">会话 / 长期记忆</th>
            <th scope="col">操作</th>
          </tr>
        </thead>
        <tbody>
          {(workspaces.data?.items ?? []).map((item: WorkspaceSummary) => (
            <tr key={item.id}>
              <td>{item.name}</td>
              <td>{item.status === 'active' ? '活跃' : '已归档'}</td>
              <td>
                {item.source || '—'}
                {item.managed ? '（受管克隆）' : '（本地引用）'}
              </td>
              <td>
                <code>{item.root_path}</code>
              </td>
              <td>
                {item.session_count ?? '—'} / {item.long_term_count ?? '—'}
              </td>
              <td>
                {item.status === 'active' ? (
                  <button type="button" onClick={() => onArchive(item.id)}>
                    归档
                  </button>
                ) : null}{' '}
                <button type="button" onClick={() => onDelete(item.id)}>
                  删除
                </button>
              </td>
            </tr>
          ))}
          {workspaces.data && workspaces.data.items.length === 0 ? (
            <tr>
              <td colSpan={6}>还没有工作区。</td>
            </tr>
          ) : null}
        </tbody>
      </table>
    </div>
  );
}
