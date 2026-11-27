/** 对话 tab：订阅会话 store 渲染消息与过程时间线，SSE 生命周期由 store 托管。
 *
 * 图标、附件文案与消息气泡按职责拆到 ./chat/ 下，本文件保留容器与原有公开导出。
 */
import { useQuery } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  deleteAttachment,
  fetchHealth,
  fetchModelConfig,
  switchSessionPermission,
  uploadAttachment,
} from '../../api/client';
import type {
  Attachment,
  ChatMessage,
  ConfirmScope,
  PendingAttachment,
  PermissionKey,
} from '../../types';
import { attachMeta } from './attachments';
import { GitHubIcon, GlobeIcon, PaperclipIcon, SendIcon, SparkIcon } from './icons';
import { AssistantMessage } from './Message';
import { newId } from '../id';
import { extractLinks } from '../links';
import { ModelConfigDrawer } from '../model-config/Drawer';
import { PermissionCard } from '../PermissionCard';
import { PermissionPicker } from '../PermissionPicker';
import { SearchToggle } from './SearchToggle';
import { confirm, refreshPending, send, setPermission, stop } from '../store/actions';
import { useSession } from '../store/state';
import { CompressionTimeline, ContextRing } from '../ContextRing';
import { TraceRail } from '../TraceRail';
import { turnsFromMessages } from '../traceData/build';

export { attachMeta, formatSize } from './attachments';
export { formatClock } from '../time';
interface Props {
  session: string;
  /** 首次发送时通知外层把「新会话」落到 URL 上。 */
  onFirstSend?: () => void;
  onOpenTrace?: () => void;
}

/** 距底小于该像素值时视为"用户还在底部"，可以继续吸底。 */
export const STICK_THRESHOLD = 80;

/** 判断是否应自动吸底：上翻读历史时不能把视口强行拽回底部。 */
export function shouldStickToBottom(
  scrollHeight: number,
  scrollTop: number,
  clientHeight: number,
  threshold: number = STICK_THRESHOLD,
): boolean {
  return scrollHeight - scrollTop - clientHeight < threshold;
}

/** 空状态的示例问题：点一下直接发起会话。 */
const SAMPLES = [
  '这个仓库的 CI 流程是怎么跑的？',
  '解释一下 deployment 阶段的失败原因',
  '帮我给流水线加一个 lint 关卡',
  '最近失败最多的阶段是哪个？',
];

