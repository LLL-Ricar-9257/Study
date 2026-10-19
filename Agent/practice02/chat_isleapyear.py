import configparser
import json
import os
import subprocess
from pathlib import Path

from openai import OpenAI

cfg = configparser.ConfigParser()
cfg.read(Path(__file__).with_name("config.ini"), encoding="utf-8")

client = OpenAI(
    api_key=cfg["llm"]["api_key"].strip(),
    base_url=cfg["llm"]["base_url"].strip(),
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "getdate",
            "description": "获取当前电脑的日期和时间",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "isleapyear",
            "description": "判断指定年份是否为闰年",
            "parameters": {
                "type": "object",
                "properties": {
                    "year": {
                        "type": "integer",
                        "description": "要判断的年份，例如 2024",
                    }
                },
                "required": ["year"],
            },
        },
    },
]


def getdate() -> str:
    if os.name == "nt":
        command = [
            "powershell",
            "-NoProfile",
            "-Command",
            "date -Format 'yyyy-MM-dd HH:mm:ss'",
        ]
    else:
        command = ["date", "+%Y-%m-%d %H:%M:%S"]

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        return f"获取日期时间失败：{result.stderr.strip()}"
    return result.stdout.strip()


def isleapyear(year: int) -> str:
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    return f"{year} 年是闰年" if leap else f"{year} 年不是闰年"


TOOL_FUNCTIONS = {"getdate": getdate, "isleapyear": isleapyear}


def run_tool_calls(tool_calls, messages):
    for tool_call in tool_calls:
        name = tool_call["function"]["name"]
        function = TOOL_FUNCTIONS.get(name)
        if function is None:
            content = f"未知工具：{name}"
        else:
            try:
                arguments = json.loads(tool_call["function"]["arguments"] or "{}")
                content = function(**arguments)
            except Exception as exc:
                content = f"工具执行失败：{exc}"

        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": content,
            }
        )


def stream_chat(messages):
    stream = client.chat.completions.create(
        model=cfg["llm"]["model"].strip(),
        messages=messages,
        tools=TOOLS,
        stream=True,
    )

    content = ""
    tool_calls = {}
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta.content:
            content += delta.content
            print(delta.content, end="", flush=True)

        for tool_call in delta.tool_calls or []:
            index = tool_call.index if tool_call.index is not None else 0
            entry = tool_calls.setdefault(
                index,
                {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                },
            )
            if tool_call.id:
                entry["id"] += tool_call.id
            if tool_call.function:
                if tool_call.function.name:
                    entry["function"]["name"] += tool_call.function.name
                if tool_call.function.arguments:
                    entry["function"]["arguments"] += tool_call.function.arguments

    if content:
        print()
    return content, [tool_calls[index] for index in sorted(tool_calls)]


def main():
    messages = [{"role": "system", "content": "你是一个友好的中文助手。"}]

    while True:
        question = input("\n请输入一句话：").strip()
        if not question:
            continue

        messages.append({"role": "user", "content": question})

        while True:
            content, tool_calls = stream_chat(messages)

            assistant_message = {"role": "assistant", "content": content or None}
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls
            messages.append(assistant_message)

            if tool_calls:
                run_tool_calls(tool_calls, messages)
                continue

            break


if __name__ == "__main__":
    main()
