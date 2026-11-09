"""AnythingLLM REST 客户端。

只做协议搬运：鉴权、超时、重试、响应拆解、错误归类都在这里；工具层只负责
把参数交过来、把结构化结果交回去。

配置全部来自环境变量，代码里不保存任何密钥：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| ``ANYTHINGLLM_URL`` | ``http://localhost:3001`` | AnythingLLM 实例地址 |
| ``ANYTHINGLLM_API_KEY`` | 无（必填） | 设置页生成的 API Key |
| ``ANYTHINGLLM_TIMEOUT`` | ``60`` | 单次请求超时（秒） |
| ``ANYTHINGLLM_MAX_RETRIES`` | ``1`` | 超时 / 5xx / 429 的额外重试次数 |
| ``ANYTHINGLLM_RETRY_BACKOFF`` | ``0.5`` | 首次退避秒数，之后按倍数递增 |
"""

from __future__ import annotations

import asyncio
import logging
import math
import mimetypes
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import httpx

from errors import (
    ApiError,
    BackendTimeoutError,
    BackendUnavailableError,
    ConfigurationError,
    InvalidResponseError,
    ValidationError,
)

__all__ = ["MAX_UPLOAD_BYTES", "AnythingLLMClient", "Settings"]

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES: Final = 20 * 1024 * 1024
"""单个本地文件的上传上限：避免把大二进制读进内存再塞给上游。"""

_RETRYABLE_STATUS: Final = frozenset({408, 425, 429, 500, 502, 503, 504})


class Settings:
    """一次性解析好的运行配置。"""

    __slots__ = ("api_key", "base_url", "max_retries", "retry_backoff", "timeout")

    def __init__(
        self,
        base_url: str = "http://localhost:3001",
        api_key: str = "",
        *,
        timeout: float = 60.0,
        max_retries: int = 1,
        retry_backoff: float = 0.5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.retry_backoff = max(0.0, retry_backoff)

    def __repr__(self) -> str:  # 密钥不进入 repr / 日志
        return (
            f"Settings(base_url={self.base_url!r}, api_key='***', "
            f"timeout={self.timeout!r}, max_retries={self.max_retries!r})"
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        source = os.environ if env is None else env
        api_key = source.get("ANYTHINGLLM_API_KEY", "").strip()
        if not api_key:
            raise ConfigurationError(
                "缺少环境变量 ANYTHINGLLM_API_KEY。请在 AnythingLLM 设置页生成 API Key，"
                "然后写入系统环境变量或本目录的 .env 后再启动服务。",
                details={"env": "ANYTHINGLLM_API_KEY"},
            )
        return cls(
            base_url=(source.get("ANYTHINGLLM_URL", "") or "http://localhost:3001").strip(),
            api_key=api_key,
            timeout=_float(source.get("ANYTHINGLLM_TIMEOUT"), 60.0, minimum=0.1),
            max_retries=_int(source.get("ANYTHINGLLM_MAX_RETRIES"), 1, minimum=0),
            retry_backoff=_float(source.get("ANYTHINGLLM_RETRY_BACKOFF"), 0.5, minimum=0.0),
        )


def _float(value: str | None, default: float, *, minimum: float) -> float:
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value.strip())
    except ValueError:
        return default
    # NaN / inf 都会让后续超时计算失效，直接回退默认值。
    return parsed if math.isfinite(parsed) and parsed >= minimum else default


def _int(value: str | None, default: int, *, minimum: int) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value.strip())
    except ValueError:
        return default
    return default if parsed < minimum else parsed


