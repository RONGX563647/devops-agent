/** 输入框里的链接识别：纯前端实时解析，不额外请求后端。 */

export interface ExtractedLink {
  /** 规范化后的可打开地址（已去掉 git 后缀与句末标点）。 */
  url: string;
  /** 芯片上展示的短文本：GitHub 显示 owner/repo，其余显示域名 + 路径。 */
  label: string;
  kind: 'github' | 'web';
}

/** 一次最多展示几条：再多就把输入区挤成链接列表了。 */
export const MAX_LINKS = 5;

/** git 引用（git@host:owner/repo.git）先于普通 URL 匹配，去重时优先级更高。 */
const GIT_REF = /\bgit@([\w.-]+):([\w./~-]+)/g;
/** 带协议的链接；排除空白、引号与尖括号等明显不属于 URL 的字符。 */
const URL_PATTERN = /\bhttps?:\/\/[^\s<>"'`]+/gi;
/** 省略协议的 GitHub 仓库地址（github.com/owner/repo）。 */
const BARE_GITHUB = /\bgithub\.com\/[\w.-]+\/[\w.-]+/gi;

const TRAILING_PUNCT = /[.,;:!?，。；：！？、"'”’）》】]+$/;

function countOf(text: string, char: string): number {
  return text.split(char).length - 1;
}

/** 去掉句末标点；只剥掉不成对的右括号，保留 Wikipedia 这类带括号的合法路径。 */
export function trimTrailingPunctuation(raw: string): string {
  let url = raw.replace(TRAILING_PUNCT, '');
  for (const [open, close] of [
    ['(', ')'],
    ['[', ']'],
    ['{', '}'],
  ]) {
    while (url.endsWith(close) && countOf(url, close) > countOf(url, open)) {
      url = url.slice(0, -1);
    }
  }
  return url.replace(TRAILING_PUNCT, '');
}

interface Normalized {
  url: string;
  host: string;
  key: string;
}

function normalize(raw: string): Normalized | null {
  try {
    const parsed = new URL(raw);
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
    const host = parsed.hostname.replace(/^www\./, '').toLowerCase();
    let path = parsed.pathname.replace(/\.git$/i, '');
    if (path.endsWith('/')) path = path.slice(0, -1);
    const url = `${parsed.protocol}//${parsed.hostname}${path}${parsed.search}`;
    return { url, host, key: `${host}${path}${parsed.search}`.toLowerCase() };
  } catch {
    return null;
  }
}

function labelFor(link: Normalized): string {
  const parsed = new URL(link.url);
  if (link.host === 'github.com') {
    const [owner, repo] = parsed.pathname.split('/').filter(Boolean);
    if (owner && repo) return `${owner}/${repo}`;
  }
  return `${link.host}${parsed.pathname === '/' ? '' : parsed.pathname}`;
}

/**
 * 从输入文本里抽取链接。
 *
 * 顺序即优先级：git 引用 → 带协议 URL → 省略协议的 GitHub 地址；
 * 同一个目标只保留第一次出现的那条，因此 git 引用会压过等价 URL。
 */
export function extractLinks(text: string, max: number = MAX_LINKS): ExtractedLink[] {
  const found: ExtractedLink[] = [];
  const seen = new Set<string>();

  function add(raw: string): void {
    const normalized = normalize(trimTrailingPunctuation(raw));
    if (!normalized || seen.has(normalized.key)) return;
    seen.add(normalized.key);
    found.push({
      url: normalized.url,
      label: labelFor(normalized),
      kind: normalized.host === 'github.com' ? 'github' : 'web',
    });
  }

  for (const match of text.matchAll(GIT_REF)) {
    add(`https://${match[1]}/${match[2].replace(/\.git$/i, '')}`);
  }
  for (const match of text.matchAll(URL_PATTERN)) {
    add(match[0]);
  }
  for (const match of text.matchAll(BARE_GITHUB)) {
    add(`https://${match[0]}`);
  }

  return found.slice(0, max);
}
