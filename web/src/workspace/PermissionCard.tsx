/** 确认卡片：把「模型想执行什么」原样摊开，再让人决定。
 *
 * 渲染纪律：
 * - 命令用纯文本节点渲染（React 默认转义），不用 dangerouslySetInnerHTML；
 * - 先剥掉 ANSI 转义序列，否则日志里的颜色码会显示成乱码；
 * - 超长命令折叠，避免一条 minified 的 curl 把卡片撑满整屏。
 */
import { useState } from 'react';
import type { ConfirmChoice, PendingCall } from '../types';
import { formatClock } from './time';

const RISK_LABEL: Record<PendingCall['risk'], string> = {
  read: '只读',
  exec: '执行',
  write: '写入',
  danger: '高危',
};

/** 折叠阈值：超过就只显示前 300 字。 */
const PREVIEW_LIMIT = 300;

/** ESC 开头的 CSI 序列；用 fromCharCode 拼，免得源码里出现控制字符。 */
const ANSI_PATTERN = new RegExp(`${String.fromCharCode(27)}\\[[0-9;?]*[A-Za-z]`, 'g');

/** 去掉 ANSI 颜色/光标控制序列。 */
export function stripAnsi(text: string): string {
  return text.replace(ANSI_PATTERN, '');
}

/** 折叠超长命令；返回是否被截断，供界面给「展开」按钮。 */
export function clampCommand(text: string, limit: number = PREVIEW_LIMIT): {
  text: string;
  truncated: boolean;
} {
  const clean = stripAnsi(text);
  if (clean.length <= limit) return { text: clean, truncated: false };
  return { text: `${clean.slice(0, limit)}…`, truncated: true };
}

interface Props {
  calls: PendingCall[];
  expiresAt: string | null;
  busy?: boolean;
  /** reject 表示拒绝这一批；其余是「这一次 / 本会话 / 以后都这样」。 */
  onDecide: (choice: ConfirmChoice) => void;
}

export function PermissionCard({ calls, expiresAt, busy, onDecide }: Props) {
  const [expanded, setExpanded] = useState(false);
  const danger = calls.some((call) => call.risk === 'danger');
  const deadline = expiresAt ? formatClock(expiresAt) : '';

  return (
    <div className="confirm-card" role="group" aria-label="等待确认的操作">
      <div className="confirm-head">
        <span className="confirm-title">需要你确认</span>
        {danger && <span className="risk-tag risk-danger">高危</span>}
        {deadline && <span className="confirm-deadline mono">有效至 {deadline}</span>}
      </div>
      <ul className="confirm-list">
        {calls.map((call) => {
          const { text, truncated } = expanded
            ? clampCommand(call.input, Number.MAX_SAFE_INTEGER)
            : clampCommand(call.input);
          return (
            <li key={call.tool_call_id} className="confirm-item">
              <div className="confirm-item-head">
                <span className="confirm-tool mono">{call.name}</span>
                <span className={`risk-tag risk-${call.risk}`}>{RISK_LABEL[call.risk]}</span>
              </div>
              <pre className="confirm-command">{text}</pre>
              {truncated && (
                <button type="button" className="confirm-more" onClick={() => setExpanded(true)}>
                  展开完整命令
                </button>
              )}
            </li>
          );
        })}
      </ul>
      <div className="confirm-actions">
        <button
          type="button"
          className="ghost-btn"
          disabled={busy}
          onClick={() => onDecide('reject')}
        >
          拒绝
        </button>
        <button type="button" className="ghost-btn" disabled={busy} onClick={() => onDecide('once')}>
          这一次
        </button>
        <button
          type="button"
          className="primary-btn"
          disabled={busy || danger}
          title={danger ? '高危命令只允许放行一次' : '本会话内这类命令不再询问'}
          onClick={() => onDecide('session')}
        >
          本会话
        </button>
        <button
          type="button"
          className="ghost-btn"
          disabled={busy || danger}
          title={
            danger
              ? '高危命令只允许放行一次'
              : '写进部署级规则文件，换会话、重启后仍然有效'
          }
          onClick={() => onDecide('forever')}
        >
          以后都这样
        </button>
      </div>
    </div>
  );
}
