"""MCP 工具集合。

阶段一只注册 ``list_pets``。新增工具的步骤固定为：

1. 在 ``tools/`` 下新建模块，定义输入 / 成功输出 / 错误输出的 Pydantic 模型；
2. 用 ``build_xxx_tool(client)`` 工厂返回绑定 REST 客户端的协程；
3. 用 ``server.add_tool(...)`` 注册，``name`` 用 snake_case，
   并调用 :func:`harden_tool_arguments` 收紧入参；
4. 在 ``server.register_tools(server, client)`` 里挂上。

REST 客户端、日志脱敏与统一错误全部复用，不重复实现。
"""

from __future__ import annotations

from ..rest_client import PetHospitalRestClient
from ._schema import harden_tool_arguments
from .list_pets import TOOL_NAME as LIST_PETS_TOOL_NAME
from .list_pets import build_list_pets_tool, register_list_pets

__all__ = [
    "PetHospitalRestClient",
    "LIST_PETS_TOOL_NAME",
    "build_list_pets_tool",
    "register_list_pets",
    "register_tools",
    "harden_tool_arguments",
]

#: 当前对外暴露的全部工具名，测试直接断言这份清单。
TOOL_NAMES: tuple[str, ...] = (LIST_PETS_TOOL_NAME,)


def register_tools(server, client: PetHospitalRestClient) -> None:
    """把当前阶段的全部工具注册到 ``server``。"""
    register_list_pets(server, client)