"""TUI 命令行入口。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from core import (
    ConfigurationError,
    XAgentError,
    __version__,
    build_agent,
    load_config,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xagent",
        description="xAgent - 使用自然语言管理本地文件的终端智能体",
    )
    parser.add_argument(
        "--workspace",
        "-w",
        metavar="DIR",
        help="受控工作区，默认为当前目录",
    )
    parser.add_argument(
        "--config",
        "-c",
        metavar="FILE",
        help="配置文件路径，默认为项目根目录的 config.ini",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"xAgent {__version__}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        config = load_config(config_path=args.config, workspace=args.workspace)
    except ConfigurationError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2
    except XAgentError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2

    try:
        agent = build_agent(config)
    except Exception as exc:
        print(f"初始化 Agent 失败：{exc}", file=sys.stderr)
        return 2

    try:
        from .app import FileAgentApp
        from .terminal import configure_console_font
    except ModuleNotFoundError as exc:
        print(
            "缺少 TUI 依赖，请先运行：pip install -e ."
            f"\n原始错误：{exc}",
            file=sys.stderr,
        )
        return 2

    # cmd.exe 的默认字体常导致中英文/边框错位。失败时静默保持原字体。
    configure_console_font()

    try:
        FileAgentApp(agent=agent, config=config).run()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
