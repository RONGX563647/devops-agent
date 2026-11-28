// @vitest-environment jsdom
/** 能力来源与发现状态的文案：后端新增来源/状态码时界面必须降级而不是崩（A44）。 */
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import {
  UNKNOWN_SOURCE,
  discoveryText,
  matchLabel,
  modelOptionLabel,
  sourceLabel,
  sourceNeedsCalibration,
  statusLabel,
} from './capability';
import { SourceBadge } from './Row';

afterEach(cleanup);

describe('sourceLabel', () => {
  it.each([
    ['session', '本会话'],
    ['stored', '部署默认'],
    ['yaml', '配置文件'],
    ['framework-default', '未配置'],
    ['calibrated', '校准表'],
    ['discovered', '网关声明'],
    ['card', '内置卡'],
  ])('%s → %s', (source, label) => {
    expect(sourceLabel(source)).toBe(label);
  });

  it('认不出的来源显示「未知来源」，不是原值也不是空白', () => {
    // 后端加了新来源而前端还没发版时，界面必须说「我不认识」，
    // 而不是把 `future` 这种内部标识直接怼给用户，更不能渲染成空串。
    expect(sourceLabel('future')).toBe(UNKNOWN_SOURCE);
    expect(sourceLabel('')).toBe(UNKNOWN_SOURCE);
    expect(sourceLabel(undefined)).toBe(UNKNOWN_SOURCE);
  });
});

describe('SourceBadge', () => {
  it('未知来源仍然渲染出徽标', () => {
    const { container } = render(<SourceBadge source="future" />);
    expect(screen.getByText(UNKNOWN_SOURCE)).toBeTruthy();
    expect(container.querySelector('.mc-src.is-future')).not.toBeNull();
  });

  it('能力来源用虚线档，与「谁设的」区分开', () => {
    render(<SourceBadge source="card" />);
    expect(screen.getByText('内置卡')).toBeTruthy();
    expect(screen.getByText('内置卡').className).toContain('is-card');
  });

  it('草稿未保存时优先说「待保存」', () => {
    render(<SourceBadge source="stored" pending />);
    expect(screen.getByText('待保存')).toBeTruthy();
  });
});

describe('sourceNeedsCalibration', () => {
  it('能力合并的三个来源不算未校准', () => {
    for (const source of ['calibrated', 'discovered', 'card', 'session', 'stored']) {
      expect(sourceNeedsCalibration(source)).toBe(false);
    }
  });

  it('框架默认与未知来源算未校准', () => {
    for (const source of ['framework-default', 'yaml', 'env', '', 'future']) {
      expect(sourceNeedsCalibration(source)).toBe(true);
    }
  });
});

describe('matchLabel / statusLabel', () => {
  it('匹配方式说人话', () => {
    expect(matchLabel('exact')).toBe('精确匹配');
    expect(matchLabel('prefix')).toBe('前缀匹配');
    expect(matchLabel('none')).toBe('未匹配');
    expect(matchLabel(undefined)).toBe('未匹配');
    // 未知值原样透出：比显示「未匹配」诚实（它其实是匹配上了，只是说法不认识）
    expect(matchLabel('semantic')).toBe('semantic');
  });

  it('模型状态说人话', () => {
    expect(statusLabel('active')).toBe('可用');
    expect(statusLabel('deprecated')).toBe('已弃用');
    expect(statusLabel('')).toBe('未声明状态');
    expect(statusLabel('retired')).toBe('retired');
  });
});

describe('discoveryText', () => {
  it('成功时给数量，并说明是不是旧缓存', () => {
    expect(discoveryText('ok', 12, false)).toBe('12 个模型');
    expect(discoveryText('ok', 12, true)).toContain('上次成功');
  });

  it.each([
    ['unauthorized', '401'],
    ['unsupported', 'manual'],
    ['rate_limited', '429'],
    ['unavailable', '超时'],
    ['empty', '空列表'],
    ['malformed', '格式'],
    ['not_configured', 'key'],
  ])('%s 的文案给出下一步：%s', (code, keyword) => {
    expect(discoveryText(code, 0, false)).toContain(keyword);
  });

  it('未知状态码不抛错', () => {
    expect(discoveryText('weird', 0, false)).toContain('weird');
  });
});

describe('modelOptionLabel', () => {
  const base = {
    id: 'qwen-flash',
    label: 'Qwen-Flash',
    created_at: null,
    context_size: 1_000_000,
    output_size: 32_768,
    input_types: ['text/plain'],
    status: 'active',
    origin: 'manual',
  };

  it('名字与 id 不同时两个都显示，并带上窗口', () => {
    expect(modelOptionLabel(base)).toBe('Qwen-Flash（qwen-flash） · 1,000,000');
  });

  it('没有窗口就不编一个 0 出来', () => {
    expect(modelOptionLabel({ ...base, label: 'qwen-flash', context_size: null })).toBe(
      'qwen-flash',
    );
  });
});
