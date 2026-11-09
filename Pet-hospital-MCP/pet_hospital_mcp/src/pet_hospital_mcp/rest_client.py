"""Go 宠物医院 REST API 的 HTTP 客户端。

职责边界：**只做协议搬运**。请求组装、超时、重试、响应信封拆解、
错误归类都在这里；工具层只负责把参数交过来、把结果交回去。

后端地址来自 :class:`~pet_hospital_mcp.config.Settings`，本模块不会自己
读环境变量，方便测试注入 :class:`httpx.MockTransport`。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Final, Self

import httpx

from .config import Settings
from .errors import (
    BackendApiError,
    BackendInvalidResponseError,
    BackendTimeoutError,
    BackendUnavailableError,
    PetHospitalMCPError,
)

__all__ = [
    "PETS_PATH",
    "STATS_PATH",
    "PetHospitalRestClient",
    "pet_detail_path",
    "pet_summary_path",
]

logger = logging.getLogger(__name__)

PETS_PATH: Final = "/api/v1/pets"
"""``GET /api/v1/pets``——列表查询端点。"""

STATS_PATH: Final = "/api/v1/stats"
"""``GET /api/v1/stats``——医院经营统计端点。"""


def pet_detail_path(pet_id: str) -> str:
    """``GET /api/v1/pets/{id}``——单个宠物档案。"""
    return f"{PETS_PATH}/{pet_id}"


def pet_summary_path(pet_id: str) -> str:
    """``GET /api/v1/pets/{id}/summary``——单只宠物的费用与就诊汇总。"""
    return f"{PETS_PATH}/{pet_id}/summary"

_RETRYABLE_STATUS: Final = frozenset({408, 425, 429, 500, 502, 503, 504})
_MAX_UPSTREAM_MESSAGE: Final = 300


class PetHospitalRestClient:
    """无状态的异步 REST 客户端；线程 / 协程安全，重试无副作用。"""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.pet_hospital_base_url,
            timeout=httpx.Timeout(settings.request_timeout),
            transport=transport,
            follow_redirects=False,
        )

    @property
    def base_url(self) -> str:
        return str(self._client.base_url)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def list_pets(self, query: Mapping[str, str]) -> Any:
        """调用 ``GET /api/v1/pets``，返回响应信封里的 ``data``。"""
        return await self._get(PETS_PATH, dict(query))

    async def get_pet(self, pet_id: str) -> Any:
        """调用 ``GET /api/v1/pets/{id}``，返回单个宠物档案的 ``data``。"""
        return await self._get(pet_detail_path(pet_id), {})

    async def get_pet_summary(self, pet_id: str) -> Any:
        """调用 ``GET /api/v1/pets/{id}/summary``，返回费用与就诊汇总的 ``data``。"""
        return await self._get(pet_summary_path(pet_id), {})

    async def get_stats(self, top: int) -> Any:
        """调用 ``GET /api/v1/stats?top=N``，返回医院经营统计的 ``data``。"""
        return await self._get(STATS_PATH, {"top": str(top)})

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    async def _get(self, path: str, query: Mapping[str, str]) -> Any:
        attempts = self._settings.max_retries + 1
        last_error: PetHospitalMCPError | None = None

        for attempt in range(1, attempts + 1):
            retryable = False
            try:
                response = await self._client.get(path, params=query)
            except httpx.TimeoutException:
                # httpx.TimeoutException 是 TransportError 子类，必须先捕获。
                last_error = BackendTimeoutError(
                    f"调用宠物医院 REST API 超时（{self._settings.request_timeout}s）",
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
                    last_error = BackendApiError(
                        details={
                            "path": path,
                            "status": response.status_code,
                            "attempt": attempt,
                            "attempts": attempts,
                        },
                    )
                    retryable = True
                else:
                    return self._unwrap(response, path)

            if not retryable or attempt == attempts:
                break
            await asyncio.sleep(self._settings.retry_backoff * (2 ** (attempt - 1)))

        if last_error is None:  # pragma: no cover - 循环至少执行一次，兜底防御
            raise BackendUnavailableError(details={"path": path, "attempts": attempts})
        raise last_error

    def _unwrap(self, response: httpx.Response, path: str) -> Any:
        """校验並拆开 Go 的统一响应信封 ``{code, message, data, time}``。"""
        if response.status_code >= 400:
            raise BackendApiError(
                details={
                    "path": path,
                    "status": response.status_code,
                    "upstream_message": _upstream_message(response),
                },
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise BackendInvalidResponseError(
                "宠物医院 REST API 返回的响应不是合法 JSON",
                details={"path": path, "status": response.status_code, "reason": type(exc).__name__},
            ) from exc

        if not isinstance(payload, dict):
            raise BackendInvalidResponseError(
                details={"path": path, "expected": "object", "actual": type(payload).__name__}
            )

        code = payload.get("code")
        if code is not None and code != 200:
            raise BackendApiError(
                details={
                    "path": path,
                    "status": response.status_code,
                    "upstream_code": code,
                    "upstream_message": _message_of(payload),
                },
            )

        if "data" not in payload:
            raise BackendInvalidResponseError(
                details={"path": path, "expected": "envelope.data", "keys": sorted(payload)[:20]}
            )

        return payload["data"]


def _message_of(payload: Mapping[str, Any]) -> str | None:
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()[:_MAX_UPSTREAM_MESSAGE]
    return None


def _upstream_message(response: httpx.Response) -> str | None:
    """尽力从错误响应体里取一句话；取不到就返回 None，绝不回传整包内容。"""
    try:
        payload = response.json()
    except ValueError:
        return None
    if isinstance(payload, dict):
        return _message_of(payload)
    return None