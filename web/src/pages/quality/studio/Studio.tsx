/**
 * 「用例与模型」视图：上面配模型，下面改用例。
 *
 * 合成一个页签而不是两个，是因为这两件事总是一起发生 —— 换了被测模型就得改断言，
 * 改了题面通常也要换裁判再跑一遍。分成两个页签反而要来回切。
 */
import { CasesPanel } from './CasesPanel';
import { ModelCard } from './ModelCard';

export function Studio() {
  return (
    <div className="oj-view">
      <ModelCard />
      <CasesPanel />
    </div>
  );
}
