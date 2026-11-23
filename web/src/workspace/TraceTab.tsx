interface Props {
  session: string | null;
}

/** 轨迹 tab（占位，下一步接入运行选择与瀑布图）。 */
export function TraceTab({ session }: Props) {
  return <div className="page"><p className="subtitle">轨迹 · {session ?? '全部会话'}</p></div>;
}
