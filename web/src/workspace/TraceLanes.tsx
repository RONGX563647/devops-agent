/** 轨迹泳道：模型 / 工具两条横向时间轴，块的宽度就是耗时。
 *
 * 位置与宽度都按会话时间归一化，所以同一轮里「模型想了 14 秒、工具跑了 2 秒」
 * 一眼能看出来，不用心算。只画真实占用，空隙留白——空闲多久由行尾占用率说明。
 */
import type { TraceLanes as Lanes, TraceSpan } from '../types';
import { formatDuration } from './time';

interface Props {
  lanes: Lanes;
  activeStepId: string | null;
  onSelect: (stepId: string) => void;
}

const ROWS: { key: keyof Lanes; label: string; hint: string }[] = [
  { key: 'model', label: '模型', hint: '模型推理，含流式输出与思考耗时' },
  { key: 'tool', label: '工具', hint: '工具执行' },
];

const RATIOS = [0, 0.25, 0.5, 0.75, 1];

/** 刻度文案：0 要显示成 0s，不能复用 formatDuration（它把 0 写成「—」）。 */
export function formatOffset(ms: number): string {
  if (ms <= 0) return '0s';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`;
  return `${Math.floor(ms / 60_000)}m${Math.round((ms % 60_000) / 1000)}s`;
}

/** 泳道占用率：这一支吃掉了窗口的百分之多少。 */
function share(segments: TraceSpan[], total: number): number {
  const used = segments.reduce((sum, segment) => sum + (segment.end - segment.start), 0);
  return Math.min(100, Math.round((used / total) * 100));
}

function Bar({
  segment,
  base,
  total,
  active,
  onSelect,
}: {
  segment: TraceSpan;
  base: number;
  total: number;
  active: boolean;
  onSelect: (stepId: string) => void;
}) {
  const left = ((segment.start - base) / total) * 100;
  const width = Math.max(0.7, ((segment.end - segment.start) / total) * 100);
  const duration = formatDuration(segment.end - segment.start);
  return (
    <button
      type="button"
      className={`lane-bar${active ? ' active' : ''}`}
      data-status={segment.status}
      style={{ left: `${left}%`, width: `${width}%` }}
      title={`${segment.label} · ${duration} · 起于 ${formatOffset(segment.start - base)}`}
      aria-label={`泳道 ${segment.label} ${duration}`}
      onClick={() => segment.stepId && onSelect(segment.stepId)}
    />
  );
}

export function TraceLanes({ lanes, activeStepId, onSelect }: Props) {
  const all = [...lanes.model, ...lanes.tool];
  if (all.length === 0) {
    return <div className="lanes lanes-empty">这一轮还没有可绘制的时间区间。</div>;
  }
  const base = Math.min(...all.map((segment) => segment.start));
  const end = Math.max(...all.map((segment) => segment.end));
  const total = Math.max(1, end - base);

  return (
    <div className="lanes" aria-label="资源泳道">
      <div className="lanes-ruler">
        <span className="lane-label" />
        <div className="lanes-ticks mono">
          {RATIOS.map((ratio) => (
            <span key={ratio} style={{ left: `${ratio * 100}%` }}>
              {formatOffset(total * ratio)}
            </span>
          ))}
        </div>
        <span className="lane-share" />
      </div>
      {ROWS.map((row) => {
        const segments = lanes[row.key];
        const pct = share(segments, total);
        return (
          <div className="lane-row" key={row.key}>
            <span className="lane-label" title={row.hint}>
              {row.label}
            </span>
            <div className="lane-track" data-lane={row.key}>
              {segments.map((segment) => (
                <Bar
                  key={segment.id}
                  segment={segment}
                  base={base}
                  total={total}
                  active={Boolean(segment.stepId) && segment.stepId === activeStepId}
                  onSelect={onSelect}
                />
              ))}
            </div>
            <span
              className={`lane-share mono${pct === 0 ? ' idle' : ''}`}
              title={segments.length === 0 ? '这一段没有占用' : `${row.label}占用窗口 ${pct}%`}
            >
              {segments.length === 0 ? '空闲' : `${pct}%`}
            </span>
          </div>
        );
      })}
      <div className="lanes-legend">
        <span><i data-lane="model" />模型</span>
        <span><i data-lane="tool" />工具</span>
        <span><i data-lane="error" />异常</span>
        <span className="mono">总长 {formatOffset(total)}</span>
      </div>
    </div>
  );
}
