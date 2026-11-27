/** 抽屉页脚：提示、危险确认、保存范围三件事。
 *
 * 抽出来的原因只是行数（抽屉本体到 500 行上限了）。这里全是纯展示：
 * 按钮该不该禁用由父层算好传进来，「谁决定什么」不在这里。
 */

interface Props {
  notice: string;
  /** 危险改动的说明，非空时要勾选确认才能保存。 */
  dangers: string[];
  /** 后端给的警告（能力不匹配、窗口未校准等）。 */
  warnings: string[];
  /** 本地越界提示：非空时禁用保存，服务端仍是唯一裁判。 */
  invalid: string;
  ack: boolean;
  busy: boolean;
  dirtyCount: number;
  /** 已点过「保存为全局默认」，正在二次确认。 */
  confirming: boolean;
  /** 落盘文件路径（已缩成末两段）。 */
  filePath: string;
  onAck: (checked: boolean) => void;
  /** 点「保存为全局默认」：先要二次确认，不直接写盘。 */
  onAskGlobal: () => void;
  onCancelConfirm: () => void;
  /** 二次确认后真正落盘。 */
  onSaveGlobal: () => void;
  onResetSession: () => void;
  onSaveSession: () => void;
}

export function DrawerFooter({
  notice,
  dangers,
  warnings,
  invalid,
  ack,
  busy,
  dirtyCount,
  confirming,
  filePath,
  onAck,
  onAskGlobal,
  onCancelConfirm,
  onSaveGlobal,
  onResetSession,
  onSaveSession,
}: Props) {
  const needAck = dangers.length > 0 && !ack;
  const blocked = busy || dirtyCount === 0 || needAck || Boolean(invalid);
  return (
    <footer className="mcd-foot">
      {notice && (
        <p className="mcd-notice" role="status">
          {notice}
        </p>
      )}
      {dangers.map((text) => (
        <p className="mc-warn" key={text}>
          {text}
        </p>
      ))}
      {dangers.length > 0 && (
        <label className="mc-ack">
          <input type="checkbox" checked={ack} onChange={(e) => onAck(e.target.checked)} />
          我了解上面的影响
        </label>
      )}
      {invalid && <p className="mc-warn">{invalid}</p>}
      {warnings.map((text) => (
        <p className="mc-warn" key={text}>
          {text}
        </p>
      ))}

      {confirming ? (
        <div className="mcd-confirm">
          <p>
            将写入 <code>{filePath}</code>，影响所有会话的下一次模型调用。
            正在跑的会话等当前轮结束才生效，不会打断它们的流。
          </p>
          <div className="mcd-actions">
            <button className="ghost-btn" onClick={onCancelConfirm}>
              取消
            </button>
            <button className="danger-btn" disabled={busy} onClick={onSaveGlobal}>
              确认写入
            </button>
          </div>
        </div>
      ) : (
        <div className="mcd-actions">
          <button className="ghost-btn" disabled={busy} onClick={onResetSession}>
            恢复本会话
          </button>
          <button className="ghost-btn" disabled={blocked} onClick={onSaveSession}>
            仅本次会话
          </button>
          <button className="primary-btn" disabled={blocked} onClick={onAskGlobal}>
            保存为全局默认
          </button>
        </div>
      )}
    </footer>
  );
}
