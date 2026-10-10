import configparser
from pathlib import Path

from openai import OpenAI

cfg = configparser.ConfigParser()
cfg.read(Path(__file__).with_name("config.ini"), encoding="utf-8")

client = OpenAI(
    api_key=cfg["llm"]["api_key"].strip(),
    base_url=cfg["llm"]["base_url"].strip(),
)

messages = [{"role": "system", "content": "你是一个友好的中文助手。"}]

while True:
    question = input("\n请输入一句话：").strip()
    if not question:
        continue

    messages.append({"role": "user", "content": question})

    stream = client.chat.completions.create(
        model=cfg["llm"]["model"].strip(),
        messages=messages,
        stream=True,
    )
    answer = ""
    for chunk in stream:
        for choice in chunk.choices:
            if choice.delta.content:
                answer += choice.delta.content
                print(choice.delta.content, end="", flush=True)
    print()

    messages.append({"role": "assistant", "content": answer})