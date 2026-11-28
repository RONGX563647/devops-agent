/**
 * 首次引导与场景预设（SPEC-0022 §8.4）。
 *
 * 为什么引导只有三步、且不遮页面：参数面板的学习成本不在「点哪里」，
 * 而在「这个数字改了会怎样」。所以引导只交代三件事（模型在哪换、来源徽标怎么读、
 * 保存范围），真正的解释留给右栏常驻说明。
 *
 * 预设把「参数组合」翻译成意图（省钱 / 长文 / 严格）。它只填**草稿**，
 * 不自动保存 —— 预设是起点，不是一次不可见的写入。
 */
import { useEffect, useState } from 'react';
import type { ModelPatch } from '../../types';

const TOUR_KEY = 'cfg.tour.v1';

const STEPS = [
  {
    title: '换模型在输入栏',
    body: '对话下方的「模型」chip 里可以跨网关挑；这里（设置）只管参数。',
  },
  {
    title: '来源徽标说明这个值是谁定的',
    body: '「代码默认 / yaml 基线 / 配置库 / 环境变量」四层，点徽标看完整链路。',
  },
  {
    title: '保存范围二选一',
    body: '「本次会话」换会话即失效；「全局默认」写进 config/model.local.yaml。',
  },
] as const;

function readFlag(): boolean {
  try {
    return localStorage.getItem(TOUR_KEY) === 'done';
  } catch {
    return true; // 存储不可用时不打扰（隐私模式下 localStorage 会抛）
  }
}

function writeFlag(): void {
  try {
    localStorage.setItem(TOUR_KEY, 'done');
  } catch {
    /* 存不下就下次再提示，不影响功能 */
  }
}

export function Tour() {
  const [step, setStep] = useState(() => (readFlag() ? -1 : 0));

  useEffect(() => {
    if (step < 0) return undefined;
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') setStep(-1);
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [step]);

  if (step < 0 || step >= STEPS.length) return null;
  const current = STEPS[step];

  function finish() {
    writeFlag();
    setStep(-1);
  }

  return (
    <aside className="tour" aria-label="首次使用引导">
      <div className="tour-body">
        <p className="tour-step">
          第 {step + 1} / {STEPS.length} 步
        </p>
        <h3 className="tour-title">{current.title}</h3>
        <p className="tour-text">{current.body}</p>
      </div>
      <div className="tour-actions">
        <button type="button" className="ghost-btn" onClick={finish}>
          不再提示
        </button>
        {step > 0 && (
          <button type="button" className="ghost-btn" onClick={() => setStep(step - 1)}>
            上一步
          </button>
        )}
        <button
          type="button"
          className="primary-btn"
          onClick={() => (step + 1 >= STEPS.length ? finish() : setStep(step + 1))}
        >
          {step + 1 >= STEPS.length ? '知道了' : '下一步'}
        </button>
      </div>
    </aside>
  );
}

export interface Preset {
  id: string;
  label: string;
  summary: string;
  patch: ModelPatch;
}

/** 预设不动 `provider` / `model`：模型是用户显式选的，不该被「省钱」顺手改掉。 */
export const PRESETS: Preset[] = [
  {
    id: 'save',
    label: '省钱',
    summary: '少试几次、不重试、更早压缩',
    patch: { max_iters: 4, model_max_retries: 0, agent_max_retries: 0, context_buffer_ratio: 0.1 },
  },
  {
    id: 'long',
    label: '长文',
    summary: '尽量塞满窗口再压缩',
    patch: { context_auto: true, trigger_ratio: 0.85 },
  },
  {
    id: 'strict',
    label: '严格',
    summary: '输出更确定、工具串行',
    patch: { temperature: 0, parallel_tool_calls: false },
  },
];
