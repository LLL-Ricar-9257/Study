"""OpenAI 兼容的流式模型适配器。

只有这一层知道 openai SDK 的数据结构。Agent 看到的是 core.models.ChatChunk。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from openai import AsyncOpenAI

from ..config import LLMSettings
from ..models import ChatChunk, ChatMessage, ToolCallDelta


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
        response = await self._client.chat.completions.create(
            model=self._settings.model,
            messages=[self._serialize_message(message) for message in messages],
            tools=list(tools) if tools else None,
            stream=True,
        )

        async for chunk in response:
            if not chunk.choices:
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
            if text or deltas:
                yield ChatChunk(text=text, tool_call_deltas=tuple(deltas))

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
