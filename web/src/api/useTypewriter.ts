/** 打字机渲染：把网络分块到达的文本按固定节奏逐字揭示。
 *
 * 后端开启 stream=True 后，chunk 仍是每 100ms 左右一块、一块 5~9 个字。
 * 直接渲染观感是"跳字"，本 hook 让显示进度与网络到达解耦：
 * 维护 target（已收到全文）与 shown（已显示字数），按固定节奏推进。
 */
import { useEffect, useState } from 'react';

/** 推进节奏：约 30ms 一帧，接近人眼可分辨的连续出字。 */
export const TICK_MS = 30;

/** 计算下一帧应推进的字数。
 *
 * 正常情况每次 1~3 字（匀速打字机）；积压过多时加速，避免显示进度被网络越落越远。
 * 剩余不足一步时直接补完，避免最后一两个字拖出明显尾巴。
 */
export function revealStep(backlog: number): number {
  if (backlog <= 0) return 0;
  if (backlog <= 3) return backlog;
  if (backlog > 240) return 12;
  if (backlog > 80) return 6;
  return 3;
}

export interface TypewriterState {
  /** 当前应显示的前缀文本。 */
  text: string;
  /** 是否已显示完 target 的全部内容。 */
  done: boolean;
}

export function useTypewriter(target: string, typing: boolean): TypewriterState {
  const [shown, setShown] = useState(0);

  useEffect(() => {
    if (!typing || shown >= target.length) return;
    const timer = setTimeout(() => {
      setShown((prev) => Math.min(prev + revealStep(target.length - prev), target.length));
    }, TICK_MS);
    return () => clearTimeout(timer);
  }, [shown, target.length, typing]);

  // 流结束后直接给全文：收尾不该再有拖尾动画。
  if (!typing) return { text: target, done: true };
  return { text: target.slice(0, Math.min(shown, target.length)), done: shown >= target.length };
}
