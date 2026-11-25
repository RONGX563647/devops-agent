/**
 * 题库面板：列出「仓库 + 本地覆盖层」合并后的用例，点开即可改。
 *
 * 列表里一定要标来源：分不清「这条是我改过的」和「这条是原版」时，用户会反复
 * 改同一条用例并怀疑没生效。`overlay` 徽标 + 顶部计数就是为此。
 */
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { fetchOpenJudgeLibrary } from '../../../api/client';
import { CaseEditor } from './CaseEditor';

const TRACK_LABEL: Record<string, string> = {
  A: 'A 仓库问答',
  B: 'B 权限边界',
  C: 'C 改动能力',
  D: 'D 输入管线',
  E: 'E 多轮会话',
};

export function CasesPanel() {
  const [track, setTrack] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const query = useQuery({
    queryKey: ['oj-library', track],
    queryFn: () => fetchOpenJudgeLibrary({ track: track || undefined }),
  });

  if (query.isLoading) return <div className="muted">加载题库…</div>;
  if (query.error || !query.data) return <div className="muted">题库不可用</div>;

  const { items, total, suite, limits } = query.data;

  return (
    <section className="panel oj-library">
      <div className="oj-library-head">
        <h3>题库</h3>
        <span className="muted">
          {total} 条 · 指纹 {suite.suite_sha.slice(0, 12) || '—'}
          {suite.overlay > 0 && ` · 本地改动 ${suite.overlay} 条`}
          {suite.deleted > 0 && ` · 已隐藏 ${suite.deleted} 条`}
        </span>
      </div>

      <div className="tag-row">
        <button
          type="button"
          className={`view-tab${track === '' ? ' active' : ''}`}
          onClick={() => setTrack('')}
        >
          全部
        </button>
        {limits.tracks.map((key) => (
          <button
            key={key}
            type="button"
            className={`view-tab${track === key ? ' active' : ''}`}
            onClick={() => setTrack(key)}
          >
            {TRACK_LABEL[key] ?? key}
          </button>
        ))}
      </div>

      <div className="oj-library-body">
        <table className="data-table">
          <thead>
            <tr>
              <th>用例</th>
              <th>赛道</th>
              <th>风险</th>
              <th>档位</th>
              <th>来源</th>
            </tr>
          </thead>
          <tbody>
            {items.map((row) => (
              <tr
                key={row.id}
                className={selected === row.id ? 'case-row is-selected' : 'case-row'}
                onClick={() => setSelected(row.id)}
              >
                <td>{row.id}</td>
                <td>{row.track}</td>
                <td>{row.risk}</td>
                <td>{row.permission}</td>
                <td>
                  <span className={row.source === 'overlay' ? 'oj-badge overlay' : 'oj-badge'}>
                    {row.source === 'overlay' ? (row.edited ? '改过' : '新增') : '仓库'}
                  </span>
                </td>
              </tr>
            ))}
            {items.length === 0 && (
              <tr>
                <td colSpan={5} className="muted">
                  这个赛道没有用例
                </td>
              </tr>
            )}
          </tbody>
        </table>

        {selected && <CaseEditor caseId={selected} onClose={() => setSelected(null)} />}
      </div>
    </section>
  );
}
