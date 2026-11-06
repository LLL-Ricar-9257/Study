"""OpenAI 兼容的流式模型适配器。

只有这一层知道 openai SDK 的数据结构。Agent 看到的是 core.models.ChatChunk。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from openai import AsyncOpenAI, BadRequestError

from ..config import LLMSettings
from ..models import ChatChunk, ChatMessage, TokenUsage, ToolCallDelta


class OpenAIChatModel:
    def __init__(self, settings: LLMSettings) -> None:
        self._settings = settings
        self._client = AsyncOpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.timeout_seconds,
        )

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[Mapping[str, Any]],
    ) -> AsyncIterator[ChatChunk]:
        response = await self._create_stream(messages, tools)

        async for chunk in response:
            # 开启 include_usage 后，供应商会在最后一个 choices 为空的分片里
            # 返回 token 用量，必须在跳过空 choices 之前提取。
            usage = _extract_usage(getattr(chunk, "usage", None))
            if not chunk.choices:
                if usage is not None:
                    yield ChatChunk(usage=usage)
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue

            deltas: list[ToolCallDelta] = []
            for call in delta.tool_calls or ():
                function = call.function
                deltas.append(
                    ToolCallDelta(
                        index=call.index if call.index is not None else 0,
                        id=call.id or "",
                        name=(function.name if function and function.name else ""),
                        arguments=(
                            function.arguments
                            if function and function.arguments
                            else ""
                        ),
                    )
                )

            text = delta.content or ""
            if text or deltas or usage is not None:
                yield ChatChunk(
                    text=text,
                    tool_call_deltas=tuple(deltas),
                    usage=usage,
                )

    async def _create_stream(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[Mapping[str, Any]],
    ) -> Any:
        payload: dict[str, Any] = {
            "model": self._settings.model,
            "messages": [self._serialize_message(message) for message in messages],
            "tools": list(tools) if tools else None,
            "stream": True,
        }
        try:
            return await self._client.chat.completions.create(
                **payload,
                stream_options={"include_usage": True},
            )
        except (BadRequestError, TypeError):
            # 少数 OpenAI 兼容服务不支持 stream_options；退化为不返回用量的模式，
            # 核心会改用本地估算展示上下文。
            return await self._client.chat.completions.create(**payload)

    async def aclose(self) -> None:
        await self._client.close()

    @staticmethod
    def _serialize_message(message: ChatMessage) -> dict[str, Any]:
        if message.role == "assistant":
            payload: dict[str, Any] = {
                "role": "assistant",
                "content": message.content,
            }
            if message.tool_calls:
                payload["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                    }
                    for call in message.tool_calls
                ]
            return payload

        if message.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": message.tool_call_id or "",
                "content": message.content or "",
            }

        return {"role": message.role, "content": message.content or ""}


def _get_field(raw: Any, name: str) -> Any:
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        return raw.get(name)
    return getattr(raw, name, None)


def _as_int(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _extract_usage(raw: Any) -> TokenUsage | None:
    """把 OpenAI / DeepSeek 不同形状的 usage 对象统一成 ``TokenUsage``。"""

    if raw is None:
        return None

    prompt = _as_int(_get_field(raw, "prompt_tokens"))
    completion = _as_int(_get_field(raw, "completion_tokens"))
    total = _as_int(_get_field(raw, "total_tokens")) or (prompt + completion)

    cached: int | None = None
    details = _get_field(raw, "prompt_tokens_details")
    cached_value = _get_field(details, "cached_tokens")
    if cached_value is not None:
        cached = _as_int(cached_value)

    # DeepSeek 的字段优先，且即使值为 0 也说明供应商返回了缓存信息。
    deepseek_hit = _get_field(raw, "prompt_cache_hit_tokens")
    if deepseek_hit is not None:
        cached = _as_int(deepseek_hit)

    miss: int | None = None
    miss_value = _get_field(raw, "prompt_cache_miss_tokens")
    if miss_value is not None:
        miss = _as_int(miss_value)
    elif cached is not None:
        miss = max(prompt - cached, 0)

    reasoning: int | None = None
    completion_details = _get_field(raw, "completion_tokens_details")
    reasoning_value = _get_field(completion_details, "reasoning_tokens")
    if reasoning_value is not None:
        reasoning = _as_int(reasoning_value)

    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cached_tokens=cached,
        cache_miss_tokens=miss,
        reasoning_tokens=reasoning,
    )


__all__ = ["OpenAIChatModel"]
