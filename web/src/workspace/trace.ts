/** 把会话消息派生为轨迹视图：轮次 + 步骤 + 真实耗时 + 三条资源泳道 + 聚合指标。
 *
 * 三条视图（时长/调用/轮次）读的是同一份 TraceStep，计时也只有一个来源：
 * - measured：起止事件都在；
 * - derived：阶段自报耗时（解析）或落库 span 反推；
 * - estimated：相邻步骤插值，只能看趋势，默认沉底且不参与「最慢 Top N」。
 * 会话级同时给两个口径：实测（span 并集）与墙钟（起止差），差值就是用户空闲。
 *
 * 实现按职责拆到 ./traceData/ 下，导出名保持不变。
 */
export { buildDiff, compact, summarizeArgs, summarizeResult, unionLength } from './traceData/format';
export { buildLanes, turnsFromMessages } from './traceData/build';
export {
  byDurationDesc,
  durationRows,
  percentile,
  topShare,
  traceStats,
  type TraceRow,
} from './traceData/stats';
