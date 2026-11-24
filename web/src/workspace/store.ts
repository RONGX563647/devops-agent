/** 会话工作台状态门面：实现按职责拆到 ./store/ 下，导出名保持不变。
 *
 * - state.ts：模块级 Map、快照、收尾与 hooks；
 * - watchdog.ts：流静默/首包看门狗；
 * - actions.ts：发起/停止/切档/确认；
 * - history.ts：历史兜底与侧栏数据源。
 */
export {
  DEFAULT_PERMISSION,
  resetStore,
  useSession,
  useStoreVersion,
  type SessionSnapshot,
} from './store/state';
export { confirm, refreshPending, send, setPermission, stop } from './store/actions';
export { ensureHistory, listSessions } from './store/history';
