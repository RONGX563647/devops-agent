/** Markdown 正文渲染：GFM 语法 + 代码块复制。不启用原始 HTML，避免注入。 */
import { memo, useRef, useState, type ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

/** 代码块：右上角复制按钮，hover 才显形。 */
function CodeBlock({ children }: { children?: ReactNode }) {
  const ref = useRef<HTMLPreElement | null>(null);
  const [copied, setCopied] = useState(false);

  async function copy() {
    const text = ref.current?.textContent ?? '';
    try {
      await navigator.clipboard?.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      // 非安全上下文（http + 非 localhost）拿不到剪贴板，静默跳过
    }
  }

  return (
    <div className="code-wrap">
      <pre ref={ref}>{children}</pre>
      <button type="button" className="code-copy" onClick={() => void copy()} aria-label="复制代码">
        {copied ? '已复制' : '复制'}
      </button>
    </div>
  );
}

/** 表格：窄屏可横向滚动，不撑破阅读列。 */
function Table({ children }: { children?: ReactNode }) {
  return (
    <div className="table-wrap">
      <table>{children}</table>
    </div>
  );
}

/** 站外链接新窗口打开，站内锚点保持原行为。 */
function Link({ href, children }: { href?: string; children?: ReactNode }) {
  const external = Boolean(href && /^https?:/i.test(href));
  return (
    <a href={href} {...(external ? { target: '_blank', rel: 'noreferrer noopener' } : {})}>
      {children}
    </a>
  );
}

// 常量放在组件外：插件与映射对象的引用稳定，流式重渲染时不必反复重建
const REMARK_PLUGINS = [remarkGfm];
const COMPONENTS = { pre: CodeBlock, table: Table, a: Link };

interface Props {
  content: string;
}

export const Markdown = memo(function Markdown({ content }: Props) {
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={REMARK_PLUGINS} components={COMPONENTS}>
        {content}
      </ReactMarkdown>
    </div>
  );
});
