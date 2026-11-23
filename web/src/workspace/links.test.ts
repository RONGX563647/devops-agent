import { describe, expect, it } from 'vitest';
import { extractLinks, trimTrailingPunctuation } from './links';

describe('extractLinks', () => {
  it('识别普通链接并去掉句末标点', () => {
    const links = extractLinks('看下 https://example.com/docs?x=1。');
    expect(links).toEqual([
      { url: 'https://example.com/docs?x=1', label: 'example.com/docs', kind: 'web' },
    ]);
  });

  it('GitHub 仓库显示 owner/repo 并标记为 github', () => {
    const links = extractLinks('https://github.com/RONGX563647/devops-agent/issues/1');
    expect(links[0].kind).toBe('github');
    expect(links[0].label).toBe('RONGX563647/devops-agent');
    expect(links[0].url).toBe('https://github.com/RONGX563647/devops-agent/issues/1');
  });

  it('git 引用优先于等价 URL，且只保留一条', () => {
    const links = extractLinks(
      'git@github.com:RONGX563647/devops-agent.git 与 https://github.com/RONGX563647/devops-agent',
    );
    expect(links).toHaveLength(1);
    expect(links[0].url).toBe('https://github.com/RONGX563647/devops-agent');
  });

  it('省略协议的 github.com/owner/repo 也能识别并去重', () => {
    const links = extractLinks('github.com/a/b 和 https://github.com/a/b');
    expect(links).toHaveLength(1);
    expect(links[0].url).toBe('https://github.com/a/b');
  });

  it('最多返回 5 条，按首次出现顺序', () => {
    const text = [
      'https://a.com/1',
      'https://a.com/2',
      'https://a.com/3',
      'https://a.com/4',
      'https://a.com/5',
      'https://a.com/6',
    ].join(' ');
    const links = extractLinks(text);
    expect(links.map((link) => link.url)).toEqual([
      'https://a.com/1',
      'https://a.com/2',
      'https://a.com/3',
      'https://a.com/4',
      'https://a.com/5',
    ]);
  });

  it('没有链接时返回空数组', () => {
    expect(extractLinks('帮我查下 CI 为什么挂了')).toEqual([]);
  });
});

describe('trimTrailingPunctuation', () => {
  it('剥掉句末中英文标点与不成对的右括号', () => {
    expect(trimTrailingPunctuation('https://example.com/a).')).toBe('https://example.com/a');
    expect(trimTrailingPunctuation('https://example.com/a，')).toBe('https://example.com/a');
  });

  it('保留成对括号（如 Wikipedia 路径）', () => {
    expect(trimTrailingPunctuation('https://example.com/a_(b)')).toBe('https://example.com/a_(b)');
  });
});
