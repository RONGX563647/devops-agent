/** 新建工作区对话框：两种来源并列，能力不足时直接禁用并说明原因。
 *
 * 用原生 `<dialog>`：焦点、Esc、背景 inert 交给浏览器；挂载即 `showModal()`，
 * 关闭由 `close` 事件回传（Esc 与「取消」走同一条路径）。
 * 表单挂载即新实例（父级按打开次数重挂），所以不需要「重置」逻辑。
 */

import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { createWorkspace } from '../../api/client';
import type {
  CreateWorkspaceRequest,
  LocalLanding,
  WorkspaceControl,
  WorkspaceSummary,
} from '../../types';
import { DirPicker } from './DirPicker';
import { FolderIcon, GitIcon } from './icons';
import { basename, errorText, repoName } from './meta';

const SETTINGS_LINK = '/?tab=settings&domain=workspace';

export type SourceKind = 'local' | 'git';

interface Props {
  initialKind: SourceKind;
  control: WorkspaceControl | undefined;
  onDismiss: () => void;
  onCreated: (item: WorkspaceSummary) => void;
}

export function NewWorkspaceDialog({ initialKind, control, onDismiss, onCreated }: Props) {
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const nameRef = useRef<HTMLInputElement | null>(null);
  const dismissRef = useRef(onDismiss);

  const [kind, setKind] = useState<SourceKind>(initialKind);
  const [name, setName] = useState('');
  const [nameTouched, setNameTouched] = useState(false);
  const [path, setPath] = useState('');
  // 默认工作树：用户选本地目录的本意是「让 Agent 在那个仓库里干活」，
  // 而不是「在我的检出上乱改」。
  const [landing, setLanding] = useState<LocalLanding>('worktree');
  const [localBranch, setLocalBranch] = useState('');
  const [url, setUrl] = useState('');
  const [branch, setBranch] = useState('');
  const [credential, setCredential] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const localBlocked = Boolean(
    control && (!control.allow_local_import || control.allowed_local_roots.length === 0),
  );
  const gitBlocked = Boolean(control && (!control.allow_git_import || control.git_available === false));

  useEffect(() => {
    dismissRef.current = onDismiss;
  }, [onDismiss]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return undefined;
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
    // showModal 会把焦点交给第一个可聚焦元素（这里是右上角关闭按钮），
    // 但用户打开对话框是为了填表，焦点应当直接落在名称上。
    nameRef.current?.focus();
    function onClose() {
      dismissRef.current();
    }
    dialog.addEventListener('close', onClose);
    return () => dialog.removeEventListener('close', onClose);
  }, []);

  function pickPath(next: string) {
    setPath(next);
    if (!nameTouched) setName(basename(next));
  }

  function changeUrl(next: string) {
    setUrl(next);
    if (!nameTouched) setName(repoName(next));
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError('');
    const clean = name.trim() || basename(path) || repoName(url);
    if (!clean) {
      setError('给工作区起个名字');
      return;
    }
    const request: CreateWorkspaceRequest =
      kind === 'local'
        ? {
            name: clean,
            source: {
              kind: 'local',
              path: path.trim(),
              mode: landing,
              branch: landing === 'worktree' ? localBranch.trim() || undefined : undefined,
            },
          }
        : {
            name: clean,
            source: {
              kind: 'git',
              url: url.trim(),
              branch: branch.trim() || undefined,
              credential_ref: credential.trim() || undefined,
            },
          };
    setBusy(true);
    try {
      const created = await createWorkspace(request);
      onCreated(created);
    } catch (exc) {
      setError(errorText(exc));
      setBusy(false);
    }
  }

  return (
    <dialog ref={dialogRef} className="wsp-dlg" aria-labelledby="wsp-dlg-title">
      <form className="wsp-dlg-form" onSubmit={(event) => void submit(event)}>
        <header className="wsp-dlg-head">
          <div>
            <h2 id="wsp-dlg-title">新建工作区</h2>
            <p>一个工作区对应一个代码库；同一工作区共享长期记忆，会话上下文仍各自隔离。</p>
          </div>
          <button
            type="button"
            className="wsp-icon-btn"
            onClick={() => dialogRef.current?.close()}
            aria-label="关闭"
          >
            ×
          </button>
        </header>

        <div className="wsp-dlg-body">
          <div className="wsp-field">
            <label htmlFor="wsp-name">名称</label>
            <input
              id="wsp-name"
              ref={nameRef}
              required
              maxLength={120}
              value={name}
              autoComplete="off"
              placeholder="例如 devops-agent"
              onChange={(event) => {
                setName(event.target.value);
                setNameTouched(true);
              }}
            />
            <p className="wsp-field-hint">列表与侧栏都显示这个名字；选目录或填 Git 地址后会自动带出。</p>
          </div>

          <fieldset className="wsp-kinds">
            <legend>来源</legend>
            <label
              className={`wsp-kind${kind === 'local' ? ' is-active' : ''}${
                localBlocked ? ' is-disabled' : ''
              }`}
            >
              <input
                type="radio"
                name="wsp-kind"
                className="sr-only"
                checked={kind === 'local'}
                disabled={localBlocked}
                onChange={() => setKind('local')}
              />
              <span className="wsp-kind-icon">
                <FolderIcon />
              </span>
              <span className="wsp-kind-text">
                <span className="wsp-kind-title">本地目录</span>
                <span className="wsp-kind-desc">在本地仓库上开工作树，主检出不动</span>
              </span>
            </label>
            <label
              className={`wsp-kind${kind === 'git' ? ' is-active' : ''}${gitBlocked ? ' is-disabled' : ''}`}
            >
              <input
                type="radio"
                name="wsp-kind"
                className="sr-only"
                checked={kind === 'git'}
                disabled={gitBlocked}
                onChange={() => setKind('git')}
              />
              <span className="wsp-kind-icon is-git">
                <GitIcon />
              </span>
              <span className="wsp-kind-text">
                <span className="wsp-kind-title">Git 仓库</span>
                <span className="wsp-kind-desc">克隆到受管目录，支持分支与凭据名</span>
              </span>
            </label>
          </fieldset>

          {kind === 'local' ? (
            <div className="wsp-field">
              <div className="wsp-field-head">
                <label htmlFor="wsp-path">目录</label>
                <span className="wsp-field-note">必须在白名单根内</span>
              </div>
              <input
                id="wsp-path"
                required
                value={path}
                autoComplete="off"
                spellCheck={false}
                placeholder={localBlocked ? '未配置白名单' : '白名单内的绝对路径'}
                onChange={(event) => pickPath(event.target.value)}
              />
              <div className="wsp-landing" role="radiogroup" aria-label="落盘方式">
                <label className={`wsp-landing-item${landing === 'worktree' ? ' is-active' : ''}`}>
                  <input
                    type="radio"
                    name="wsp-landing"
                    className="sr-only"
                    checked={landing === 'worktree'}
                    onChange={() => setLanding('worktree')}
                  />
                  <span className="wsp-landing-title">工作树</span>
                  <span className="wsp-landing-desc">
                    在受管目录新建一个 git worktree，Agent 用自己的分支改，你的检出不受影响
                  </span>
                </label>
                <label className={`wsp-landing-item${landing === 'in_place' ? ' is-active' : ''}`}>
                  <input
                    type="radio"
                    name="wsp-landing"
                    className="sr-only"
                    checked={landing === 'in_place'}
                    onChange={() => setLanding('in_place')}
                  />
                  <span className="wsp-landing-title">原地引用</span>
                  <span className="wsp-landing-desc">
                    直接在这个目录里跑：能看到你未提交的改动，也会改到它们
                  </span>
                </label>
              </div>
              {landing === 'worktree' ? (
                <div className="wsp-field">
                  <label htmlFor="wsp-local-branch">分支</label>
                  <input
                    id="wsp-local-branch"
                    value={localBranch}
                    spellCheck={false}
                    autoComplete="off"
                    placeholder="留空自动生成 agent/<名字>-<后缀>"
                    onChange={(event) => setLocalBranch(event.target.value)}
                  />
                  <p className="wsp-field-hint">
                    目录不是 git 仓库时开不了工作树，会提示改选「原地引用」。
                  </p>
                </div>
              ) : null}
              {localBlocked && control ? (
                <p className="wsp-notice">
                  {control.allow_local_import
                    ? '还没有配置本地目录白名单，本地导入暂不可用。'
                    : '部署关闭了本地目录导入（allow_local_import=false）。'}
                  <Link to={SETTINGS_LINK}>去「设置 · 工作区」添加</Link>
                </p>
              ) : (
                <DirPicker initial={path} onChange={pickPath} />
              )}
            </div>
          ) : (
            <>
              <div className="wsp-field">
                <label htmlFor="wsp-url">Git 地址</label>
                <input
                  id="wsp-url"
                  required
                  value={url}
                  spellCheck={false}
                  autoComplete="off"
                  placeholder="https://github.com/org/repo.git"
                  onChange={(event) => changeUrl(event.target.value)}
                />
                <p className="wsp-field-hint">
                  支持 https:// / ssh:// / git@host:path；只接受远端地址，不接受本地路径。
                </p>
              </div>
              <div className="wsp-grid-2">
                <div className="wsp-field">
                  <label htmlFor="wsp-branch">分支</label>
                  <input
                    id="wsp-branch"
                    value={branch}
                    spellCheck={false}
                    autoComplete="off"
                    placeholder="留空用远端默认分支"
                    onChange={(event) => setBranch(event.target.value)}
                  />
                </div>
                <div className="wsp-field">
                  <label htmlFor="wsp-cred">凭据名</label>
                  <input
                    id="wsp-cred"
                    value={credential}
                    spellCheck={false}
                    autoComplete="off"
                    placeholder="私有仓库填密钥库里的名字"
                    onChange={(event) => setCredential(event.target.value)}
                  />
                </div>
              </div>
              {gitBlocked && control ? (
                <p className="wsp-notice">
                  {control.allow_git_import ? '本机没有可用的 git 命令。' : '部署关闭了 Git 导入。'}
                </p>
              ) : (
                <p className="wsp-field-hint">
                  克隆在服务端执行，大仓库可能要等一会儿；成功后登记为受管工作区。
                </p>
              )}
            </>
          )}

          {error ? (
            <p className="wsp-alert" role="alert">
              {error}
            </p>
          ) : null}
        </div>

        <footer className="wsp-dlg-foot">
          <span className="wsp-dlg-foot-note">
            {kind === 'git' && !gitBlocked ? '克隆期间请勿关闭此窗口' : '\u00A0'}
          </span>
          <button type="button" className="wsp-btn" onClick={() => dialogRef.current?.close()}>
            取消
          </button>
          <button type="submit" className="wsp-btn is-primary" disabled={busy}>
            {busy ? '创建中…' : kind === 'local' ? '添加工作区' : '克隆并添加'}
          </button>
        </footer>
      </form>
    </dialog>
  );
}
