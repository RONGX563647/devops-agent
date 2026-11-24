/**
 * 健康分图表 —— 四维雷达、分数直方图、拖分排行。
 *
 * 与 `pages/quality/charts.tsx` 同一套约定：图必须配一行文字结论（figcaption），
 * 空数据给空态文案而不是空白画布；颜色只表达「好/中/坏」，不表达涨跌。
 */
import type { ReactNode } from 'react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  PolarAngleAxis,
  PolarGrid,
  PolarRadiusAxis,
  Radar,
  RadarChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { HealthBucket, HealthDimension } from '../types';

const TONE = {
  good: '#15803d',
  bad: '#b91c1c',
  warn: '#b45309',
  accent: '#3b82f6',
  grid: '#e5e5e5',
} as const;

/** 按分数取色：>=80 好、>=60 中、其余坏。 */
export function toneFor(value: number): string {
  if (value >= 80) return TONE.good;
  if (value >= 60) return TONE.warn;
  return TONE.bad;
}

interface FigureProps {
  caption: string;
  empty?: string;
  children?: ReactNode;
}

function Figure({ caption, empty, children }: FigureProps) {
  return (
    <figure className="quality-figure">
      {empty ? <p className="muted">{empty}</p> : children}
      <figcaption className="hint">{caption}</figcaption>
    </figure>
  );
}

interface RadarProps {
  dimensions: Record<string, HealthDimension>;
  caption: string;
}

/** 四维雷达：一眼看出「哪一维塌了」，比分条更容易比较形状。 */
export function DimensionRadar({ dimensions, caption }: RadarProps) {
  const points = Object.values(dimensions).map((dimension) => ({
    name: dimension.label,
    value: dimension.value === null ? 0 : Math.round(dimension.value),
    actual: dimension.value,
  }));
  const missing = Object.values(dimensions).filter((item) => item.value === null);
  if (points.length === 0) {
    return <Figure caption={caption} empty="没有可绘制的维度。" />;
  }
  return (
    <Figure
      caption={
        missing.length > 0
          ? `${caption}；${missing.map((item) => item.label).join('、')}缺少证据，图上按 0 画但不参与加权。`
          : caption
      }
    >
      <ResponsiveContainer width="100%" height={220}>
        <RadarChart data={points} outerRadius="72%">
          <PolarGrid stroke={TONE.grid} />
          <PolarAngleAxis dataKey="name" fontSize={12} />
          <PolarRadiusAxis domain={[0, 100]} tick={false} axisLine={false} />
          <Tooltip
            formatter={(value: number, _name: string, item: { payload?: { actual?: number | null } }) => [
              item?.payload?.actual === null ? '无证据' : `${value} 分`,
              '得分',
            ]}
          />
          <Radar dataKey="value" stroke={TONE.accent} fill={TONE.accent} fillOpacity={0.25} />
        </RadarChart>
      </ResponsiveContainer>
    </Figure>
  );
}

interface AverageRadarProps {
  averages: Record<string, { label: string; avg: number | null; samples: number }>;
  caption: string;
}

/** 四维均值雷达（看板用）：样本数为 0 的维度不画，避免把「没数据」画成 0 分。 */
export function AverageRadar({ averages, caption }: AverageRadarProps) {
  const entries = Object.values(averages).filter((item) => item.avg !== null);
  if (entries.length === 0) {
    return <Figure caption={caption} empty="还没有可聚合的评分。" />;
  }
  const points = entries.map((item) => ({
    name: item.label,
    value: Math.round(item.avg ?? 0),
    samples: item.samples,
  }));
  const weakest = points.reduce((worst, item) => (item.value < worst.value ? item : worst), points[0]);
  return (
    <Figure
      caption={`${caption}；${points.length} 个维度有样本，当前最弱是 ${weakest.name}（均值 ${weakest.value}，n=${weakest.samples}）。`}
    >
      <ResponsiveContainer width="100%" height={230}>
        <RadarChart data={points} outerRadius="72%">
          <PolarGrid stroke={TONE.grid} />
          <PolarAngleAxis dataKey="name" fontSize={12} />
          <PolarRadiusAxis domain={[0, 100]} tick={false} axisLine={false} />
          <Tooltip
            formatter={(value: number, _name: string, item: { payload?: { samples?: number } }) => [
              `${value} 分（n=${item?.payload?.samples ?? 0}）`,
              '均值',
            ]}
          />
          <Radar dataKey="value" stroke={TONE.accent} fill={TONE.accent} fillOpacity={0.25} />
        </RadarChart>
      </ResponsiveContainer>
    </Figure>
  );
}

interface HistogramProps {
  buckets: HealthBucket[];
  caption: string;
}

/** 分数分布：均值相同也可能一个「都很稳」一个「一半满分一半崩」，分布才看得出。 */
export function ScoreHistogram({ buckets, caption }: HistogramProps) {
  const total = buckets.reduce((sum, bucket) => sum + bucket.count, 0);
  if (total === 0) {
    return <Figure caption={caption} empty="还没有可评分的运行。" />;
  }
  const peak = buckets.reduce((best, bucket) => (bucket.count > best.count ? bucket : best), buckets[0]);
  return (
    <Figure caption={`${caption}；样本 ${total} 次，最多的区间是 ${peak.bucket}（${peak.count} 次）。`}>
      <ResponsiveContainer width="100%" height={220}>
        <BarChart data={buckets}>
          <CartesianGrid strokeDasharray="3 3" stroke={TONE.grid} vertical={false} />
          <XAxis dataKey="bucket" fontSize={11} interval={0} angle={-30} textAnchor="end" height={54} />
          <YAxis fontSize={11} allowDecimals={false} />
          <Tooltip formatter={(value: number) => [`${value} 次`, '运行数']} />
          <Bar dataKey="count" radius={[4, 4, 0, 0]}>
            {buckets.map((bucket) => (
              <Cell key={bucket.bucket} fill={toneFor(bucket.from)} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </Figure>
  );
}

export interface DeficitRow {
  key: string;
  label: string;
  avg: number;
  loss: number;
}

interface DeficitProps {
  rows: DeficitRow[];
  caption: string;
}

/** 拖分排行：weight × (100 − 均值)，直接回答「先修哪个维度最划算」。 */
export function DeficitChart({ rows, caption }: DeficitProps) {
  if (rows.length === 0) {
    return <Figure caption={caption} empty="还没有可比较的维度。" />;
  }
  const top = rows[0];
  return (
    <Figure caption={`${caption}；当前最拖分的是 ${top.label}（−${top.loss.toFixed(1)} 分）。`}>
      <ResponsiveContainer width="100%" height={Math.max(160, rows.length * 44)}>
        <BarChart data={rows} layout="vertical" margin={{ left: 8, right: 16 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={TONE.grid} horizontal={false} />
          <XAxis type="number" fontSize={11} />
          <YAxis type="category" dataKey="label" width={78} fontSize={12} />
          <Tooltip formatter={(value: number) => [`−${value.toFixed(1)} 分`, '拖分']} />
          <Bar dataKey="loss" radius={[0, 4, 4, 0]}>
            {rows.map((row) => (
              <Cell key={row.key} fill={toneFor(row.avg)} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </Figure>
  );
}
