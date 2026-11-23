import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { Fragment, useState } from 'react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { fetchEvalTrends, fetchRuns, postFeedback } from '../api/client';
import { EvalBatches } from './EvalBatches';

type QualityView = 'feedback' | 'batches';

export function Quality() {
  const queryClient = useQueryClient();
  const trends = useQuery({ queryKey: ['eval-trends'], queryFn: fetchEvalTrends, refetchInterval: 10000 });
  const runs = useQuery({ queryKey: ['runs', 'quality'], queryFn: () => fetchRuns({ limit: 10 }) });
  const [view, setView] = useState<QualityView>('feedback');
  const [feedbackRun, setFeedbackRun] = useState<number | null>(null);
  const [comment, setComment] = useState('');
  const [message, setMessage] = useState('');

  const avgData = Object.entries(trends.data?.averages ?? {}).map(([dim, value]) => ({
    dimension: dim,
    score: value,
  }));
  const runItems = runs.data?.items ?? [];

  async function submit(score: number) {
    if (feedbackRun === null) return;
    try {
      await postFeedback(feedbackRun, score, comment);
      setMessage(`已记录反馈：run #${feedbackRun} = ${score === 1 ? '👍' : '👎'}`);
      setComment('');
      setFeedbackRun(null);
      await queryClient.invalidateQueries({ queryKey: ['eval-trends'] });
    } catch (err) {
      setMessage(`反馈失败：${err instanceof Error ? err.message : String(err)}`);
    }
  }

  return (
    <div className="page">
      <h1>质量评估</h1>
      <p className="subtitle">correctness / performance / safety 三维评分 · 人工反馈进入 eval_scores</p>

      <div className="view-tabs" role="tablist" aria-label="质量视图">
        <button
          type="button"
          role="tab"
          aria-selected={view === 'feedback'}
          className={`view-tab${view === 'feedback' ? ' active' : ''}`}
          onClick={() => setView('feedback')}
        >
          线上反馈
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={view === 'batches'}
          className={`view-tab${view === 'batches' ? ' active' : ''}`}
          onClick={() => setView('batches')}
        >
          评测批次
        </button>
      </div>

      {view === 'feedback' && (
        <Fragment>
          {trends.isError && <p className="query-error">质量趋势加载失败，稍后自动重试。</p>}

          <div className="panel-grid">
        <section className="panel">
          <h2>各维度平均分</h2>
          {trends.isLoading && avgData.length === 0 && (
            <p className="query-loading">正在加载评分趋势…</p>
          )}
          {!trends.isLoading && !trends.isError && avgData.length === 0 && (
            <p className="muted">还没有评分数据。</p>
          )}
          {avgData.length > 0 && (
            <div className="chart-wrap">
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={avgData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e5e5e5" />
                  <XAxis dataKey="dimension" fontSize={12} />
                  <YAxis domain={[0, 1]} fontSize={12} />
                  <Tooltip />
                  <Bar dataKey="score" name="平均分" fill="#3b82f6" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
          <p className="hint">共 {trends.data?.count ?? 0} 条评分 · 来源：{JSON.stringify(trends.data?.by_source ?? {})}</p>
        </section>

        <section className="panel">
          <h2>人工反馈</h2>
          <p className="hint">选择一次运行，给出 👍（correctness=1）/ 👎（correctness=0）</p>
          {runs.isError && <p className="query-error">运行列表加载失败。</p>}
          {runs.isLoading && runItems.length === 0 && (
            <p className="query-loading">正在加载运行列表…</p>
          )}
          {!runs.isLoading && !runs.isError && runItems.length === 0 && (
            <p className="muted">暂无可评价的运行。</p>
          )}
          {runItems.map((run) => (
            <div className="feedback-row" key={run.id}>
              <Link to={`/runs/${run.id}`}>#{run.id} {run.session_id}</Link>
              <span className={`badge badge-${run.status}`}>{run.status}</span>
              <button onClick={() => setFeedbackRun(run.id)}>评价</button>
            </div>
          ))}
          {feedbackRun !== null && (
            <div className="feedback-box">
              <div>正在评价 run #{feedbackRun}</div>
              <input
                aria-label="评价备注"
                value={comment}
                onChange={(e) => setComment(e.target.value)}
                placeholder="可选备注"
              />
              <div className="feedback-actions">
                <button onClick={() => void submit(1)}>👍 好</button>
                <button onClick={() => void submit(0)}>👎 差</button>
                <button onClick={() => setFeedbackRun(null)}>取消</button>
              </div>
            </div>
          )}
          {message && <p className="hint">{message}</p>}
        </section>
          </div>
        </Fragment>
      )}

      {view === 'batches' && <EvalBatches />}
    </div>
  );
}
