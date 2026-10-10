"""工具注册、工具名、JSON Schema 与工具描述。"""

from __future__ import annotations

import inspect
import re

import pytest

from pet_hospital_mcp.server import SERVER_NAME, SERVER_VERSION
from pet_hospital_mcp.tools import TOOL_NAMES
from pet_hospital_mcp.tools.list_pets import LIST_PETS_DESCRIPTION, TOOL_NAME

EXPECTED_QUERY_PARAMS = {
    "q",
    "name",
    "ownerName",
    "ownerPhone",
    "species",
    "doctor",
    "disease",
    "status",
    "min",
    "max",
    "sortBy",
    "order",
    "page",
    "pageSize",
}


async def test_only_one_tool_is_registered(mcp_server) -> None:
    """阶段一只暴露 list_pets，阶段二工具不得提前出现。"""
    tools = await mcp_server.list_tools()
    assert [tool.name for tool in tools] == ["list_pets"]
    assert TOOL_NAMES == ("list_pets",)


async def test_tool_name_is_snake_case(mcp_server) -> None:
    tool = (await mcp_server.list_tools())[0]
    assert tool.name == TOOL_NAME
    assert re.fullmatch(r"[a-z0-9_]+", tool.name)
    assert not re.search(r"[A-Z-]", tool.name)


async def test_input_schema_shape(mcp_server) -> None:
    tool = (await mcp_server.list_tools())[0]
    schema = tool.input_schema

    assert schema["type"] == "object"
    assert set(schema["properties"]) == EXPECTED_QUERY_PARAMS
    # 未知字段必须被拒绝，而不是被静默丢弃。
    assert schema["additionalProperties"] is False
    assert schema.get("required", []) == []


async def test_input_schema_publishes_backend_enums(mcp_server) -> None:
    props = (await mcp_server.list_tools())[0].input_schema["properties"]

    assert props["species"]["anyOf"][0]["enum"] == ["犬", "猫", "兔", "鸟", "仓鼠", "爬宠", "其他"]
    assert props["status"]["anyOf"][0]["enum"] == [
        "待就诊",
        "就诊中",
        "住院中",
        "已康复",
        "慢性病随访",
    ]
    assert props["order"]["anyOf"][0]["enum"] == ["asc", "desc"]
    assert "totalCost" in props["sortBy"]["anyOf"][0]["enum"]
    # Optional 字段的数值约束由 Pydantic 以 ge/le 写在 anyOf 之外的顶层。
    assert props["page"]["ge"] == 1
    assert props["pageSize"]["ge"] == 1
    assert props["pageSize"]["le"] == 500
    assert props["min"]["ge"] == 0
    assert props["max"]["ge"] == 0


async def test_every_parameter_has_a_description(mcp_server) -> None:
    props = (await mcp_server.list_tools())[0].input_schema["properties"]
    for name, schema in props.items():
        assert schema.get("description"), f"{name} 缺少 description"


async def test_tool_description_covers_usage_params_scenarios_and_returns(mcp_server) -> None:
    tool = (await mcp_server.list_tools())[0]
    description = tool.description

    assert description == LIST_PETS_DESCRIPTION
    assert "GET /api/v1/pets" in description
    assert "适用场景" in description
    assert "返回值" in description
    assert "失败时返回统一错误结构" in description
    for name in EXPECTED_QUERY_PARAMS:
        assert f"`{name}`" in description


async def test_tool_annotations_declare_read_only(mcp_server) -> None:
    annotations = (await mcp_server.list_tools())[0].annotations
    assert annotations.read_only_hint is True
    assert annotations.destructive_hint is False
    assert annotations.idempotent_hint is True


async def test_server_metadata(mcp_server) -> None:
    assert mcp_server.name == SERVER_NAME
    assert mcp_server.version == SERVER_VERSION
    assert "list_pets" in (mcp_server.instructions or "")


def test_tool_function_is_async() -> None:
    from pet_hospital_mcp.tools.list_pets import build_list_pets_tool

    class _Stub:
        async def list_pets(self, params):  # pragma: no cover
            return None

        async def aclose(self):  # pragma: no cover
            return None

    fn = build_list_pets_tool(_Stub())
    assert inspect.iscoroutinefunction(fn)
    assert fn.__name__ == "list_pets"


def test_package_never_imports_fastmcp() -> None:
    """SDK 2.x 已移除 fastmcp，本服务也必须没有对它的任何 import。"""
    assert not _forbidden_imports(), _forbidden_imports()


def test_server_uses_mcpserver_class() -> None:
    """必须用 SDK 2.x 的 MCPServer 装配。"""
    from mcp.server import MCPServer as SdkMCPServer

    import pet_hospital_mcp.server as server_module

    assert server_module.MCPServer is SdkMCPServer
    assert server_module.MCPServer.__module__ == "mcp.server.mcpserver.server"


@pytest.mark.parametrize("forbidden", ["fastmcp", "FastMCP"])
def test_fastmcp_symbol_is_not_referenced(forbidden: str) -> None:
    """除文档字符串外，源码里不应出现 FastMCP 符号。"""
    import pathlib

    import pet_hospital_mcp

    root = pathlib.Path(pet_hospital_mcp.__file__).parent
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith(("#", "*"))
        )
        assert forbidden not in code, f"{path.name} 仍然引用了 {forbidden}"


def _forbidden_imports() -> list[str]:
    import ast
    import pathlib

    import pet_hospital_mcp

    offenders: list[str] = []
    root = pathlib.Path(pet_hospital_mcp.__file__).parent
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("mcp.server.fastmcp"):
                    offenders.append(f"{path.name}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                offenders.extend(
                    f"{path.name}: import {alias.name}"
                    for alias in node.names
                    if alias.name.startswith("mcp.server.fastmcp")
                )
    return offenders