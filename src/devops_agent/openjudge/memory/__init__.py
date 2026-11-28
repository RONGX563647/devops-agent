"""记忆效果评测（SPEC-0014）。

本包复用 openjudge 的题库/统计/门禁口径，只新增：
- 题库解析与变体派生（models / suite / variants）；
- 确定性执行器（harness）与指标、门禁、报告（metrics / gates / report）；
- 专用表与接口（schema / store / runner / api）。

P1/P2 不依赖模型 Key：轨迹由 MemoryRuntime 直接驱动，判分全部是确定性规则。
"""
