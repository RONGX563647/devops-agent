/** 与后端 Pydantic 响应对齐的前端类型（门面）。
 *
 * 实际定义按领域拆到 ./types/ 下；同名 .ts 文件会被解析器优先命中，
 * 因此 `from '../types'` 这类既有 import 无需改动。
 */
export * from './types/permission';
export * from './types/run';
export * from './types/health';
export * from './types/eval';
export * from './types/model';
