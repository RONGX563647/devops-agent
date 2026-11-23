// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import type { ProcessEntry } from '../types';
import { formatDuration, ProcessBlock } from './ProcessBlock';

const steps: ProcessEntry[] = [
  { id: 'reply:1', kind: 'lifecycle', label: '开始回复', detail: '', state: 'success' },
  { id: 'hint:1', kind: 'lifecycle', label: '分析提示 · reasoning', detail: '先看画面', state: 'success' },
  { id: 'thinking:1', kind: 'thinking', label: '思考中', detail: '先看清问题', state: 'success' },
  { id: 'tool:t1', kind: 'tool', label: '调用工具 · bash', detail: 'ls -la', state: 'success' },
  { id: 'control:1', kind: 'control', label: '等待用户确认', detail: '', state: 'running' },
  { id: 'error:1', kind: 'error', label: '达到最大迭代次数', detail: '', state: 'error' },
  { id: 'text:1', kind: 'text', label: '正文输出', detail: '结果', state: 'success' },
];

function renderBlock(overrides: Partial<Parameters<typeof ProcessBlock>[0]> = {}) {
  return render(
    <ProcessBlock
      steps={steps}
      running={false}
      modelName="qwen3.8-flash"
      inputTokens={10}
      outputTokens={20}
      startedAt="2026-11-23T20:00:00Z"
      endedAt="2026-11-23T20:00:04Z"
      {...overrides}
    />,
  );
}

describe('formatDuration', () => {
  it('计算秒数', () => {
    expect(formatDuration('2026-11-23T20:00:00Z', '2026-11-23T20:00:04Z')).toBe('4.0s');
  });
  it('缺时间戳返回空串', () => {
    expect(formatDuration(null, '2026-11-23T20:00:04Z')).toBe('');
    expect(formatDuration('bad', 'worse')).toBe('');
  });
});

describe('ProcessBlock', () => {
  it('每类步骤渲染不同标注标签', () => {
    renderBlock();
    for (const tag of ['系统', '思考', '工具', '控制', '异常', '正文']) {
      expect(screen.getAllByText(tag).length).toBeGreaterThan(0);
    }
  });

  it('每类步骤带 data-kind 便于样式区分', () => {
    const { container } = renderBlock();
    const kinds = Array.from(container.querySelectorAll('.process-step')).map((el) =>
      el.getAttribute('data-kind'),
    );
    expect(kinds).toEqual(['lifecycle', 'lifecycle', 'thinking', 'tool', 'control', 'error', 'text']);
  });

  it('显示模型、用时、token 汇总', () => {
    renderBlock();
    expect(screen.getByText(/qwen3\.8-flash/)).toBeTruthy();
    expect(screen.getByText(/用时 4\.0s/)).toBeTruthy();
    expect(screen.getByText(/输入 token 10 \/ 输出 token 20/)).toBeTruthy();
  });

  it('运行中显示执行中标题', () => {
    renderBlock({ running: true });
    expect(screen.getByText('执行中…')).toBeTruthy();
  });

  it('折叠后隐藏步骤、展开后恢复', () => {
    const { container } = renderBlock();
    expect(container.querySelectorAll('.process-step').length).toBe(7);
    fireEvent.click(screen.getByRole('button', { name: /收起/ }));
    expect(container.querySelectorAll('.process-step').length).toBe(0);
    fireEvent.click(screen.getByRole('button', { name: /展开/ }));
    expect(container.querySelectorAll('.process-step').length).toBe(7);
  });

  it('无步骤时不渲染', () => {
    const { container } = renderBlock({ steps: [] });
    expect(container.firstChild).toBeNull();
  });
});

afterEach(cleanup);
