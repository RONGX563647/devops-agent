// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { TraceLanes as Lanes } from '../types';
import { TraceLanes } from './TraceLanes';

const T0 = Date.parse('2026-11-23T10:00:00.000Z');
const at = (offset: number) => T0 + offset;

const LANES: Lanes = {
  model: [
    { id: 'm1', lane: 'model', start: at(0), end: at(8000), status: 'ok', label: '模型调用', stepId: 'model:1' },
    { id: 'm2', lane: 'model', start: at(30_000), end: at(60_000), status: 'ok', label: '思考', stepId: 'think:1' },
  ],
  tool: [
    { id: 't1', lane: 'tool', start: at(9000), end: at(11_000), status: 'ok', label: 'Bash', stepId: 'tool:1' },
    { id: 't2', lane: 'tool', start: at(62_000), end: at(64_000), status: 'error', label: 'Bash', stepId: 'tool:2' },
  ],
  memory: [
    { id: 'mem1', lane: 'memory', start: at(61_000), end: at(61_008), status: 'ok', label: '记忆压缩', stepId: 'memory:1' },
  ],
};

afterEach(cleanup);

describe('TraceLanes', () => {
  it('只为真实占用画块，空隙不补等待段', () => {
    const { container } = render(
      <TraceLanes lanes={LANES} activeStepId={null} onSelect={() => {}} />,
    );
    // 5 条真实区间（模型 2 + 工具 2 + 记忆 1）→ 5 个块；空隙不出现占位块
    expect(container.querySelectorAll('.lane-bar')).toHaveLength(5);
    expect(screen.queryByLabelText(/泳道 等待/)).toBeNull();
  });

  it('行尾给出占用率，空泳道写明空闲', () => {
    render(<TraceLanes lanes={LANES} activeStepId={null} onSelect={() => {}} />);
    // 模型 8s + 30s = 38s / 64s
    expect(screen.getByText('59%')).toBeTruthy();
    // 工具 2s + 2s = 4s / 64s → 6%
    expect(screen.getByText('6%')).toBeTruthy();

    cleanup();
    render(
      <TraceLanes
        lanes={{ model: LANES.model, tool: [], memory: [] }}
        activeStepId={null}
        onSelect={() => {}}
      />,
    );
    expect(screen.getAllByText('空闲').length).toBeGreaterThan(0);
  });

  it('刻度和总长按窗口末位对齐', () => {
    render(<TraceLanes lanes={LANES} activeStepId={null} onSelect={() => {}} />);
    expect(screen.getByText('总长 1m4s')).toBeTruthy();
    // 0 / 16s / 32s / 48s / 1m4s
    expect(screen.getAllByText('16s').length).toBeGreaterThan(0);
  });

  it('点块回到对应步骤，当前步骤高亮', () => {
    const onSelect = vi.fn();
    const { container, rerender } = render(
      <TraceLanes lanes={LANES} activeStepId="tool:1" onSelect={onSelect} />,
    );
    const bars = Array.from(container.querySelectorAll<HTMLButtonElement>('.lane-bar'));
    expect(bars[2].className).toContain('active');
    fireEvent.click(bars[2]);
    expect(onSelect).toHaveBeenCalledWith('tool:1');

    rerender(
      <TraceLanes lanes={{ model: [], tool: [], memory: [] }} activeStepId={null} onSelect={onSelect} />,
    );
    expect(screen.getByText('这一轮还没有可绘制的时间区间。')).toBeTruthy();
  });
});
