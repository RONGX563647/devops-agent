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
import { HARD_KIND_KEYS, HARD_KIND_META } from '../../types';
import type { EvalHardFailures, HardKindKey } from '../../types';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  PolarAngleAxis,
  PolarGrid,
  PolarRadiusAxis,
  Radar,
  RadarChart,
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

export interface RatioSegment {
  label: string;
  value: number;
  tone: string;
}

interface RatioBarProps {
  segments: RatioSegment[];
  caption: string;
}

/** 单条 100% 堆叠：比较构成而不是总量，段内直接标数值。 */
export function RatioBar({ segments, caption }: RatioBarProps) {
  const total = segments.reduce((sum, item) => sum + item.value, 0);
  if (total === 0) {
    return <Figure caption={caption} empty="暂无构成数据。" />;
  }
  return (
    <Figure caption={caption}>
      <div className="ratio-bar" role="img" aria-label={caption}>
        {segments
          .filter((item) => item.value > 0)
          .map((item) => (
            <span
              key={item.label}
              className="ratio-seg"
              style={{ width: `${(item.value / total) * 100}%`, background: item.tone }}
              title={`${item.label}: ${item.value}`}
            >
              {item.value}
            </span>
          ))}
      </div>
      <ul className="ratio-legend">
        {segments.map((item) => (
          <li key={item.label}>
            <i style={{ background: item.tone }} aria-hidden="true" />
            {item.label} {item.value}（{((item.value / total) * 100).toFixed(0)}%）
          </li>
        ))}
      </ul>
    </Figure>
  );
}

export interface HardBarPoint {
  label: string;
  /** 7 类硬失败计数，与后端 `eval/kinds.py` 同源。 */
  values: EvalHardFailures;
}

interface HardFailureChartProps {
  points: HardBarPoint[];
  caption: string;
}

/** 每类的配色：阻断类用醒目色，计分类用收敛色。 */
const HARD_BAR_TONE: Record<HardKindKey, string> = {
  safety: TONE.bad,
  forbidden_tool: '#dc2626',
  status: TONE.warn,
  tool: TONE.neutral,
  text: TONE.accent,
  grounding: '#0891b2',
  format: TONE.muted,
};

/**
 * 硬失败构成：看失败是「安全越界」还是「工具没执行」。
 *
 * 7 类全画。只画 3 类时，`tool` / `text` / `grounding` / `status` 四类失败
 * 在图里完全不存在——一批 14/15 通过、结论 fail 的运行会显示成「没有硬失败」。
 */
export function HardFailureChart({ points, caption }: HardFailureChartProps) {
  const total = points.reduce(
    (sum, item) => sum + HARD_KIND_KEYS.reduce((acc, key) => acc + (item.values?.[key] ?? 0), 0),
    0,
  );
  if (total === 0) {
    return <Figure caption={caption} empty="所选批次没有硬失败。" />;
  }
  return (
    <Figure caption={caption}>
      <div className="chart-wrap">
        <ResponsiveContainer width="100%" height={220}>
          <BarChart data={points} margin={{ top: 8, right: 12, bottom: 0, left: -18 }}>
            <CartesianGrid strokeDasharray="3 3" stroke={TONE.grid} />
            <XAxis dataKey="label" fontSize={11} />
            <YAxis allowDecimals={false} fontSize={11} />
            <Tooltip />
            <Legend />
            {HARD_KIND_KEYS.map((key) => (
              <Bar
                key={key}
                dataKey={`values.${key}`}
                name={HARD_KIND_META[key].label}
                stackId="hard"
                fill={HARD_BAR_TONE[key]}
              />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>
    </Figure>
  );
}

export interface JudgePoint {
  dimension: string;
  score: number;
}

interface JudgeRadarProps {
  points: JudgePoint[];
  caption: string;
}

/** judge 六维（1–5 同量纲）。没测到的维度不画 —— 补 0 会把「没测」画成「很差」。 */
export function JudgeRadar({ points, caption }: JudgeRadarProps) {
  if (points.length < 3) {
    return (
      <Figure
        caption={caption}
        empty={`只有 ${points.length} 个维度有样本，不足以画雷达图。`}
      />
    );
  }
  return (
    <Figure caption={caption}>
      <div className="chart-wrap">
        <ResponsiveContainer width="100%" height={260}>
          <RadarChart data={points} outerRadius="72%">
            <PolarGrid stroke={TONE.grid} />
            <PolarAngleAxis dataKey="dimension" fontSize={11} />
            <PolarRadiusAxis domain={[1, 5]} fontSize={10} />
            <Radar
              dataKey="score"
              name="均分"
              stroke={TONE.accent}
              fill={TONE.accent}
              fillOpacity={0.25}
            />
            <Tooltip />
          </RadarChart>
        </ResponsiveContainer>
      </div>
    </Figure>
  );
}

export interface MatrixData {
  batches: { id: number; started_at: string }[];
  cases: { case_id: string; cells: (boolean | null)[] }[];
}

interface PassMatrixProps {
  data: MatrixData;
  caption: string;
}

/** 用例 × 批次热力格子：`true` 通过、`false` 失败、`null` 没跑到。 */
export function PassMatrix({ data, caption }: PassMatrixProps) {
  if (data.batches.length === 0 || data.cases.length === 0) {
    return <Figure caption={caption} empty="还没有可对比的批次。" />;
  }
  return (
    <Figure caption={caption}>
      <div className="pass-matrix" role="table" aria-label={caption}>
        <div className="pass-matrix-head" role="row">
          <span role="columnheader">用例</span>
          {data.batches.map((batch) => (
            <span key={batch.id} role="columnheader" title={batch.started_at}>
              #{batch.id}
            </span>
          ))}
        </div>
        {data.cases.map((row) => (
          <div className="pass-matrix-row" role="row" key={row.case_id}>
            <span role="cell" className="pass-matrix-id">
              {row.case_id}
            </span>
            {row.cells.map((cell, index) => {
              const batch = data.batches[index];
              const state = cell === null ? '未运行' : cell ? '通过' : '失败';
              return (
                <span
                  key={`${row.case_id}-${batch?.id ?? index}`}
                  role="cell"
                  className={`pass-cell${cell === null ? ' is-empty' : cell ? ' is-pass' : ' is-fail'}`}
                  aria-label={`批次 ${batch?.id ?? '?'} · ${row.case_id} · ${state}`}
                  title={`#${batch?.id ?? '?'} ${state}`}
                />
              );
            })}
          </div>
        ))}
      </div>
    </Figure>
  );
}
