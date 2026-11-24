// @vitest-environment jsdom
/**
 * 健康分面板单测：分数必须配「为什么」与「值不值得信」。
 *
 * 覆盖三种真实情况：证据齐全、某维度缺证据（显示 —）、严重超预算。
 */
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { HealthPanel } from './HealthPanel';
import type { HealthDimension, RunHealth } from '../types';

function dim(
  key: string,
  label: string,
  value: number | null,
  extra: Partial<HealthDimension> = {},
): HealthDimension {
  return {
    key,
    label,
    value,
    weight: 0.25,
    confidence: 1,
    note: 'ok',
    items: [],
    ...extra,
  };
}

function makeHealth(overrides: Partial<RunHealth> = {}): RunHealth {
  return {
    run_id: 1,
    score: 66.7,
    grade: 'D',
    coverage: 0.75,
    confidence: 0.78,
    version: 'v2',
    budget_ms: 17000,
    actual_ms: 58700,
    model_calls: 3,
    dimensions: {
      execution: dim('execution', '执行健康', 100, { weight: 0.35 }),
      efficiency: dim('efficiency', '效率成本', 48, {
        weight: 0.25,
        note: '3 次模型调用 · 预算 17000ms',
        items: [{ label: '耗时', delta: -52, detail: '58700ms / 预算 17000ms = 3.5×' }],
      }),
      quality: dim('quality', '效果质量', null, {
        weight: 0.25,
        confidence: 0,
        note: '没有 correctness 评分，该维度不参与加权',
        items: [{ label: '样本', delta: 0, detail: '0 条评分' }],
      }),
      safety: dim('safety', '安全合规', 100, { weight: 0.15, confidence: 0.4 }),
    },
    ...overrides,
  };
}

describe('HealthPanel', () => {
  // 没开 globals，RTL 不会自动清理：不清就会读到上一个用例残留的 DOM
  afterEach(cleanup);

  beforeEach(() => {
    // recharts 的 ResponsiveContainer 需要 ResizeObserver，jsdom 没有
    vi.stubGlobal('ResizeObserver', class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
  });

  it('给出总分、等级、覆盖度与置信度，而不是只有一个数字', () => {
    render(<HealthPanel health={makeHealth()} />);
    expect(screen.getByText('总分 66.7')).toBeTruthy();
    expect(screen.getByText('D')).toBeTruthy();
    // 覆盖度 75%、置信度 78% 都要露出来，否则读者会以为这是全量信号
    expect(screen.getByText('75%')).toBeTruthy();
    expect(screen.getByText('78%')).toBeTruthy();
    expect(screen.getByText('v2')).toBeTruthy();
  });

  it('缺证据的维度显示 — 而不是 0 分', () => {
    render(<HealthPanel health={makeHealth()} />);
    // 雷达轴的标签与明细列表同名，这里只在明细列表里查
    const list = within(screen.getByRole('list', { name: '维度明细' }));
    expect(list.getByText('效果质量')).toBeTruthy();
    // 「—」出现两次：维度值 + 扣分项的 delta 为 0；关键是**没有**变成 0 分
    expect(list.getAllByText('—').length).toBeGreaterThan(0);
    expect(list.queryByText('0')).toBeNull();
    expect(
      list.getByText(/没有 correctness 评分，该维度不参与加权 · 置信度 0%/),
    ).toBeTruthy();
  });

  it('逐条列出扣分项，读者能对上分数为什么少了', () => {
    render(<HealthPanel health={makeHealth()} />);
    const list = within(screen.getByRole('list', { name: '维度明细' }));
    expect(list.getByText('耗时')).toBeTruthy();
    expect(list.getByText('-52.0')).toBeTruthy();
    expect(list.getByText('58700ms / 预算 17000ms = 3.5×')).toBeTruthy();
    expect(list.getAllByText(/权重 35%/).length).toBeGreaterThan(0);
  });

  it('超预算时明确提示按幂律衰减', () => {
    render(<HealthPanel health={makeHealth()} />);
    expect(screen.getByText(/实际 .*预算 .*3.5×/)).toBeTruthy();
    expect(screen.getByText(/超预算按幂律衰减/)).toBeTruthy();
  });

  it('没有耗时记录时说明效率维度不参与评分', () => {
    render(<HealthPanel health={makeHealth({ budget_ms: 0, actual_ms: 0 })} />);
    expect(screen.getByText(/没有耗时记录，效率维度不参与评分/)).toBeTruthy();
  });
});
