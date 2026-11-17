#!/usr/bin/env python3
"""extract_assets.py —— 把 ``tools/sink.py`` 落盘的捕获记录抽成两个资产文件。

格式契约：``docs/asset-format.md``（唯一真源，本文件逐条照做）

  * §3     prompt.txt 只保留静态正文：抓到的是"静态正文 + 运行时尾部"，
           必须在**行首**出现 ``Today's date: `` 或 ``<env>`` 处切断，去掉尾部空行，
           落盘时补**恰好一个** ``\n``（§3.2-3，2026-11-17 修正）
  * §4     候选识别：seq 升序 → POST → path → UA → body 可解析且含 messages/input
           → tools>=3 且名字全在已知官方名集合 → 取 (工具数, system 长度) 最大者
           → 按 body 的 sha256 去重。**绝不取第一条**（CLI 会先发 tools=0 的标题请求）
  * §5     失败语义：``NO_CAPTURE`` / ``EXTRACT_FAIL`` 一律 exit 1，且信息必须能定位到 seq + 原因
  * §6     脱敏第 2 层：输出里不出现任何请求头

用法::

    python tools/extract_assets.py \
        --captures bodies --out candidate --cli-version "$(opencode --version)"

退出码：0 = 成功；1 = NO_CAPTURE 或 EXTRACT_FAIL。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 常量（契约 §4 / §5）
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_FAIL = 1

STATUS_OK = "OK"
STATUS_NO_CAPTURE = "NO_CAPTURE"
STATUS_EXTRACT_FAIL = "EXTRACT_FAIL"

CAPTURE_PATH_RE = re.compile(r"^/inference/v1/(chat/completions|responses)$")
CLI_UA_RE = re.compile(r"^opencode/latest/[\d.]+/cli$")
CLIENT_HEADER = "x-opencode-client"

#: §3.1：这两个标记出现在**行首**时，就是静态正文的结束位置
SPLIT_MARKERS: Tuple[str, ...] = ("Today's date: ", "<env>")

#: §3.2 规则 2：这些串一个都不许出现在 prompt.txt 里
PROMPT_FORBIDDEN: Tuple[str, ...] = (
    "Today's date:",
    "Working directory:",
    "Platform:",
    "<env>",
)

#: §3.2 规则 1
PROMPT_MIN_CHARS = 2000

#: §3.2 规则 3（2026-11-17 修正）：prompt.txt 必须以**恰好一个** ``\n`` 结尾，
#: 正文本身不得以空白结尾（不许尾部空行、行尾空格/制表符）。
PROMPT_TRAILING_NEWLINE = "\n"

#: §4：system / developer 都算"系统消息"
SYSTEM_ROLES = ("system", "developer")

#: §4 已知官方名集合（只用于候选过滤，不是资产内容）
KNOWN_OFFICIAL_TOOLS = frozenset(
    (
        "edit",
        "execute",
        "glob",
        "grep",
        "patch",
        "question",
        "read",
        "shell",
        "skill",
        "subagent",
        "webfetch",
        "websearch",
        "write",
        "experimental_image_generate",
    )
)

MIN_TOOLS = 3

#: §2.1 允许出现在 official_tools.json 顶层的字段（其余一律不写）
TOP_LEVEL_KEYS = ("cli_version", "captured_at", "source", "order", "tools")
#: §2.1 允许出现在 tools[*] 里的字段（其余一律不写，尤其不许混进请求头）
TOOL_KEYS = ("description", "parameters", "strict")
SOURCE_WORKFLOW = "extract"

#: §2.1 source.sha256 的 canonical 形式。
#: 契约字面量是 ``json.dumps(tools, sort_keys=True, separators=(',', ':'))``，
#: 未写 ensure_ascii，这里沿用 Python 默认（True）以便和 validator 抄同一行时对齐；
#: 已知 ``assets/official_tools.json`` 的 bootstrap 记录用的是默认分隔符 +
#: ensure_ascii=False（见回报中的歧义条目）。
CANONICAL_DUMPS_KWARGS: Dict[str, Any] = {"sort_keys": True, "separators": (",", ":")}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Rejection:
    """一条被排除的记录：必须能定位到 seq 与原因（§5）。"""

    source: str
    seq: Optional[int]
    reason: str

    def describe(self) -> str:
        seq = "?" if self.seq is None else self.seq
        return "seq={seq} file={source} reason={reason}".format(
            seq=seq, source=self.source, reason=self.reason
        )


@dataclass
class Candidate:
    """通过 §4 全部筛选的一条捕获记录。"""

    seq: int
    source: str
    body_sha256: str
    order: List[str]
    raw_tools: List[Dict[str, Any]]
    system_text: Optional[str]
    ts: Any

    @property
    def tool_count(self) -> int:
        return len(self.order)

    @property
    def system_len(self) -> int:
        return len(self.system_text or "")


@dataclass
class Result:
    """抽取结果 + 自检指标（全部是计数/摘要，不含任何请求头）。"""

    status: str
    message: str = ""
    rejections: List[Rejection] = field(default_factory=list)
    record_count: int = 0
    candidates: int = 0
    duplicates: int = 0
    selected_seq: Optional[int] = None
    order: List[str] = field(default_factory=list)
    prompt_chars: int = 0
    prompt_bytes: int = 0
    split_offset: Optional[int] = None
    split_marker: Optional[str] = None
    captured_at: Optional[str] = None
    sha256: Optional[str] = None
    out_dir: Optional[str] = None
    official_tools_path: Optional[str] = None
    prompt_path: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def summary(self) -> str:
        return (
            "status={status} seq={seq} records={records} candidates={candidates} "
            "dup={dup} order={order} tools={tools} prompt_chars={chars} "
            "prompt_bytes={nbytes} split_offset={offset} marker={marker} "
            "captured_at={captured} sha256={sha}"
        ).format(
            status=self.status,
            seq=self.selected_seq,
            records=self.record_count,
            candidates=self.candidates,
            dup=self.duplicates,
            order=len(self.order),
            tools=len(self.order),
            chars=self.prompt_chars,
            nbytes=self.prompt_bytes,
            offset=self.split_offset,
            marker=self.split_marker,
            captured=self.captured_at,
            sha=self.sha256,
        )


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_tools_json(tools: Dict[str, Dict[str, Any]]) -> str:
    """§2.1：tools 对象的 canonical JSON。"""
    return json.dumps(tools, **CANONICAL_DUMPS_KWARGS)


def tools_sha256(tools: Dict[str, Dict[str, Any]]) -> str:
    return hashlib.sha256(canonical_tools_json(tools).encode("utf-8")).hexdigest()


def rfc3339_utc(ts: Any) -> str:
    """把 sink 的 ``ts``（epoch 秒，float/int）转成 RFC3339 UTC（``...Z``）。"""
    moment: Optional[datetime] = None
    if isinstance(ts, bool):
        ts = None
    if isinstance(ts, (int, float)):
        try:
            moment = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            moment = None
    elif isinstance(ts, str) and ts.strip():
        raw = ts.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(raw)
        except ValueError:
            moment = None
        if moment is not None and moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if moment is not None:
            moment = moment.astimezone(timezone.utc)
    if moment is None:
        moment = datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 记录加载
# ---------------------------------------------------------------------------


def read_record_files(captures_dir: str) -> List[str]:
    """列出 ``*.json``（含子目录，排序后返回，确保可复现）。"""
    found: List[str] = []
    for root, dirs, names in os.walk(captures_dir):
        dirs.sort()
        for name in sorted(names):
            if name.lower().endswith(".json"):
                found.append(os.path.join(root, name))
    return found


def load_records(captures_dir: str) -> Tuple[List[Tuple[int, str, Dict[str, Any]]], List[Rejection]]:
    """读全部记录并按 ``seq`` 升序排列（§4 第一步）。"""
    ordered: List[Tuple[int, str, Dict[str, Any]]] = []
    rejections: List[Rejection] = []
    for path in read_record_files(captures_dir):
        name = os.path.basename(path)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                record = json.load(handle)
        except OSError as exc:
            rejections.append(Rejection(name, None, "unreadable capture file ({0})".format(exc.__class__.__name__)))
            continue
        except ValueError as exc:
            rejections.append(Rejection(name, None, "capture file is not valid JSON ({0})".format(exc)))
            continue
        if not isinstance(record, dict):
            rejections.append(Rejection(name, None, "capture file is not a JSON object"))
            continue
        seq = record.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int):
            rejections.append(Rejection(name, None, "record has no integer 'seq'"))
            continue
        ordered.append((seq, name, record))
    ordered.sort(key=lambda item: (item[0], item[1]))
    return ordered, rejections


def headers_lookup(headers: Any) -> Dict[str, str]:
    """把 sink 的 ``[[k, v], ...]`` 转成小写 key 的 dict（只用于匹配，绝不输出）。"""
    pairs: List[Tuple[Any, Any]] = []
    if isinstance(headers, dict):
        pairs = list(headers.items())
    elif isinstance(headers, (list, tuple)):
        for item in headers:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                pairs.append((item[0], item[1]))
            elif isinstance(item, dict) and "name" in item:
                pairs.append((item.get("name"), item.get("value")))
    lookup: Dict[str, str] = {}
    for key, value in pairs:
        if not isinstance(key, str):
            continue
        lowered = key.strip().lower()
        if lowered in lookup:
            continue
        if value is None:
            lookup[lowered] = ""
        elif isinstance(value, str):
            lookup[lowered] = value
        else:
            lookup[lowered] = str(value)
    return lookup


def parse_body(record: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """§4：body 解析成功（对象）。返回 (body, 失败原因)。"""
    raw = record.get("body")
    if isinstance(raw, dict):
        return raw, None
    if not isinstance(raw, str):
        return None, "body is missing or not a string"
    if not raw.strip():
        return None, "body is empty"
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        return None, "body is not valid JSON ({0})".format(exc)
    if not isinstance(parsed, dict):
        return None, "body JSON is not an object"
    return parsed, None


def part_text(value: Any) -> str:
    """兼容 ``content`` 是字符串或 parts 数组（chat/completions 与 responses 两种形状）。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks: List[str] = []
        for part in value:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                for key in ("text", "content"):
                    inner = part.get(key)
                    if isinstance(inner, str):
                        chunks.append(inner)
                        break
                    if isinstance(inner, list):
                        chunks.append(part_text(inner))
                        break
        return "".join(chunks)
    if isinstance(value, dict):
        for key in ("text", "content"):
            inner = value.get(key)
            if isinstance(inner, str):
                return inner
            if isinstance(inner, list):
                return part_text(inner)
    return ""


