/**
 * 质量页的图表薄封装 —— 统一配色、空态与「图必须配文字结论」的约定。
 *
 * 颜色语义：绿=好、红=坏。这**不是** A 股的涨跌配色（红涨绿跌），
 * 质量指标编码的是对错而不是涨跌，别按股票习惯反过来。
 *
 * 每个组件都渲染 `<figure><figcaption>`：图表不能是唯一信息载体，
 * 读屏与截图场景下那行文字才是结论本身。
 */
import type { ReactNode } from 'react';
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

export const TONE = {
  good: '#15803d',
  bad: '#b91c1c',
  warn: '#b45309',
  neutral: '#475569',
  accent: '#3b82f6',
  muted: '#94a3b8',
  grid: '#e5e5e5',
} as const;

interface FigureProps {
  caption: string;
  children?: ReactNode;
  empty?: string;
}

/** 统一的图容器：caption 是必填的，空数据时优先显示空态文案。 */
function Figure({ caption, children, empty }: FigureProps) {
  return (
    <figure className="quality-figure">
      {empty ? <p className="muted">{empty}</p> : children}
      <figcaption className="hint">{caption}</figcaption>
    </figure>
  );
}

export interface TrendPoint {
  label: string;
  passRate: number;
  n: number;
}

interface TrendChartProps {
  points: TrendPoint[];
  threshold: number;
  caption: string;
}

/** 通过率趋势 + 阈值参考线。少于 2 个点时不画线（一个点画不出趋势）。 */
export function TrendChart({ points, threshold, caption }: TrendChartProps) {
  if (points.length < 2) {
    return (
      <Figure
        caption={caption}
        empty={`只有 ${points.length} 个批次，至少需要 2 个才能看趋势。`}
      />
    );
  }
  return (
    <Figure caption={caption}>
      <div className="chart-wrap">
        <ResponsiveContainer width="100%" height={220}>
          <LineChart data={points} margin={{ top: 8, right: 12, bottom: 0, left: -12 }}>
            <CartesianGrid strokeDasharray="3 3" stroke={TONE.grid} />
            <XAxis dataKey="label" fontSize={11} />
            <YAxis
              domain={[0, 1]}
              fontSize={11}
              tickFormatter={(value: number) => `${Math.round(value * 100)}%`}
            />
            <Tooltip />
            <ReferenceLine y={threshold} stroke={TONE.bad} strokeDasharray="4 4" />
            <Line
              type="monotone"
              dataKey="passRate"
              name="通过率"
              stroke={TONE.accent}
              strokeWidth={2}
              dot={{ r: 3 }}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </Figure>
  );
}
