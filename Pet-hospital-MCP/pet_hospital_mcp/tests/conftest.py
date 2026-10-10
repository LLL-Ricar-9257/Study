"""共享测试夹具。

**硬性约束：测试绝不访问真实 Go 服务。** 所有对上游的调用都走
:class:`httpx.MockTransport`，因此整套用例可以离线、并行、可重复运行。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from starlette.applications import Starlette

from pet_hospital_mcp.config import Settings
from pet_hospital_mcp.rest_client import PetHospitalRestClient
from pet_hospital_mcp.server import PROTOCOL_VERSION, build_app, create_server

# ---------------------------------------------------------------------------
# 样本数据（形状严格对齐 Go 的 model.Pet / store.Result）
# ---------------------------------------------------------------------------
SAMPLE_PET: dict[str, Any] = {
    "id": "PET-000001",
    "name": "旺财",
    "species": "犬",
    "breed": "金毛",
    "gender": "公",
    "ageMonths": 36,
    "color": "金色",
    "chipNo": "CHIP-9001",
    "ownerName": "张三",
    "ownerPhone": "13800001111",
    "ownerAddr": "北京市海淀区中关村大街 1 号",
    "doctor": "李医生",
    "disease": "急性肠胃炎",
    "status": "待就诊",
    "allergy": "无",
    "note": "",
    "records": [
        {
            "id": "REC-1",
            "visitDate": "2025-01-02",
            "doctor": "李医生",
            "diagnosis": "急性肠胃炎",
            "symptoms": "呕吐腹泻",
            "treatment": "补液消炎",
            "prescription": ["阿莫西林"],
            "weightKg": 28.5,
            "temperature": 39.2,
            "followUp": "三天后复诊",
            "charge": 380,
            "createdAt": "2025-01-02T10:00:00+08:00",
        }
    ],
    "charges": [
        {
            "id": "CHG-1",
            "item": "血常规检查",
            "category": "检查",
            "amount": 180,
            "doctor": "李医生",
            "date": "2025-01-02",
        }
    ],
    "totalCost": 180,
    "visitCount": 1,
    "createdAt": "2025-01-01T09:00:00+08:00",
    "updatedAt": "2025-01-02T10:00:00+08:00",
}

#: Go 对 nil slice 序列化成 null 的真实表现。
SAMPLE_PET_WITH_NULLS: dict[str, Any] = {
    **SAMPLE_PET,
    "id": "PET-000002",
    "name": "咪咪",
    "species": "猫",
    "records": None,
    "charges": None,
    "breed": "",
    "chipNo": "",
    "ownerAddr": "",
    "totalCost": 0,
    "visitCount": 0,
}


def make_envelope(data: Any, *, code: int = 200, message: str = "ok") -> dict[str, Any]:
    """构造 Go 的统一响应信封。"""
    return {
        "code": code,
        "message": message,
        "data": data,
        "time": "2025-01-01T00:00:00+08:00",
    }


def make_list_data(
    items: list[dict[str, Any]] | None = None,
    *,
    total: int | None = None,
    page: int = 1,
    page_size: int = 10,
    total_pages: int = 1,
    total_cost: float = 180.0,
) -> dict[str, Any]:
    """构造 ``store.Result``。"""
    items = [SAMPLE_PET] if items is None else items
    return {
        "items": items,
        "total": len(items) if total is None else total,
        "page": page,
        "pageSize": page_size,
        "totalPages": total_pages,
        "totalCost": total_cost,
    }


# ---------------------------------------------------------------------------
# 可记录的后端替身
# ---------------------------------------------------------------------------
@dataclass
class RecordedRequest:
    method: str
    path: str
    query: dict[str, list[str]]

    def single(self, key: str) -> str | None:
        values = self.query.get(key)
        return values[0] if values else None


@dataclass
class FakeBackend:
    """``httpx.MockTransport`` 的 handler 包装：记录请求，按脚本返回响应。"""

    responses: list[httpx.Response | Exception] = field(default_factory=list)
    requests: list[RecordedRequest] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        parsed = urlparse(str(request.url))
        self.requests.append(
            RecordedRequest(
                method=request.method,
                path=parsed.path,
                query=parse_qs(parsed.query),
            )
        )
        index = len(self.requests) - 1
        if not self.responses:
            return httpx.Response(200, json=make_envelope(make_list_data()))
        outcome = self.responses[min(index, len(self.responses) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    @property
    def last(self) -> RecordedRequest:
        return self.requests[-1]

    @property
    def call_count(self) -> int:
        return len(self.requests)


@pytest.fixture
def settings() -> Settings:
    """重试退避设为 0，保证测试快速且确定性。"""
    return Settings(
        pet_hospital_base_url="http://127.0.0.1:8080",
        host="127.0.0.1",
        port=8765,
        retry_backoff=0.0,
    )


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def transport(backend: FakeBackend) -> httpx.MockTransport:
    return httpx.MockTransport(backend.handler)


@pytest.fixture
async def rest_client(
    settings: Settings, transport: httpx.MockTransport
) -> AsyncIterator[PetHospitalRestClient]:
    client = PetHospitalRestClient(settings, transport=transport)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def make_client(
    settings: Settings,
) -> Callable[[FakeBackend], PetHospitalRestClient]:
    def factory(fake: FakeBackend) -> PetHospitalRestClient:
        return PetHospitalRestClient(settings, transport=httpx.MockTransport(fake.handler))

    return factory


@pytest.fixture
async def mcp_server(settings: Settings, transport: httpx.MockTransport) -> AsyncIterator[Any]:
    """已注册工具的 ``MCPServer``，可直接 ``call_tool``（不经 HTTP，无 lifespan）。"""
    rest = PetHospitalRestClient(settings, transport=transport)
    server = create_server(settings, client=rest)
    try:
        yield server
    finally:
        # httpx 的 aclose 幂等，server lifespan 里那次是兜底，这里先收干净。
        await rest.aclose()


@pytest.fixture
def mcp_app(settings: Settings, transport: httpx.MockTransport) -> Starlette:
    """无状态 Streamable HTTP 的 Starlette 应用（lifespan 由测试自己驱动）。

    lifespan 不能放在 async fixture 里：pytest-asyncio 的 setup 与 teardown
    跑在不同 task，任何io task group 都会在退出时报
    "Attempted to exit cancel scope in a different task"。因此这里只造 app，
    进入 / 退出 lifespan 由 :func:`mcp_client` 在测试函数体内完成。
    """
    return build_app(settings, transport=transport)


@asynccontextmanager
async def mcp_client(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    """进入 app 的 lifespan，给出可直接打 MCP 端点的客户端（无真实网络）。"""
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            yield client


# ---------------------------------------------------------------------------
# 2026-07-28 无状态请求封装
# ---------------------------------------------------------------------------
PROTOCOL_META_KEY = "io.modelcontextprotocol/protocolVersion"
CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"


def stateless_headers(method: str, name: str | None = None) -> dict[str, str]:
    """构造 2026-07-28 规定的路由头（无 initialize、无 session id）。"""
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": PROTOCOL_VERSION,
        "mcp-method": method,
    }
    if name is not None:
        headers["mcp-name"] = name
    return headers


def stateless_envelope(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    name: str | None = None,
    protocol_version: str = PROTOCOL_VERSION,
    include_meta: bool = True,
    request_id: int = 1,
) -> dict[str, Any]:
    """构造 2026-07-28 的"每请求信封"：协议版本随请求携带，无需 initialize。"""
    payload: dict[str, Any] = dict(params or {})
    if include_meta:
        meta: dict[str, Any] = {
            PROTOCOL_META_KEY: protocol_version,
            CAPABILITIES_META_KEY: {},
        }
        if name is not None:
            meta[CLIENT_INFO_META_KEY] = {"name": "pytest", "version": "1.0"}
        payload["_meta"] = meta
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": payload}


def parse_tool_error(text: str) -> dict[str, Any]:
    """从工具错误文本里抠出统一错误信封。"""
    marker = text.find("{")
    assert marker != -1, f"错误文本里没有 JSON：{text!r}"
    return json.loads(text[marker:])