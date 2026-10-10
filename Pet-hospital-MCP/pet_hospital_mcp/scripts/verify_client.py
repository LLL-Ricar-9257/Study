"""用官方 SDK 2.x 客户端验证无状态 MCP 连接与 ``list_pets`` 调用。

用法（先启动 Go 服务与 MCP 服务）::

    python scripts/verify_client.py
    MCP_PORT=18765 python scripts/verify_client.py        # 自定义端口

脚本本身不硬编码端口：``MCP_HOST`` / ``MCP_PORT`` / ``MCP_PATH`` 与服务读同一套
环境变量，默认与 :mod:`pet_hospital_mcp.config` 一致。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import Client

from pet_hospital_mcp.config import Settings


def _endpoint() -> str:
    settings = Settings.from_env()
    return f"http://{settings.host}:{settings.port}{settings.mcp_path}"


async def main() -> int:
    endpoint = _endpoint()
    print(f"→ 连接 {endpoint}")
    print("  （不发送 initialize，不携带 Mcp-Session-Id）\n")

    async with Client(endpoint) as client:
        listing = await client.list_tools()
        names = [tool.name for tool in listing.tools]
        print(f"[1] 发现工具        : {names}")
        print(f"    协议版本        : {client.protocol_version}")
        print(f"    服务端          : {client.server_info.name} {client.server_info.version}")
        if names != ["list_pets"]:
            print(f"    ✗ 期望只有 list_pets，实际 {names}")
            return 1

        tool = listing.tools[0]
        print(f"    标题 / 只读     : {tool.title} / {tool.annotations.read_only_hint}")
        print(f"    入参字段        : {', '.join(sorted(tool.input_schema['properties']))}")
        print(f"    拒绝额外字段    : {tool.input_schema.get('additionalProperties') is False}")
        print(f"    出参字段        : {', '.join(tool.output_schema['properties'])}\n")

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

        cases = [
            ("非法种类", {"species": "鼠"}),
            ("pageSize 超界", {"pageSize": 999}),
            ("min > max", {"min": 100, "max": 10}),
            ("未知字段", {"nope": 1}),
        ]
        print("[3] 错误路径        :")
        ok = True
        for label, args in cases:
            failed = await client.call_tool("list_pets", args)
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
    port = os.environ.get("MCP_PORT", "8765")
    sys.exit(asyncio.run(main()))