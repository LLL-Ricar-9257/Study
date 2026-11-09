"""测试夹具：把 ``AnythingLLMMCP`` 目录加入 ``sys.path``，并提供假上游。"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client import AnythingLLMClient, Settings


@dataclass
class FakeUpstream:
    """记录请求并按脚本返回响应的 ``httpx.MockTransport`` 包装。"""

    responses: list[httpx.Response] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            return httpx.Response(200, json={"workspaces": []})
        index = min(len(self.requests) - 1, len(self.responses) - 1)
        return self.responses[index]

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    @property
    def call_count(self) -> int:
        return len(self.requests)


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


@pytest.fixture
def settings() -> Settings:
    """重试退避设为 0，测试快速且确定。"""
    return Settings(
        base_url="http://localhost:3001",
        api_key="test-key",
        timeout=5.0,
        max_retries=0,
        retry_backoff=0.0,
    )


@pytest.fixture
def client(settings: Settings, upstream: FakeUpstream) -> Iterator[AnythingLLMClient]:
    instance = AnythingLLMClient(settings, transport=httpx.MockTransport(upstream.handler))
    yield instance
    # httpx 的 aclose 是协程；测试进程结束即回收，不需要显式等待


def json_response(payload: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)
