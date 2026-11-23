"""`python -m devops_agent.eval` 入口。"""

from __future__ import annotations

import sys

from devops_agent.eval.cli import main

if __name__ == "__main__":
    sys.exit(main())
