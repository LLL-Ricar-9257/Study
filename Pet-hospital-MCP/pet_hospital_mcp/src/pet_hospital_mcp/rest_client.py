"""Go 宠物医院 REST API 的 HTTP 客户端。

職責邊界：**只做協議搬運**。請求組裝、超時、重試、響應信封拆解、
錯誤歸類都在這裡；工具層只負責把參數交過來、把結果交回去。

後端地址來自 :class:`~pet_hospital_mcp.config.Settings`，本模塊不會自己
讀環境變量，方便測試注入 :class:`httpx.MockTransport`。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Final

import httpx

from .config import Settings
from .errors import (
    BackendApiError,
    BackendInvalidResponseError,
    BackendTimeoutError,
    BackendUnavailableError,
    PetHospitalMCPError,
)

__all__ = ["PetHospitalRestClient", "PETS_PATH"]

logger = logging.getLogger(__name__)

PETS_PATH: Final = "/api/v1/pets"
"""``GET /api/v1/pets``——本服務唯一對接的後端端點。"""

_RETRYABLE_STATUS: Final = frozenset({408, 425, 429, 500, 502, 503, 504})
_MAX_UPSTREAM_MESSAGE: Final = 300


class PetHospitalRestClient:
    """無狀態的異步 REST 客戶端；線程 / 协程安全，重試無副作用。"""

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

    async def __aenter__(self) -> PetHospitalRestClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def list_pets(self, query: Mapping[str, str]) -> Any:
        """調用 ``GET /api/v1/pets``，返回響應信封裡的 ``data``。"""
        return await self._get(PETS_PATH, dict(query))

    # ------------------------------------------------------------------
    # 內部實現
    # ------------------------------------------------------------------
    async def _get(self, path: str, query: Mapping[str, str]) -> Any:
        attempts = self._settings.max_retries + 1
        last_error: PetHospitalMCPError | None = None

        for attempt in range(1, attempts + 1):
            retryable = False
            try:
                response = await self._client.get(path, params=query)
            except httpx.TimeoutException as exc:
                # httpx.TimeoutException 是 TransportError 子類，必須先捕獲。
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

        assert last_error is not None  # 循環內必然賦值或返回
        raise last_error

    def _unwrap(self, response: httpx.Response, path: str) -> Any:
        """校驗並拆開 Go 的統一響應信封 ``{code, message, data, time}``。"""
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
    """盡力從錯誤響應體裡取一句話；取不到就返回 None，絕不回傳整包內容。"""
    try:
        payload = response.json()
    except ValueError:
        return None
    if isinstance(payload, dict):
        return _message_of(payload)
    return None