def extract_system_text(body: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """取 role in {system, developer} 的消息内容（§4）。返回 (文本, 来源标签)。

    文本为 ``None`` 表示"根本没有系统消息"——这在 §5 里属于 EXTRACT_FAIL，
    所以不在候选筛选阶段丢弃（否则会被误报成 NO_CAPTURE）。
    """
    for key in ("messages", "input"):
        messages = body.get(key)
        if not isinstance(messages, list):
            continue
        chunks: List[str] = []
        seen_system = False
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if not isinstance(role, str) or role.strip().lower() not in SYSTEM_ROLES:
                continue
            seen_system = True
            text = part_text(message.get("content"))
            if text:
                chunks.append(text)
        if chunks:
            return "\n\n".join(chunks), key
        if seen_system:
            return "", key
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        return instructions, "instructions"
    return None, None


def tool_payload(entry: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """把 ``{"type":"function","function":{...}}`` 或扁平 ``{name:...}`` 归一成一份定义。"""
    if not isinstance(entry, dict):
        return None, "tool entry is not an object"
    inner = entry.get("function")
    source = inner if isinstance(inner, dict) else entry
    name = source.get("name")
    if not isinstance(name, str) or not name.strip():
        return None, "tool entry has no usable name"
    payload = {
        "name": name.strip(),
        "description": source.get("description"),
        "parameters": source.get("parameters"),
        "strict": source.get("strict", False),
    }
    return payload, None


def split_prompt(system_text: str) -> Tuple[str, Optional[int], Optional[str]]:
    """§3.1 切分：返回 (静态正文, 切分点字符偏移, 命中的标记)。

    切分点 = **行首**第一个 ``Today's date: `` 或 ``<env>`` 的位置；
    保留其前的全部内容并去掉尾部空行。
    """
    best: Optional[int] = None
    best_marker: Optional[str] = None
    for marker in SPLIT_MARKERS:
        index = system_text.find(marker)
        while index >= 0:
            if index == 0 or system_text[index - 1] == "\n":
                if best is None or index < best:
                    best = index
                    best_marker = marker
                break
            index = system_text.find(marker, index + 1)
    if best is None:
        # 没有运行时尾部：整段都是静态正文
        return system_text.rstrip(), None, None
    head = system_text[:best]
    if head.endswith("\r"):
        head = head[:-1]
    return head.rstrip(), best, best_marker


def split_metrics(system_text: str) -> Tuple[str, Optional[int], Optional[str]]:
    return split_prompt(system_text)


# ---------------------------------------------------------------------------
# 候选筛选（§4）
# ---------------------------------------------------------------------------


def screen_record(
    seq: int, source: str, record: Dict[str, Any]
) -> Tuple[Optional[Candidate], Optional[str]]:
    """单条记录的 §4 筛选。返回 (候选, None) 或 (None, 失败原因)。"""
    method = record.get("method")
    if not isinstance(method, str) or method.strip().upper() != "POST":
        return None, "method is not POST ({0!r})".format(method)

    path = record.get("path")
    if not isinstance(path, str) or not CAPTURE_PATH_RE.match(path):
        return None, "path does not match {0} ({1!r})".format(CAPTURE_PATH_RE.pattern, path)

    headers = headers_lookup(record.get("headers"))
    user_agent = headers.get("user-agent", "")
    has_client_header = CLIENT_HEADER in headers
    if not CLI_UA_RE.match(user_agent) and not has_client_header:
        return None, "user-agent {0!r} is not CLI-shaped and {1} is absent".format(
            user_agent, CLIENT_HEADER
        )

    body, reason = parse_body(record)
    if body is None:
        return None, reason or "body unusable"

    if "messages" not in body and "input" not in body:
        return None, "body has neither 'messages' nor 'input'"

    tools = body.get("tools")
    if not isinstance(tools, list):
        return None, "body has no 'tools' array"
    if len(tools) < MIN_TOOLS:
        return None, "tools count {0} < {1}".format(len(tools), MIN_TOOLS)

    order: List[str] = []
    raw_tools: List[Dict[str, Any]] = []
    unknown: List[str] = []
    for entry in tools:
        payload, reason = tool_payload(entry)
        if payload is None:
            return None, reason or "tool entry unusable"
        name = payload["name"]
        if name not in KNOWN_OFFICIAL_TOOLS:
            unknown.append(name)
        if name in order:
            return None, "tools array repeats name {0!r}".format(name)
        order.append(name)
        raw_tools.append(payload)
    if unknown:
        return None, "unknown tool name(s) not in official set: {0}".format(
            ", ".join(sorted(set(unknown)))
        )

    system_text, _origin = extract_system_text(body)

    raw_body = record.get("body")
    body_for_hash = raw_body if isinstance(raw_body, str) else json.dumps(raw_body, sort_keys=True)

    return (
        Candidate(
            seq=seq,
            source=source,
            body_sha256=sha256_text(body_for_hash),
            order=order,
            raw_tools=raw_tools,
            system_text=system_text,
            ts=record.get("ts"),
        ),
        None,
    )


def collect_candidates(
    captures_dir: str,
) -> Tuple[List[Candidate], List[Rejection], int]:
    """全量候选（已去重）+ 被排除记录 + 记录总数。"""
    records, rejections = load_records(captures_dir)
    candidates: List[Candidate] = []
    for seq, source, record in records:
        candidate, reason = screen_record(seq, source, record)
        if candidate is None:
            rejections.append(Rejection(source, seq, reason or "rejected"))
            continue
        candidates.append(candidate)

    unique: Dict[str, Candidate] = {}
    for candidate in candidates:
        first = unique.get(candidate.body_sha256)
        if first is not None:
            rejections.append(
                Rejection(
                    candidate.source,
                    candidate.seq,
                    "duplicate body sha256 of seq={0}".format(first.seq),
                )
            )
            continue
        unique[candidate.body_sha256] = candidate
    return list(unique.values()), rejections, len(records)


def pick_candidate(candidates: Sequence[Candidate]) -> Candidate:
    """(工具数, system 长度) 最大者；并列时取 seq 更小的。"""
    return max(candidates, key=lambda c: (c.tool_count, c.system_len, -c.seq))


# ---------------------------------------------------------------------------
# 资产生成 + 自检
# ---------------------------------------------------------------------------


def build_tools(candidate: Candidate) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """按抓包顺序产出 tools 映射，并做 §2.2 的字段自检。"""
    tools: Dict[str, Dict[str, Any]] = {}
    problems: List[str] = []
    for payload in candidate.raw_tools:
        name = payload["name"]
        description = payload["description"]
        parameters = payload["parameters"]
        strict = payload["strict"]

        if not isinstance(description, str) or not description.strip():
            problems.append("tool {0!r}: description is missing/empty".format(name))
            description = ""
        if not isinstance(parameters, dict):
            problems.append("tool {0!r}: parameters is not an object".format(name))
            parameters = {}
        elif parameters.get("type") != "object":
            problems.append("tool {0!r}: parameters has no \"type\": \"object\"".format(name))
        if not isinstance(strict, bool):
            strict = False

        tools[name] = {
            "description": description,
            "parameters": parameters,
            "strict": strict,
        }
    return tools, problems


def check_prompt(prompt: str) -> List[str]:
    """§3.2 自检（对**正文**；落盘时再补恰好一个换行）。"""
    problems: List[str] = []
    if not prompt.strip():
        problems.append("prompt is empty after split")
    if len(prompt) < PROMPT_MIN_CHARS:
        problems.append(
            "prompt length {0} < {1}".format(len(prompt), PROMPT_MIN_CHARS)
        )
    for token in PROMPT_FORBIDDEN:
        if token in prompt:
            problems.append("prompt still contains runtime token {0!r}".format(token))
    if prompt != prompt.rstrip():
        # 尾部空行/行尾空格一律去掉后再落盘，这里只做兜底断言
        problems.append("prompt body ends with whitespace")
    return problems


def check_assets_shape(document: Dict[str, Any]) -> List[str]:
    """§2.1/§2.2/§6：只许出现契约字段，且 order 与 tools 键集合一致、无重复。"""
    problems: List[str] = []
    extra = [key for key in document if key not in TOP_LEVEL_KEYS]
    if extra:
        problems.append("unexpected top-level field(s): {0}".format(", ".join(sorted(extra))))
    order = document.get("order") or []
    tools = document.get("tools") or {}
    if not isinstance(order, list) or not isinstance(tools, dict):
        problems.append("order/tools have wrong types")
        return problems
    if len(order) < 8:
        problems.append("order length {0} < 8".format(len(order)))
    if len(set(order)) != len(order):
        problems.append("order contains duplicates")
    if set(order) != set(tools):
        problems.append("order and tools keys differ")
    for name, payload in tools.items():
        extra_keys = [key for key in payload if key not in TOOL_KEYS]
        if extra_keys:
            problems.append(
                "tool {0!r} has unexpected field(s): {1}".format(name, ", ".join(sorted(extra_keys)))
            )
        if not payload.get("description"):
            problems.append("tool {0!r} has empty description".format(name))
        params = payload.get("parameters")
        if not isinstance(params, dict) or params.get("type") != "object":
            problems.append("tool {0!r} parameters lack \"type\": \"object\"".format(name))
    return problems


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def build_document(
    cli_version: str,
    captured_at: str,
    order: List[str],
    tools: Dict[str, Dict[str, Any]],
    sha256_value: str,
) -> Dict[str, Any]:
    return {
        "cli_version": cli_version,
        "captured_at": captured_at,
        "source": {
            "workflow": SOURCE_WORKFLOW,
            "run_url": None,
            "sha256": sha256_value,
        },
        "order": list(order),
        "tools": {name: tools[name] for name in sorted(tools)},
    }


def write_assets(out_dir: str, document: Dict[str, Any], prompt: str) -> Tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    tools_path = os.path.join(out_dir, "official_tools.json")
    prompt_path = os.path.join(out_dir, "prompt.txt")
    with open(tools_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    # 契约 §3.2-3（2026-11-17 修正）：UTF-8 无 BOM + 恰好一个 \n 结尾
    blob = (prompt.rstrip() + PROMPT_TRAILING_NEWLINE).encode("utf-8")
    with open(prompt_path, "wb") as handle:
        handle.write(blob)
    return tools_path, prompt_path


def extract_assets(captures_dir: str, out_dir: str, cli_version: str) -> Result:
    """抽取主入口：返回 Result，不在这里退出进程（便于单测与复用）。"""
    result = Result(status=STATUS_NO_CAPTURE)

    if not isinstance(cli_version, str) or not cli_version.strip():
        result.status = STATUS_EXTRACT_FAIL
        result.message = "--cli-version must be the real `opencode --version` output"
        return result
    cli_version = cli_version.strip()

    if not os.path.isdir(captures_dir):
        result.status = STATUS_NO_CAPTURE
        result.message = "--captures directory not found: {0}".format(captures_dir)
        return result

    candidates, rejections, record_count = collect_candidates(captures_dir)
    result.rejections.extend(rejections)
    result.record_count = record_count
    result.candidates = len(candidates)
    result.duplicates = sum(1 for item in rejections if item.reason.startswith("duplicate body sha256"))

    if not candidates:
        result.status = STATUS_NO_CAPTURE
        result.message = "0 candidate request among {0} record(s)".format(record_count)
        return result

    best = pick_candidate(candidates)
    result.selected_seq = best.seq

    tools, problems = build_tools(best)
    prompt = ""
    split_offset: Optional[int] = None
    split_marker: Optional[str] = None
    if best.system_text is None:
        problems.append("no system/developer message content")
    elif not best.system_text.strip():
        problems.append("system/developer message content is empty")
    else:
        prompt, split_offset, split_marker = split_prompt(best.system_text)
        problems.extend(check_prompt(prompt))

    if problems:
        result.status = STATUS_EXTRACT_FAIL
        result.message = "candidate seq={0} ({1}) failed self-check: {2}".format(
            best.seq, best.source, "; ".join(problems)
        )
        return result

    captured_at = rfc3339_utc(best.ts)
    sha_value = tools_sha256(tools)
    document = build_document(cli_version, captured_at, best.order, tools, sha_value)

    shape_problems = check_assets_shape(document)
    if shape_problems:
        result.status = STATUS_EXTRACT_FAIL
        result.message = "candidate seq={0} produced an invalid document: {1}".format(
            best.seq, "; ".join(shape_problems)
        )
        return result

    tools_path, prompt_path = write_assets(out_dir, document, prompt)

    result.status = STATUS_OK
    result.message = "extracted from seq={0}".format(best.seq)
    result.order = list(best.order)
    result.prompt_chars = len(prompt)
    result.prompt_bytes = len((prompt.rstrip() + PROMPT_TRAILING_NEWLINE).encode("utf-8"))
    result.split_offset = split_offset
    result.split_marker = split_marker
    result.captured_at = captured_at
    result.sha256 = sha_value
    result.out_dir = out_dir
    result.official_tools_path = tools_path
    result.prompt_path = prompt_path
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="sink.py captures -> assets/official_tools.json + assets/prompt.txt"
    )
    parser.add_argument("--captures", required=True, help="sink.py --out 目录")
    parser.add_argument("--out", required=True, help="候选资产输出目录")
    parser.add_argument(
        "--cli-version",
        required=True,
        help="抓包那次 `opencode --version` 的实际输出（禁止硬编码）",
    )
    parser.add_argument(
        "--report-json",
        default=None,
        help="可选：把本次抽取指标写到该 JSON 文件（只含计数，不含请求头）",
    )
    args = parser.parse_args(argv)

    result = extract_assets(args.captures, args.out, args.cli_version)

    if result.rejections:
        stream = sys.stderr if not result.ok else sys.stdout
        if not result.ok:
            for rejection in result.rejections:
                print("skip {0}".format(rejection.describe()), file=stream)
        else:
            print(
                "skipped {0} non-candidate record(s)".format(len(result.rejections)),
                file=stream,
            )

    if not result.ok:
        print("ERROR {0}".format(result.status), file=sys.stderr)
        print(result.message, file=sys.stderr)
        if args.report_json:
            _write_report(args.report_json, result)
        return EXIT_FAIL

    print(result.summary())
    if args.report_json:
        _write_report(args.report_json, result)
    return EXIT_OK


def _reason_histogram(rejections: Sequence[Rejection]) -> Dict[str, int]:
    """把被排除记录的原因聚成计数表（只含原因文本，不含路径/请求头）。"""
    buckets: Dict[str, int] = {}
    for rejection in rejections:
        key = re.sub(r"\s+", " ", rejection.reason or "rejected").strip()
        buckets[key] = buckets.get(key, 0) + 1
    return dict(sorted(buckets.items()))


def _write_report(path: str, result: Result) -> None:
    payload = {
        "status": result.status,
        "message": result.message,
        "selected_seq": result.selected_seq,
        "records": result.record_count,
        "candidates": result.candidates,
        "duplicates": result.duplicates,
        "order": result.order,
        "tool_count": len(result.order),
        "prompt_chars": result.prompt_chars,
        "prompt_bytes": result.prompt_bytes,
        "split_offset": result.split_offset,
        "split_marker": result.split_marker,
        "captured_at": result.captured_at,
        "sha256": result.sha256,
        "excluded": _reason_histogram(result.rejections),
        "excluded_total": len(result.rejections),
    }
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


if __name__ == "__main__":
    sys.exit(main())
