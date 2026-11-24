/**
 * 运行详情页 —— 只负责页面外壳，内容全部复用 `workspace/RunTrace`。
 *
 * 这里原本有一份「概览 + 健康分条 + 瀑布图」的复制实现，与轨迹 tab 里的
 * RunTrace 各改各的：健康分口径升级时两处都要改，且详情页少了检查面板。
 * 现在统一走 RunTrace，加指标只需要改一处。
 */
import { Link, useParams } from 'react-router-dom';
import { RunTrace } from '../workspace/RunTrace';

export function RunDetail() {
  const { id } = useParams();
  const runId = Number(id);

  return (
    <div className="page">
      <h1>Trace 详情 · run #{runId}</h1>
      <p className="subtitle">
        <Link to="/runs">← 运行列表</Link>
      </p>
      <RunTrace runId={runId} />
    </div>
  );
}
