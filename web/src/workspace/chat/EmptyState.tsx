/** 对话区的空状态：有工作区时给示例问题，还没有工作区时先让用户去左栏选。
 *
 * 单独成文件是为了让 ChatTab 留在行数棘轮以内——它已经背着输入框、附件、
 * 权限卡片与轨迹刻度条了。
 */
import { SparkIcon } from './icons';

/** 空状态的示例问题：点一下直接发起会话。 */
const SAMPLES = [
  '这个仓库的 CI 流程是怎么跑的？',
  '解释一下 deployment 阶段的失败原因',
  '帮我给流水线加一个 lint 关卡',
  '最近失败最多的阶段是哪个？',
];

interface Props {
  /** 还没有挂到任何工作区：不显示示例问题，也不允许直接发问。 */
  needsWorkspace: boolean;
  onAsk: (text: string) => void;
}

export function ChatEmptyState({ needsWorkspace, onAsk }: Props) {
  return (
    <div className="chat-empty">
      <span className="chat-empty-icon">
        <SparkIcon />
      </span>
      {needsWorkspace ? (
        <>
          <h2 className="chat-empty-title">先选择一个工作区</h2>
          <p className="chat-empty-hint">
            会话都归属于某个工作区：左栏点工作区右侧的 + 新开一条，或先在工作区页添加一个本地目录。
          </p>
        </>
      ) : (
        <>
          <h2 className="chat-empty-title">开始一次会话</h2>
          <p className="chat-empty-hint">
            描述现象，Agent 会读代码、跑流水线、翻日志。每一步都以一行记录留在回答上方，
            点开就能看它到底调用了什么、拿回了什么。
          </p>
          <div className="chat-samples">
            {SAMPLES.map((q) => (
              <button key={q} className="chat-sample" onClick={() => onAsk(q)}>
                <span>{q}</span>
                <span className="chat-sample-arrow" aria-hidden="true">
                  →
                </span>
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