class AnythingLLMClient:
    """异步 AnythingLLM 客户端；线程 / 协程安全（内部只读）。"""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=httpx.Timeout(settings.timeout),
            headers={"Authorization": f"Bearer {settings.api_key}"},
            transport=transport,
            follow_redirects=False,
        )

    @property
    def base_url(self) -> str:
        return str(self._client.base_url)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # 工作区
    # ------------------------------------------------------------------
    async def list_workspaces(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/api/v1/workspaces")
        workspaces = payload.get("workspaces")
        if not isinstance(workspaces, list):
            raise InvalidResponseError(
                "工作区列表缺少 workspaces 数组",
                details={"keys": sorted(payload)[:20]},
            )
        return [item for item in workspaces if isinstance(item, dict)]

    async def get_workspace(self, slug: str) -> dict[str, Any]:
        payload = await self._request("GET", f"/api/v1/workspace/{slug}")
        workspace = payload.get("workspace")
        if isinstance(workspace, list):  # 1.x 返回数组，2.x 返回对象
            workspace = workspace[0] if workspace else None
        if not isinstance(workspace, dict):
            raise InvalidResponseError(
                f"工作区 {slug} 的详情缺少 workspace 对象",
                details={"slug": slug},
            )
        return workspace

    async def create_workspace(self, name: str) -> dict[str, Any]:
        payload = await self._request("POST", "/api/v1/workspace/new", json_body={"name": name})
        workspace = payload.get("workspace")
        if isinstance(workspace, list):
            workspace = workspace[0] if workspace else None
        if not isinstance(workspace, dict):
            raise InvalidResponseError(
                "创建工作区的响应缺少 workspace 对象",
                details={"name": name},
            )
        return workspace

    # ------------------------------------------------------------------
    # 文档
    # ------------------------------------------------------------------
    async def upload_text(
        self,
        text: str,
        title: str,
        *,
        add_to_workspaces: list[str] | None = None,
    ) -> dict[str, Any]:
        body = {
            "textContent": text,
            "addToWorkspaces": ",".join(add_to_workspaces or []),
            "metadata": {"title": title},
        }
        payload = await self._request("POST", "/api/v1/document/raw-text", json_body=body)
        self._ensure_success(payload, action="上传文本")
        return payload

    async def upload_file(
        self,
        path: Path,
        *,
        add_to_workspaces: list[str] | None = None,
    ) -> dict[str, Any]:
        if not path.is_file():
            raise ValidationError(
                f"文件不存在或不是普通文件：{path}",
                details={"path": str(path)},
            )
        size = path.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise ValidationError(
                f"文件过大（{size} 字节），超过 {MAX_UPLOAD_BYTES} 字节的上传上限",
                details={"path": str(path), "size": size},
            )
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ValidationError(f"读取文件失败：{path}", details={"reason": type(exc).__name__}) from exc

        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        files = {"file": (path.name, content, mime)}
        data = {"addToWorkspaces": ",".join(add_to_workspaces or [])}
        payload = await self._request("POST", "/api/v1/document/upload", files=files, data=data)
        self._ensure_success(payload, action="上传文件")
        return payload

    async def vectorize(self, slug: str) -> dict[str, Any]:
        payload = await self._request("POST", f"/api/v1/workspace/{slug}/update-embeddings")
        self._ensure_success(payload, action="触发向量化")
        return payload

    # ------------------------------------------------------------------
    # 对话 / 检索
    # ------------------------------------------------------------------
    async def chat(
        self,
        slug: str,
        message: str,
        *,
        mode: str = "query",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"message": message, "mode": mode}
        if session_id:
            body["sessionId"] = session_id
        payload = await self._request("POST", f"/api/v1/workspace/{slug}/chat", json_body=body)
        if payload.get("error"):
            raise ApiError(
                str(payload.get("error"))[:512],
                details={"slug": slug, "mode": mode},
            )
        return payload

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _ensure_success(self, payload: Mapping[str, Any], *, action: str) -> None:
        """AnythingLLM 用 ``success: false`` + ``error`` 表达业务失败。"""
        if payload.get("success") is False or payload.get("error"):
            reason = payload.get("error") or "上游未给出原因"
            raise ApiError(f"{action}失败：{reason}", details={"action": action})

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        attempts = self.settings.max_retries + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            retryable = False
            try:
                response = await self._client.request(
                    method, path, json=json_body, data=data, files=files
                )
            except httpx.TimeoutException:
                last_error = BackendTimeoutError(
                    f"调用 AnythingLLM 超时（{self.settings.timeout:g}s）",
                    details={"path": path, "attempt": attempt, "attempts": attempts},
                )
                retryable = True
            except httpx.TransportError as exc:
                last_error = BackendUnavailableError(
                    details={
                        "path": path,
                        "attempt": attempt,
                        "attempts": attempts,
                        "reason": type(exc).__name__,
                    },
                )
                retryable = True
            else:
                if response.status_code in _RETRYABLE_STATUS:
                    last_error = ApiError(
                        f"AnythingLLM 返回 HTTP {response.status_code}",
                        details={
                            "path": path,
                            "status": response.status_code,
                            "upstream_message": _upstream_message(response),
                        },
                    )
                    retryable = True
                else:
                    return self._decode(response, path)

            if not retryable or attempt == attempts:
                break
            await asyncio.sleep(self.settings.retry_backoff * (2 ** (attempt - 1)))

        if last_error is None:  # pragma: no cover - 循环至少执行一次，兜底防御
            raise BackendUnavailableError(details={"path": path, "attempts": attempts})
        raise last_error

    def _decode(self, response: httpx.Response, path: str) -> dict[str, Any]:
        if response.status_code >= 400:
            raise ApiError(
                f"AnythingLLM 返回 HTTP {response.status_code}",
                details={
                    "path": path,
                    "status": response.status_code,
                    "upstream_message": _upstream_message(response),
                },
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise InvalidResponseError(
                "AnythingLLM 返回的响应不是合法 JSON",
                details={"path": path, "status": response.status_code},
            ) from exc
        if not isinstance(payload, dict):
            raise InvalidResponseError(
                "AnythingLLM 返回的响应顶层不是 JSON 对象",
                details={"path": path, "actual": type(payload).__name__},
            )
        return payload


def _upstream_message(response: httpx.Response) -> str:
    """尽力取出上游的一句错误说明；取不到就返回空串，绝不回传整包。"""
    try:
        payload = response.json()
    except ValueError:
        return ""
    if isinstance(payload, dict):
        for key in ("error", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return " ".join(value.split())[:300]
    return ""
