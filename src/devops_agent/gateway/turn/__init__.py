"""一轮 run 的机制：编排、租约、收尾决策、端口、输入准备、保活。

包内依赖单向：`orchestrate` 用到其余全部；其余只依赖叶子层（`contract` / `settings`），
彼此之间只有 `lease → ports` 一条线，所以没有循环可言。

对外不 re-export：路由层按需 import 具体模块（`from ...turn.orchestrate import run_turn`），
避免包级 `__init__` 变成隐式的第二条公开面。
"""
