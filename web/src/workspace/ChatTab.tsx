interface Props {
  session: string | null;
}

/** 对话 tab（占位，下一步接入 SSE 流式消息列表）。 */
export function ChatTab({ session }: Props) {
  return <div className="page"><p className="subtitle">对话 · {session ?? '全部会话'}</p></div>;
}
