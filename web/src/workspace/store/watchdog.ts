/** 流看门狗：字节层保活 + 首包/静默双阈值 + 回前台重校准。 */

import { finalize, type Entry } from './state';

/** 首包宽限：模型排队首 token 常需几十秒，这段给足容错。 */
const FIRST_EVENT_MS = 90_000;
/** 有事件后的静默窗：收到任意字节即重置，超过才判假死。 */
const IDLE_MS = 45_000;
/** 看门狗扫描周期；误判只会晚一个周期，换来更低的定时器开销。 */
const WATCHDOG_TICK_MS = 5_000;

/** 看门狗句柄：touch() 在任意字节到达时调用；dispose() 在流收尾时清理。 */
export interface Watchdog {
  touch: () => void;
  dispose: () => void;
}

/** 建立流看门狗：字节层保活 + 首包/静默双阈值 + 回前台重校准。
 *
 * - 首包前用 FIRST_EVENT_MS（模型排队慢），收到任意字节后改用 IDLE_MS。
 * - touch 由 consumeStream 的 onActivity 驱动，注释帧/未闭合 chunk 也算活动。
 * - 隐藏标签页的 setInterval 会被节流，回前台时若本没超时就重置计时，
 *   避免节流期的假死补判把一条仍在收字节的流砍掉。
 */
export function startWatchdog(
  entry: Entry,
  generation: number,
  controller: AbortController,
): Watchdog {
  let lastEventAt = Date.now();
  let firstByteSeen = false;

  const expire = () => {
    if (generation !== entry.generation || !entry.running) return;
    const limit = firstByteSeen ? IDLE_MS : FIRST_EVENT_MS;
    if (Date.now() - lastEventAt < limit) return;
    entry.stream = {
      ...entry.stream,
      error: `服务端 ${Math.round(limit / 1000)} 秒无响应，已断开`,
      running: false,
    };
    controller.abort();
    finalize(entry, 'error');
  };

  const timer = setInterval(expire, WATCHDOG_TICK_MS);
  const onVisible = () => {
    if (document.visibilityState !== 'visible') return;
    if (generation !== entry.generation || !entry.running) return;
    // 只重置「本来还没超时」的流；真断流的计时不因切回前台而清零。
    if (Date.now() - lastEventAt < IDLE_MS) lastEventAt = Date.now();
  };
  document.addEventListener('visibilitychange', onVisible);

  return {
    touch: () => {
      lastEventAt = Date.now();
      firstByteSeen = true;
    },
    dispose: () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    },
  };
}
