"""python -m access — IAM 开发初始化 CLI（显式、opt-in）。

用法（在项目根目录，PYTHONPATH 含 src/ 或项目根）：
    PYTHONPATH=src .venv/bin/python -m access --init-dev --db data/iam.db [--force]

- 缺省 --db 为 data/iam.db（相对项目根）。
- --force 重写演示用户的角色与范围，使其与 DEMO_IDENTITIES 完全一致。
- 本 CLI 只做本地开发初始化，不涉及任何认证/口令。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Knowledge Lantern IAM 开发初始化")
    parser.add_argument(
        "--init-dev", action="store_true",
        help="用演示身份初始化（播种）IAM 库；不传则仅打印说明",
    )
    parser.add_argument(
        "--db", default=str(Path(__file__).resolve().parents[2] / "data" / "iam.db"),
        help="IAM 库路径（默认 data/iam.db）",
    )
    parser.add_argument("--force", action="store_true", help="重写演示用户角色/范围")
    args = parser.parse_args(argv)

    if not args.init_dev:
        print(
            "未指定 --init-dev；IAM 库不会被创建或修改。"
            "\n可用身份（演示/本地开发，不含任何认证材料）："
            "\n  U001 Zhang San — viewer：DIRECT + 内部检索（受 proj-alpha / finance 范围约束）"
            "\n  U002 Li Si     — admin ：DIRECT + 内部检索（全范围）"
            "\n初始化命令：python -m access --init-dev --db data/iam.db"
        )
        return 0

    from .seed import initialize_dev_iam

    counts = initialize_dev_iam(Path(args.db), force=args.force)
    print(f"IAM 开发初始化完成: {args.db}")
    print(f"  角色={counts['roles']} 用户={counts['users']} "
          f"user_roles={counts['user_roles']} user_scopes={counts['user_scopes']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
