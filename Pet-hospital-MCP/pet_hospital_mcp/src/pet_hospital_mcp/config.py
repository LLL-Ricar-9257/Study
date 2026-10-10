"""运行配置：全部来自环境变量，带安全默认值。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| ``PET_HOSPITAL_BASE_URL`` | ``http://127.0.0.1:8080`` | Go 宠物医院 REST API 根地址 |
| ``MCP_HOST`` | ``127.0.0.1`` | MCP 服务监听地址 |
| ``MCP_PORT`` | ``8765`` | MCP 服务监听端口 |
| ``MCP_PATH`` | ``/mcp`` | Streamable HTTP 的 MCP 端点路径 |
| ``MCP_REQUEST_TIMEOUT`` | ``10.0`` | 单次上游请求超时（秒） |
| ``MCP_MAX_RETRIES`` | ``2`` | 上游失败后的额外重试次数 |
| ``MCP_RETRY_BACKOFF`` | ``0.2`` | 首次退避秒数，后续按倍数递增 |
| ``MCP_LOG_LEVEL`` | ``INFO`` | 日志级别 |
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["Settings", "DEFAULT_PET_HOSPITAL_BASE_URL", "DEFAULT_MCP_HOST", "DEFAULT_MCP_PORT"]

DEFAULT_PET_HOSPITAL_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_MCP_HOST = "127.0.0.1"
DEFAULT_MCP_PORT = 8765
DEFAULT_MCP_PATH = "/mcp"


@dataclass(frozen=True, slots=True)
class Settings:
    """一次性解析好的运行配置。"""

    pet_hospital_base_url: str = DEFAULT_PET_HOSPITAL_BASE_URL
    host: str = DEFAULT_MCP_HOST
    port: int = DEFAULT_MCP_PORT
    mcp_path: str = DEFAULT_MCP_PATH
    request_timeout: float = 10.0
    max_retries: int = 2
    retry_backoff: float = 0.2
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """从环境变量构造配置；非法数值回落到默认值并保持可预测。"""
        source = os.environ if env is None else env
        return cls(
            pet_hospital_base_url=_clean_base_url(
                source.get("PET_HOSPITAL_BASE_URL", DEFAULT_PET_HOSPITAL_BASE_URL)
            ),
            host=source.get("MCP_HOST", DEFAULT_MCP_HOST).strip() or DEFAULT_MCP_HOST,
            port=_int(source.get("MCP_PORT"), DEFAULT_MCP_PORT, minimum=1),
            mcp_path="/" + source.get("MCP_PATH", DEFAULT_MCP_PATH).strip().lstrip("/"),
            request_timeout=_float(source.get("MCP_REQUEST_TIMEOUT"), 10.0, minimum=0.001),
            max_retries=_int(source.get("MCP_MAX_RETRIES"), 2, minimum=0),
            retry_backoff=_float(source.get("MCP_RETRY_BACKOFF"), 0.2, minimum=0.0),
            log_level=source.get("MCP_LOG_LEVEL", "INFO").strip().upper() or "INFO",
        )


def _clean_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    return value or DEFAULT_PET_HOSPITAL_BASE_URL


def _int(value: str | None, default: int, *, minimum: int | None = None) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value.strip())
    except ValueError:
        return default
    if minimum is not None and parsed < minimum:
        return default
    return parsed


def _float(value: str | None, default: float, *, minimum: float) -> float:
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value.strip())
    except ValueError:
        return default
    if parsed != parsed or parsed < minimum:  # NaN / 越界
        return default
    return parsed