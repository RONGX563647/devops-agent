"""`python -m devops_agent.workspace` —— 与服务层共用同一组函数，不复制业务逻辑。

输出一律 JSON（便于脚本消费）；错误走 stderr 且退出码非零。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from typing import Any

from devops_agent.config import load_workspace
from devops_agent.workspace import errors
from devops_agent.workspace.service import WorkspaceService


def _service() -> WorkspaceService:
    return WorkspaceService(load_workspace())


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workspace", description="工作区管理（SPEC-0021）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出工作区")
    sub.add_parser("status", help="查看详情").add_argument("id")

    create = sub.add_parser("create", help="创建工作区")
    create.add_argument("--name", required=True)
    create.add_argument("--local", default="", help="引用本地目录（原地引用）")
    create.add_argument("--git", default="", help="从远端克隆")
    create.add_argument("--branch", default="")
    create.add_argument("--credential", default="", help="secrets 里的凭据名")
    create.add_argument("--profile", default=None, help="默认权限档位")

    bind = sub.add_parser("bind", help="把会话绑定到工作区")
    bind.add_argument("id")
    bind.add_argument("--session", required=True)

    archive = sub.add_parser("archive", help="归档")
    archive.add_argument("id")

    delete = sub.add_parser("delete", help="删除")
    delete.add_argument("id")
    delete.add_argument("--force", action="store_true")
    delete.add_argument("--keep-memory", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口；返回进程退出码。"""
    args = _build_parser().parse_args(argv)
    service = _service()
    try:
        if args.command == "list":
            _print(service.list())
        elif args.command == "status":
            _print(service.detail(args.id))
        elif args.command == "create":
            if bool(args.local) == bool(args.git):
                raise errors.WorkspaceError(errors.SOURCE_INVALID, "--local 与 --git 必须二选一")
            if args.local:
                _print(asdict(service.create_local(args.name, args.local, args.profile)))
            else:
                _print(
                    asdict(
                        service.create_git(
                            args.name,
                            args.git,
                            branch=args.branch,
                            credential_ref=args.credential,
                            default_profile=args.profile,
                        )
                    )
                )
        elif args.command == "bind":
            _print(asdict(service.bind_session(args.session, args.id)))
        elif args.command == "archive":
            _print(asdict(service.archive(args.id)))
        elif args.command == "delete":
            # --keep-memory 是 P2：当前删除工作区即清理工作区 scope 的长期条目
            _print(service.delete(args.id, force=args.force))
    except errors.WorkspaceError as exc:
        print(json.dumps({"error": exc.code, "message": exc.message, **exc.extra}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
