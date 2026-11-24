/**
 * 评测集面板 —— 让「基数」可见。
 *
 * 光看 `pass_rate 73%` 没有意义：15 条玩具题与 40 条覆盖四档位的题库，
 * 同一个数字的含义完全不同。这里把题库构成、断言分布与每条用例的断言摊开，
 * 恒真断言（`dead`）单独着色 —— 静态检查的前端出口就在这张图上。
 */
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { fetchEvalSuites } from '../../api/client';
import type { EvalSuite } from '../../types';
import { RatioBar, TONE, type RatioSegment } from './charts';

const SCENARIOS: { key: string; label: string; tone: string }[] = [
  { key: 'common', label: '常规', tone: TONE.accent },
  { key: 'variant', label: '变体', tone: '#7c3aed' },
  { key: 'edge', label: '边界', tone: TONE.warn },
  { key: 'adversarial', label: '对抗', tone: TONE.bad },
];

const ASSERTION_LABEL: Record<string, string> = {
  must_contain: '必含事实值',
  must_contain_any: '同义组',
  must_not_contain: '必不含',
  must_not_regex: '禁止形状',
  must_cite: '行号定位',
  regex: '正则',
  file_state: '文件内容',
  tool_must: '必用工具',
  tool_forbid: '禁用工具',
  tool_budget: '调用预算',
  dead: '恒真（无效）',
};

const ASSERTION_TONE: Record<string, string> = {
  must_cite: TONE.good,
  must_contain_any: TONE.good,
  must_not_regex: TONE.warn,
  dead: TONE.bad,
};

function scenarioSegments(suite: EvalSuite): RatioSegment[] {
  return SCENARIOS.map((item) => ({
    label: item.label,
    value: suite.counts[item.key] ?? 0,
    tone: item.tone,
  }));
}

function assertionSegments(suite: EvalSuite): RatioSegment[] {
  return Object.entries(suite.assertions)
    .filter(([, value]) => value > 0)
    .map(([key, value]) => ({
      label: ASSERTION_LABEL[key] ?? key,
      value,
      tone: ASSERTION_TONE[key] ?? TONE.muted,
    }));
}

function totalAssertions(suite: EvalSuite): number {
  return Object.values(suite.assertions).reduce((sum, value) => sum + value, 0);
}

function SuiteBody({ suite }: { suite: EvalSuite }) {
  const [openCase, setOpenCase] = useState<string | null>(null);
  const dead = suite.assertions.dead ?? 0;
  const total = totalAssertions(suite);
  const balanced = Object.entries(suite.counts)
    .map(([key, value]) => `${key} ${value}`)
    .join(' / ');

  return (
    <>
      {dead > 0 && (
        <p className="quality-alert" role="status">
          有 {dead} / {total} 条断言恒真：它们与该档配置绑定，模型怎么做都通过。
          用 <code>python -m devops_agent.eval prompt-lint</code> 可以看到具体条目。
        </p>
      )}

      <div className="panel-grid">
        <section className="panel">
          <h2>场景配比</h2>
          <RatioBar
            segments={scenarioSegments(suite)}
            caption={`共 ${suite.cases.length} 条用例，场景分布 ${balanced}。`}
          />
        </section>

        <section className="panel">
          <h2>断言构成</h2>
          <RatioBar
            segments={assertionSegments(suite)}
            caption={`共 ${total} 条断言；其中行号定位 ${suite.assertions.must_cite ?? 0} 条，恒真 ${dead} 条。`}
          />
        </section>
      </div>

      <section className="panel">
        <h2>用例清单</h2>
        <p className="hint">
          点开一条看它到底断言了什么 —— 断言读不懂的用例，分数也不可信。
        </p>
        <ul className="suite-cases">
          {suite.cases.map((item) => {
            const expanded = openCase === item.id;
            return (
              <li key={item.id} className="suite-case">
                <button
                  type="button"
                  className="suite-case-head"
                  aria-expanded={expanded}
                  onClick={() => setOpenCase(expanded ? null : item.id)}
                >
                  <code>{item.id}</code>
                  <span className="suite-case-meta">
                    {item.permission} · {item.subject} · {item.assertions.length} 条断言
                  </span>
                </button>
                {expanded && (
                  <ul className="suite-assertions">
                    {item.assertions.map((assertion, index) => (
                      <li key={`${assertion.kind}-${index}`}>
                        <span className="assertion-kind">
                          {ASSERTION_LABEL[assertion.kind] ?? assertion.kind}
                        </span>
                        <code>{assertion.target}</code>
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            );
          })}
        </ul>
      </section>
    </>
  );
}

export function SuitePanel() {
  const suites = useQuery({ queryKey: ['eval-suites'], queryFn: fetchEvalSuites });

  if (suites.isError) {
    return <p className="query-error">评测集加载失败，稍后自动重试。</p>;
  }
  if (suites.isLoading) {
    return <p className="query-loading">正在加载评测集…</p>;
  }
  const suite = suites.data?.items[0] ?? null;
  if (!suite) {
    return <p className="muted">仓库里没有找到评测集文件（eval/suites/*.jsonl）。</p>;
  }

  return (
    <div className="suite-panel">
      <section className="panel">
        <h2>
          {suite.suite}@{suite.version}
        </h2>
        <p className="hint">
          sha {suite.sha256.slice(0, 8)} · grader {suite.grader_version || '未记录'} · 门禁样本量{' '}
          {suite.min_cases_for_gate}
          {suite.cases.length < suite.min_cases_for_gate
            ? `（当前 ${suite.cases.length} 条，未达门槛）`
            : ''}
        </p>
        <ul className="threshold-list">
          {Object.entries(suite.thresholds).map(([key, spec]) => (
            <li key={key}>
              <code>{key}</code> ≥ {spec.min}
              {spec.min_samples > 0 ? `（至少 ${spec.min_samples} 条样本）` : ''}
            </li>
          ))}
        </ul>
      </section>
      <SuiteBody suite={suite} />
    </div>
  );
}
