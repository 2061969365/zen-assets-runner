#!/usr/bin/env python3
"""从真实抓包重建 sink 记录 fixture（离线、可复现）。

来源（只读，不拷入本仓库）：

  * ``F:\\worker\\zen-direct-v2\\tools\\cli_capture_body.json``
    真实 CLI 主请求体：12 个工具 + 完整 system prompt（含运行时尾部）
  * ``F:\\worker\\zen-direct-v2\\tools\\opencode_tools.json``
    工具真定义 13 个（含 ``patch``，主请求体里没有）

本脚本把它们改造成 ``tools/sink.py`` 的落盘格式::

    {seq, ts, method, path, headers: [[k, v], ...], body_len, body: "<json text>"}

并额外造几条"上游形状变了"的记录（标题生成请求、重试同 body、无 system、非 CLI UA）。
请求头按 §6 第 1 层脱敏后留存（除 ``user-agent`` 外一律 ``<redacted>``），
只为验证"提取层不许把请求头带进资产"。

重跑::

    python tests/fixtures/build_fixtures.py
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.environ.get("ZEN_DIRECT_V2_TOOLS", r"F:\worker\zen-direct-v2\tools")
CAPTURE_BODY = os.path.join(SOURCE_DIR, "cli_capture_body.json")
TOOLS_DEFS = os.path.join(SOURCE_DIR, "opencode_tools.json")

#: 抓包时刻（固定值，保证 fixture 与测试断言可复现）：2026-10-24T07:39:47Z
BASE_TS = datetime(2026, 10, 24, 7, 39, 47, tzinfo=timezone.utc).timestamp()

CLI_UA = "opencode/latest/2.2.5/cli"

#: 抓包机的稳定 24 hex，用于断言"资产里不许出现"
PROJECT_HASH = "5f1c0a9b8d7e6f4a3b2c1d0e"

TITLE_SYSTEM = (
    "You are a title generator. You read a conversation between a user and a coding "
    "agent, then produce a short, specific title in the user's language. The title "
    "must name only the core action and subject, at most 50 characters, with no "
    "quotes, no trailing punctuation, and no execution details such as frequency, "
    "scheduled dates, time zones, status, or configuration. Prefer the concrete "
    "target identifier (file, module, service, PR number, branch name) over generic "
    "phrasing. Do not describe the conversation, do not mention the assistant, and do "
    "not answer the user's question. Output the title and nothing else.\n"
)


def read_json(path: str):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def wrap_tool(definitions, names):
    """扁平定义 -> chat/completions 的 ``{"type":"function","function":{...}}``。"""
    tools = []
    for name in names:
        payload = definitions[name]
        entry = {"type": "function"}
        function = {"name": name, "description": payload["description"]}
        function["parameters"] = payload["parameters"]
        if "strict" in payload:
            function["strict"] = payload["strict"]
        entry["function"] = function
        tools.append(entry)
    return tools


def dump_body(body) -> str:
    return json.dumps(body, ensure_ascii=False, indent=1)


def record(seq: int, path: str, headers, body_text: str, ts_offset: float = 0.0) -> dict:
    return {
        "seq": seq,
        "ts": BASE_TS + ts_offset,
        "method": "POST",
        "path": path,
        "headers": [[key, value] for key, value in headers],
        "body_len": len(body_text.encode("utf-8")),
        "body": body_text,
    }


def cli_headers() -> list:
    return [
        ("user-agent", CLI_UA),
        ("content-type", "application/json"),
        ("accept", "text/event-stream"),
        ("x-opencode-client", "cli"),
        ("x-opencode-project", PROJECT_HASH),
        ("x-opencode-session", "<redacted>"),
        ("x-session-id", "<redacted>"),
        ("authorization", "<redacted>"),
    ]


def write(name: str, payload: dict) -> None:
    target = os.path.join(HERE, name)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
        handle.write("\n")
    print("wrote {0} ({1} B)".format(name, os.path.getsize(target)))


def main() -> int:
    main_body = read_json(CAPTURE_BODY)
    definitions = read_json(TOOLS_DEFS)
    main_text = dump_body(main_body)

    # 1) 标题生成请求：tools=0，system 很短 —— 实测 CLI 最先发的就是这条
    title_body = {
        "model": main_body.get("model"),
        "messages": [
            {"role": "system", "content": TITLE_SYSTEM},
            {"role": "user", "content": "Reply with the single word: ok"},
        ],
        "tools": [],
        "stream": False,
    }
    write(
        "capture_001_title.json",
        record(1, "/inference/v1/chat/completions", cli_headers(), dump_body(title_body)),
    )

    # 2) 主 agent 请求：12 tools + 完整 system（含 Today's date / <env> 尾部）
    write(
        "capture_002_main.json",
        record(2, "/inference/v1/chat/completions", cli_headers(), main_text, 0.4),
    )

    # 3) CLI 重试：body 与 #2 逐字节相同，seq 更大
    write(
        "capture_003_retry.json",
        record(3, "/inference/v1/chat/completions", cli_headers(), main_text, 1.9),
    )

    # 4) 形状正常但没有 system/developer 消息 —— EXTRACT_FAIL
    nosystem_body = {
        "model": main_body.get("model"),
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "tools": wrap_tool(definitions, ["read", "grep", "write"]),
        "stream": True,
    }
    write(
        "capture_004_nosystem.json",
        record(4, "/inference/v1/chat/completions", cli_headers(), dump_body(nosystem_body), 2.5),
    )

    # 5) 我们自己的网关请求：UA 不是 CLI 形状，且没有 x-opencode-client
    write(
        "capture_005_badua.json",
        record(
            5,
            "/inference/v1/chat/completions",
            [("user-agent", "zen-gateway/1.0"), ("content-type", "application/json")],
            main_text,
            3.1,
        ),
    )

    # 6) 工具真定义（13 个，含 patch）：测试用它合成"工具更多的候选"
    write("tools_defs.json", definitions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
