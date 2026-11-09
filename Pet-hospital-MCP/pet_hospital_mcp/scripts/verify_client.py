"""用官方 SDK 2.x 客户端验证无状态 MCP 连接与全部 4 个只读工具。

用法（先启动 Go 服务与 MCP 服务）::

    python scripts/verify_client.py
    MCP_PORT=18765 python scripts/verify_client.py        # 自定义端口

脚本本身不硬编码端口：``MCP_HOST`` / ``MCP_PORT`` / ``MCP_PATH`` 与服务读同一套
环境变量，默认与 :mod:`pet_hospital_mcp.config` 一致。
"""

from __future__ import annotations

import asyncio
import json
import sys

from mcp import Client

from pet_hospital_mcp.config import Settings
from pet_hospital_mcp.tools import TOOL_NAMES

EXPECTED_TOOLS = list(TOOL_NAMES)


def _endpoint() -> str:
    settings = Settings.from_env()
    return f"http://{settings.host}:{settings.port}{settings.mcp_path}"


async def _first_pet_id(client: Client) -> str:
    """先列表拿一个真实编号，后面的单只查询才有确定的入参。"""
    result = await client.call_tool("list_pets", {"pageSize": 1})
    items = result.structured_content["items"]
    return items[0]["id"] if items else "PET-000001"


async def main() -> int:
    endpoint = _endpoint()
    print(f"→ 连接 {endpoint}")
    print("  （不发送 initialize，不携带 Mcp-Session-Id）\n")

    ok = True
    async with Client(endpoint) as client:
        listing = await client.list_tools()
        names = [tool.name for tool in listing.tools]
        print(f"[1] 发现工具        : {names}")
        print(f"    协议版本        : {client.protocol_version}")
        print(f"    服务端          : {client.server_info.name} {client.server_info.version}")
        if names != EXPECTED_TOOLS:
            print(f"    ✗ 期望 {EXPECTED_TOOLS}，实际 {names}")
            return 1

        for tool in listing.tools:
            print(
                f"    - {tool.name:17s} 只读={tool.annotations.read_only_hint} "
                f"拒绝额外字段={tool.input_schema.get('additionalProperties') is False} "
                f"入参={', '.join(sorted(tool.input_schema['properties'])) or '（无）'}"
            )
        print()

        result = await client.call_tool(
            "list_pets",
            {"species": "犬", "sortBy": "totalCost", "order": "desc", "pageSize": 3},
        )
        if result.is_error:
            print(f"    ✗ 调用失败：{result.content[0].text}")
            return 1

        data = result.structured_content
        print("[2] 调用 list_pets  : isError=False")
        print(
            f"    total={data['total']} page={data['page']} pageSize={data['pageSize']} "
            f"totalPages={data['totalPages']} totalCost={round(data['totalCost'], 2)}"
        )
        for pet in data["items"]:
            records = "null" if pet["records"] is None else len(pet["records"])
            charges = "null" if pet["charges"] is None else len(pet["charges"])
            print(
                f"      - {pet['id']} {pet['name']} / {pet['species']} / {pet['status']} "
                f"/ {pet['doctor']} / cost={pet['totalCost']} "
                f"/ visits={pet['visitCount']} / records={records} charges={charges}"
            )
        print()

        pet_id = await _first_pet_id(client)
        detail = await client.call_tool("get_pet", {"pet_id": pet_id})
        summary = await client.call_tool("get_pet_summary", {"pet_id": pet_id})
        stats = await client.call_tool("get_hospital_stats", {"top": 3})

        print(f"[3] 单只查询        : pet_id={pet_id}")
        print(
            f"    get_pet          : isError={detail.is_error} "
            f"records={len(detail.structured_content.get('records') or [])} "
            f"charges={len(detail.structured_content.get('charges') or [])}"
        )
        print(
            f"    get_pet_summary  : isError={summary.is_error} "
            f"totalCost={summary.structured_content.get('totalCost')} "
            f"byCategory={summary.structured_content.get('costByCategory')}"
        )
        print(
            f"    get_hospital_stats: isError={stats.is_error} "
            f"pets={stats.structured_content.get('totalPets')} "
            f"revenue={stats.structured_content.get('totalRevenue')} "
            f"top={len(stats.structured_content.get('topSpenders') or [])}"
        )
        ok = ok and not (detail.is_error or summary.is_error or stats.is_error)
        print()

        cases = [
            ("非法种类", "list_pets", {"species": "鼠"}),
            ("pageSize 超界", "list_pets", {"pageSize": 999}),
            ("min > max", "list_pets", {"min": 100, "max": 10}),
            ("未知字段", "list_pets", {"nope": 1}),
            ("编号含斜杠", "get_pet", {"pet_id": "../admin"}),
            ("top 超界", "get_hospital_stats", {"top": 99}),
        ]
        print("[4] 错误路径        :")
        for label, tool_name, args in cases:
            failed = await client.call_tool(tool_name, args)
            text = failed.content[0].text
            marker = text.find("{")
            payload = json.loads(text[marker:]) if marker != -1 else {}
            code = payload.get("error", {}).get("code")
            mark = "✓" if failed.is_error and code == "VALIDATION_ERROR" else "✗"
            ok = ok and mark == "✓"
            print(f"    {mark} {label:12s} isError={failed.is_error} code={code}")

    print("\n全部通过。" if ok else "\n有检查未通过。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))