export function ChatTab({ session, onFirstSend, onOpenTrace }: Props) {
  const view = useSession(session);
  // 运行时模式：Echo 兜底意味着没配模型凭据，回复只是把输入回显一遍。
  // 不显式说出来，用户只会看到「模型答非所问」然后反复重试。
  const health = useQuery({
    queryKey: ['health'],
    queryFn: fetchHealth,
    staleTime: 60_000,
    retry: 1,
  });
  const degraded = health.data?.runtime?.kind === 'echo';
  // 模型参数：面板与 chat-meta 的「参数已改」标记共用同一份数据，
  // 拉两次会出现「面板说改了、标记说没改」。
  const modelConfig = useQuery({
    queryKey: ['model-config', session],
    queryFn: () => fetchModelConfig(session),
    staleTime: 30_000,
    retry: 1,
  });
  const overrideCount = Object.keys(modelConfig.data?.overrides ?? {}).length;
  const [input, setInput] = useState('');
  // 联网搜索：跟 DeepSeek 一样做成输入框下方的显式开关，默认关，逐轮生效
  const [webSearch, setWebSearch] = useState(false);
  // 切档结果必须可见：服务端可能没有活跃实例（只改下一轮），也可能拒绝越权档。
  const [permNotice, setPermNotice] = useState('');
  const [files, setFiles] = useState<PendingAttachment[]>([]);
  const [activeId, setActiveId] = useState('');
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const stickRef = useRef(true);
  const composingRef = useRef(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const messages = view.messages;
  const turns = useMemo(() => turnsFromMessages(messages), [messages]);
  // 输入即解析：不等到发送，让用户先确认链接被认对了没有。
  const links = useMemo(() => extractLinks(input), [input]);
  const last = messages[messages.length - 1];
  const lastText = last?.text ?? '';

  /** 滚动时记录用户是否仍贴着底部，决定后续 token 是否自动跟随。 */
  function handleScroll() {
    const el = scrollRef.current;
    if (!el) return;
    stickRef.current = shouldStickToBottom(el.scrollHeight, el.scrollTop, el.clientHeight);
    syncActiveTurn();
  }

  /** 用可视区顶部反查当前轮次，供右侧刻度条高亮。 */
  function syncActiveTurn() {
    const el = scrollRef.current;
    if (!el || turns.length === 0) return;
    const anchor = el.getBoundingClientRect().top + 72;
    let current = turns[0].id;
    for (const turn of turns) {
      const node = document.getElementById(`msg-${turn.id}`);
      if (!node) continue;
      if (node.getBoundingClientRect().top <= anchor) current = turn.id;
      else break;
    }
    setActiveId(current);
  }

  // 新 token 或新消息到达时吸底；用户上翻时 stickRef 为假，不打扰阅读。
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !stickRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [lastText, messages.length]);

  useEffect(() => {
    syncActiveTurn();
    // turns 变化时同步一次高亮，避免切换会话后刻度条无选中项
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session, turns.length]);

  // 重新挂载（刷新/切回会话）时把服务端还挂着的确认找回来，
  // 否则前端丢了卡片、后端还在等，这一轮就永远停住了。
  useEffect(() => {
    void refreshPending(session);
  }, [session]);

  // 输入框随内容自动增高，到 200px 后交给内部滚动。
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [input]);

  /** 发起一轮：首次发送时把草稿会话 id 落到 URL，再交给 store 后台执行。 */
  function run(text: string, attachments: Attachment[] = []) {
    const value = text.trim();
    // 只挂附件、不写字也是合法的一轮：这时候问题就是「看看这个文件」
    if ((!value && attachments.length === 0) || view.running || view.awaiting) return;
    onFirstSend?.();
    send(session, value, attachments, view.permission, webSearch);
  }

  /** 确认卡片的三选一：全部待确认调用一起答复。 */
  function decide(scope: ConfirmScope | 'reject') {
    const awaiting = view.awaiting;
    if (!awaiting || awaiting.calls.length === 0) return;
    confirm(
      session,
      awaiting.calls.map((call) => ({
        tool_call_id: call.tool_call_id,
        confirmed: scope !== 'reject',
        scope: scope === 'reject' ? 'once' : scope,
      })),
    );
  }

  /** 运行中也能切：服务端原地热替换当前实例的权限上下文与工具集。 */
  async function changePermission(key: PermissionKey) {
    const previous = view.permission;
    setPermission(session, key);
    setPermNotice('切档中…');
    try {
      const result = await switchSessionPermission(session, key, webSearch);
      setPermNotice(
        result.applied
          ? '已生效：本轮后续工具调用与模型请求按新档位'
          : '下一轮发送按新档位（当前没有活跃会话）',
      );
    } catch (error) {
      // 未开放档 / 会话不匹配：撤回本地选择，别让界面说一套、后端做另一套
      setPermission(session, previous);
      setPermNotice(error instanceof Error ? error.message : '切档失败');
    }
  }

  function submit() {
    const ready = files.filter((item) => item.status === 'ok');
    if ((!input.trim() && ready.length === 0) || view.running || view.awaiting) return;
    const text = input;
    setInput('');
    setFiles([]);
    run(text, ready);
  }

  /** 单个文件上传：成功替换乐观条目，失败把原因留在芯片上。 */
  async function uploadOne(file: File, key: string) {
    try {
      const saved = await uploadAttachment(file);
      setFiles((prev) => prev.map((item) => (item.key === key ? { ...saved, key } : item)));
    } catch (error) {
      const problem = error instanceof Error ? error.message : '上传失败';
      setFiles((prev) =>
        prev.map((item) => (item.key === key ? { ...item, status: 'error', problem } : item)),
      );
    }
  }

  /** 选中文件后先占位再上传：上传期间用户能看到「正在处理哪几个文件」。 */
  function addFiles(picked: FileList | null) {
    if (!picked || picked.length === 0) return;
    const chosen = Array.from(picked);
    const placeholders: PendingAttachment[] = chosen.map((file) => ({
      key: newId('att'),
      id: '',
      name: file.name,
      size: file.size,
      suffix: '',
      chars: 0,
      truncated: false,
      status: 'uploading',
    }));
    setFiles((prev) => [...prev, ...placeholders]);
    void Promise.all(chosen.map((file, index) => uploadOne(file, placeholders[index].key)));
  }

  function removeFile(key: string) {
    const target = files.find((item) => item.key === key);
    setFiles((prev) => prev.filter((item) => item.key !== key));
    // 只删服务端已落盘的那份；上传中的条目没有 id，自然跳过
    if (target?.id) void deleteAttachment(target.id);
  }

  /** 重试：用同一条用户输入再发一轮（保留历史，不重写已发生的内容）。 */
  function retry(message: ChatMessage) {
    if (view.running) return;
    const index = messages.findIndex((item) => item.id === message.id);
    const asked = messages[index - 1];
    if (!asked || asked.role !== 'user') return;
    run(asked.text);
  }

  function locate(turnId: string) {
    const node = document.getElementById(`msg-${turnId}`);
    node?.scrollIntoView({ block: 'start', behavior: 'smooth' });
    setActiveId(turnId);
  }

  const empty = messages.length === 0;
  const readyCount = files.filter((item) => item.status === 'ok').length;
  // 挂起等确认时不让发下一轮：同一会话同时只能有一条未结束的 run
  const canSend =
    (Boolean(input.trim()) || readyCount > 0) && !view.running && !view.awaiting;

  return (
    <div className="chat-wrap">
      {degraded && (
        <div className="runtime-banner" role="status">
          <span className="runtime-banner-tag mono">Echo</span>
          <span className="runtime-banner-text">
            {health.data?.runtime?.notice || '未配置模型凭据，当前回复只是回显输入'}
          </span>
        </div>
      )}
      <div
        className={`chat-scroll${empty ? ' is-empty' : ''}`}
        ref={scrollRef}
        onScroll={handleScroll}
      >
        <div className="chat-thread">
          {view.loading && <div className="chat-loading mono">正在载入会话历史…</div>}
          {messages.map((m) => {
            if (m.role === 'user') {
              return (
                <div className="chat-msg user" id={`msg-${m.id}`} key={m.id}>
                  <div className="chat-bubble">
                    {m.text && <span className="chat-bubble-text">{m.text}</span>}
                    {m.attachments && m.attachments.length > 0 && (
                      <span className="chat-attachments">
                        {m.attachments.map((item) => (
                          <span className="attach-tag" key={item.id} title={item.name}>
                            {item.name}
                          </span>
                        ))}
                      </span>
                    )}
                  </div>
                </div>
              );
            }
            return (
              <AssistantMessage
                key={m.id}
                message={m}
                live={view.running && m.id === last?.id}
                onRetry={retry}
              />
            );
          })}
          {empty && !view.loading && (
            <div className="chat-empty">
              <span className="chat-empty-icon">
                <SparkIcon />
              </span>
              <h2 className="chat-empty-title">开始一次会话</h2>
              <p className="chat-empty-hint">
                描述现象，Agent 会读代码、跑流水线、翻日志。每一步都以一行记录留在回答上方，
                点开就能看它到底调用了什么、拿回了什么。
              </p>
              <div className="chat-samples">
                {SAMPLES.map((q) => (
                  <button key={q} className="chat-sample" onClick={() => run(q)}>
                    <span>{q}</span>
                    <span className="chat-sample-arrow" aria-hidden="true">
                      →
                    </span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
      {/* 确认卡片脱离消息流，常驻在输入框上方：消息再长也不会被滚到中间去。 */}
      {view.awaiting && view.awaiting.calls.length > 0 && (
        <div className="confirm-dock">
          <PermissionCard
            calls={view.awaiting.calls}
            expiresAt={view.awaiting.expiresAt}
            busy={view.running}
            onDecide={decide}
          />
        </div>
      )}
      <TraceRail
        turns={turns}
        activeId={activeId}
        streaming={view.running}
        onLocate={(turn) => locate(turn.id)}
        onOpenTrace={onOpenTrace}
      />
      {/* 压缩过程贴着输入框：它解释的是「为什么接下来的回答变短了」。 */}
      <CompressionTimeline records={view.compressions} />
      <div className="chat-composer">
        <div className="chat-compose">
          {files.length > 0 && (
            <div className="attach-row">
              {files.map((item) => (
                <span
                  className={`attach-chip${item.status === 'error' ? ' is-error' : ''}`}
                  key={item.key}
                  title={item.problem || item.preview || item.name}
                >
                  <span className="attach-name">{item.name}</span>
                  <span className="attach-meta mono">{attachMeta(item)}</span>
                  <button
                    className="attach-remove"
                    aria-label={`移除 ${item.name}`}
                    onClick={() => removeFile(item.key)}
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
          )}
          {links.length > 0 && (
            <div className="link-row" role="group" aria-label="消息中的链接">
              {links.map((link) => (
                <a
                  key={link.url}
                  className={`link-chip${link.kind === 'github' ? ' is-github' : ''}`}
                  href={link.url}
                  target="_blank"
                  rel="noreferrer noopener"
                  title={link.url}
                >
                  {link.kind === 'github' ? <GitHubIcon /> : <GlobeIcon />}
                  <span className="link-label">{link.label}</span>
                </a>
              ))}
            </div>
          )}
          <textarea
            ref={textareaRef}
            aria-label="用户输入"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onCompositionStart={() => {
              composingRef.current = true;
            }}
            onCompositionEnd={() => {
              composingRef.current = false;
            }}
            onKeyDown={(e) => {
              // 中文输入法选词时的 Enter 不能当发送；同时兼容 isComposing 标记。
              if (e.key === 'Enter' && !e.shiftKey && !composingRef.current && !e.nativeEvent.isComposing) {
                e.preventDefault();
                submit();
              }
            }}
            placeholder="给 DevOps Agent 发消息…"
            rows={1}
          />
          <div className="composer-bar">
            <PermissionPicker current={view.permission} onChange={changePermission} />
            <ModelConfigDrawer
              session={session}
              config={modelConfig.data}
              contextTokens={view.contextTokens}
              onChanged={() => void modelConfig.refetch()}
            />
            <SearchToggle
              on={webSearch}
              running={view.running}
              onToggle={() => setWebSearch((value) => !value)}
            />
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className="attach-input"
              aria-label="选择要上传的文件"
              onChange={(e) => {
                addFiles(e.target.files);
                // 清空 value：同一个文件连选两次也要能再次触发 change
                e.target.value = '';
              }}
            />
            <button
              className="attach-btn"
              aria-label="添加文件"
              title="添加文件（本地解析后随消息一起发送）"
              onClick={() => fileInputRef.current?.click()}
            >
              <PaperclipIcon />
            </button>
            <span className="composer-hint" aria-live="polite">
              {permNotice ||
                (view.awaiting
                  ? '等待你确认下方卡片'
                  : view.running
                    ? '生成中，仍可切档；停止后可发送新消息'
                    : 'Enter 发送 · Shift+Enter 换行')}
            </span>
            <ContextRing
              tokens={view.contextTokens}
              threshold={view.contextThreshold}
              contextSize={view.contextSize}
              busy={view.compressions.some((item) => item.state === 'running')}
            />
            {view.running && (
              <button className="send-btn stop" onClick={() => stop(session)}>
                停止
              </button>
            )}
            <button
              className={`send-btn${canSend ? ' ready' : ''}`}
              aria-label="发送"
              onClick={submit}
              disabled={!canSend}
            >
              <SendIcon />
            </button>
          </div>
        </div>
        <div className="chat-meta">
          <span>{view.eventCount} 帧事件</span>
          <span>
            {view.inputTokens} in / {view.outputTokens} out
          </span>
          {view.modelName && <span className="mono">{view.modelName}</span>}
          {overrideCount > 0 && (
            <span className="chat-meta-override" title="本会话有模型参数覆盖，点齿轮可查看">
              参数已改 {overrideCount}
            </span>
          )}
          {view.runId !== null && <span>run #{view.runId}</span>}
        </div>
      </div>
    </div>
  );
}
