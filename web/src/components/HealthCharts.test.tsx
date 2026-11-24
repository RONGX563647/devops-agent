// @vitest-environment jsdom
/**
 * 健康分图表：空态与取色分档。
 *
 * 图表本身由 recharts 负责渲染（jsdom 里量不到尺寸），这里钉住的是
 * 「没有数据时不能画成 0 分」与「颜色只表达好/中/坏」这两条约定。
 */
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AverageRadar, DeficitChart, DimensionRadar, ScoreHistogram, toneFor } from './HealthCharts';
import type { HealthDimension } from '../types';

afterEach(cleanup);

beforeEach(() => {
  // recharts 的 ResponsiveContainer 依赖 ResizeObserver
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
});

function dim(label: string, value: number | null): HealthDimension {
  return { key: label, label, value, weight: 0.25, confidence: 1, note: '', items: [] };
}

describe('HealthCharts', () => {
  it('取色按 80 / 60 分档，只有好中坏三种语义', () => {
    expect(toneFor(90)).toBe('#15803d');
    expect(toneFor(80)).toBe('#15803d');
    expect(toneFor(70)).toBe('#b45309');
    expect(toneFor(59)).toBe('#b91c1c');
    expect(toneFor(0)).toBe('#b91c1c');
  });

  it('直方图没有样本时说清楚，而不是画一排 0', () => {
    render(<ScoreHistogram buckets={[]} caption="分布" />);
    expect(screen.getByText('还没有可评分的运行。')).toBeTruthy();
  });

  it('直方图给了样本就在 caption 里报峰值区间', () => {
    render(
      <ScoreHistogram
        buckets={[
          { bucket: '60-70', from: 60, count: 2 },
          { bucket: '90-100', from: 90, count: 5 },
        ]}
        caption="分布"
      />,
    );
    expect(screen.getByText(/样本 7 次，最多的区间是 90-100（5 次）/)).toBeTruthy();
  });

  it('拖分排行空态与首选维度', () => {
    render(<DeficitChart rows={[]} caption="拖分" />);
    expect(screen.getByText('还没有可比较的维度。')).toBeTruthy();
    cleanup();
    render(
      <DeficitChart
        rows={[
          { key: 'efficiency', label: '效率成本', avg: 48, loss: 13 },
          { key: 'execution', label: '执行健康', avg: 100, loss: 0 },
        ]}
        caption="拖分"
      />,
    );
    expect(screen.getByText(/最拖分的是 效率成本（−13.0 分）/)).toBeTruthy();
  });

  it('雷达把缺证据的维度说出来，不假装它是 0 分', () => {
    render(
      <DimensionRadar
        dimensions={{
          execution: dim('执行健康', 100),
          quality: dim('效果质量', null),
        }}
        caption="四维"
      />,
    );
    expect(screen.getByText(/效果质量缺少证据，图上按 0 画但不参与加权/)).toBeTruthy();
  });

  it('均值雷达在没有样本时给空态', () => {
    render(<AverageRadar averages={{ quality: { label: '效果质量', avg: null, samples: 0 } }} caption="均值" />);
    expect(screen.getByText('还没有可聚合的评分。')).toBeTruthy();
  });

  it('均值雷达报出最弱维度与样本数', () => {
    render(
      <AverageRadar
        averages={{
          execution: { label: '执行健康', avg: 96, samples: 10 },
          efficiency: { label: '效率成本', avg: 42, samples: 8 },
        }}
        caption="均值"
      />,
    );
    expect(screen.getByText(/最弱是 效率成本（均值 42，n=8）/)).toBeTruthy();
  });
});
