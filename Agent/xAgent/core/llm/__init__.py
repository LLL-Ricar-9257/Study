"""LLM 适配器。核心 Agent 只依赖 core.ports.ChatModel。"""

from .openai_chat import OpenAIChatModel

__all__ = ["OpenAIChatModel"]
