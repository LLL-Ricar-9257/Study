"""TUI 命令行入口。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from core import (
    ConfigFileCreatedError,
    ConfigurationError,
    XAgentError,
    __version__,
    build_agent,
    configure_logging,
    discover_model_metadata,
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
        help="配置文件路径，默认为当前目录或项目根目录的 config.ini",
    )
    parser.add_argument(
        "--mcp-config",
        metavar="FILE",
        help="MCP 配置文件路径，默认为启动目录下的 .mcp.json",
    )
    parser.add_argument(
        "--no-mcp",
        action="store_true",
        help="本次启动不连接任何 MCP Server",
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
    except ConfigFileCreatedError as exc:
        # 当前目录没有配置：已经生成模板，提示用户修改后重启。
        from .dialog import show_notice_dialog

        show_notice_dialog(
            "已生成新的 config.ini",
            "当前目录没有配置文件，xAgent 已生成一份完整模板：\n\n"
            f"{exc.path}\n\n"
            "请把 [llm] api_key 改成真实值（其余选项可按需调整），"
            "然后重新启动 xAgent。",
            button="退出",
        )
        return 2
    except ConfigurationError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2
    except XAgentError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2

    mcp_settings = config.mcp.to_settings()
    if args.mcp_config:
        mcp_settings = replace(mcp_settings, config_path=args.mcp_config)
    if args.no_mcp:
        mcp_settings = replace(mcp_settings, enabled=False)

    # 调用日志落盘：目录不可写时降级为无日志，不影响启动。
    log_path = configure_logging(config.logs, project_root=Path.cwd())
    if log_path is not None:
        print(f"调用日志：{log_path}", file=sys.stderr)

    # 上下文窗口等信息必须来自当前模型的上游接口，不能依赖本地配置。
    model_metadata = discover_model_metadata(
        config.llm,
        timeout=config.tools.model_info_timeout_seconds,
    )

    try:
        agent = build_agent(
            config,
            mcp_settings=mcp_settings,
            model_metadata=model_metadata,
        )
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
        FileAgentApp(
            agent=agent,
            config=config,
            model_metadata=model_metadata,
        ).run()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
