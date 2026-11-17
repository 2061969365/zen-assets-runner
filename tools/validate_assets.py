#!/usr/bin/env python3
"""validate_assets.py — 资产业务校验（只读，不碰上游，不改写任何文件）。

唯一依据：`docs/asset-format.md` §2.2（official_tools.json 的 8 条不变量）
与 §3.2（prompt.txt 的 3 条规则，2026-11-17 修订版：恰好一个结尾 `\\n`），
外加 §3.4（上游名单比对默认关闭，显式 `--compare-upstream` 才开）。
本文件手写校验，**不引 jsonschema**；`schema/official_tools.schema.json`
只作文档与未来工具用。

为什么每条失败都要打印字段路径：
    "failed" 三个字在 CI 日志里等于没信息。必须能一眼定位到
    `tools.read.description: empty` 这种粒度，否则修的人还得自己猜。

退出码：
    0  通过（可能有 warning）
    1  校验失败
    2  参数错误（--now / --compare-upstream 读不出来、--assets 不是目录、argparse 报错）

用法：
    python tools/validate_assets.py --assets assets
    python tools/validate_assets.py --assets assets --now 2026-11-17T00:00:00Z
    python tools/validate_assets.py --assets assets --strict
    python tools/validate_assets.py --assets assets --compare-upstream upstream_names.json
"""

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

# §4 已知官方名：**只用于抓包候选过滤**。按 §3.4 它不参与默认资产告警，
# 只有显式 `--compare-upstream -` 时才会拿来和 order 比对。
KNOWN_OFFICIAL_NAMES = (
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

# §2.2-8：出现在 tools 里说明抽错了东西
TOOL_FORBIDDEN_SUBSTRINGS = ("Today's date:", "Working directory:")

# §3.2-2：运行时尾部，由网关自己拼，不得入库
PROMPT_FORBIDDEN_SUBSTRINGS = (
    "Today's date:",
    "Working directory:",
    "Platform:",
    "<env>",
)

# 环境痕迹（抓包机路径），只告警：它是"抽错/固化 CI 环境"的先行指标
ENV_TRACE_MARKERS = ("/home/runner", "C:\\Users", "/home/")

MIN_ORDER_LENGTH = 8
MIN_PROMPT_CHARS = 2000
CLOCK_SKEW_DAYS = 30


def type_name(value):
    """给报错用的类型名，比 type(value).__name__ 更贴近 JSON 语义。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


class Report:
    """收集结果并即时打印；strict 把 warning 就地升级为 error。"""

    def __init__(self, strict=False, stream=None):
        self.strict = strict
        self.stream = stream or sys.stdout
        self.errors = []
        self.warnings = []
        self.ok_count = 0

    def _emit(self, tag, path, message):
        print(f"  [{tag}] {path}: {message}", file=self.stream)

    def ok(self, path, message):
        self.ok_count += 1
        self._emit("ok", path, message)

    def fail(self, path, message):
        self.errors.append(f"{path}: {message}")
        self._emit("fail", path, message)

    def warn(self, path, message):
        if self.strict:
            self.fail(f"{path} (strict)", message)
            return
        self.warnings.append(f"{path}: {message}")
        self._emit("warn", path, message)


def parse_timestamp(text):
    """RFC3339 -> aware UTC datetime；解析不了就抛 ValueError。"""
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"missing or not a non-empty string (got {type_name(text)})")
    raw = text.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"not RFC3339: {text!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def canonical_tools_sha256(tools):
    """契约 §2.1：tools 的 canonical JSON（sort_keys + 紧凑分隔符）的 sha256。"""
    payload = json.dumps(tools, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def walk_strings(value, path):
    """递归产出 (字段路径, 字符串)，路径形如 tools.read.parameters.properties.code.description。"""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key in value:
            child = f"{path}.{key}" if path else str(key)
            yield from walk_strings(value[key], child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from walk_strings(item, f"{path}[{index}]")


# --------------------------------------------------------------------------
# §2.2 不变量
# --------------------------------------------------------------------------


def check_order_length(order, report):
    """不变量 1：order 非空且长度 >= 8。"""
    if not isinstance(order, list):
        report.fail("order", f"missing or not an array (got {type_name(order)})")
        return False
    if len(order) < MIN_ORDER_LENGTH:
        report.fail("order", f"length {len(order)} < required {MIN_ORDER_LENGTH}")
        return False
    report.ok("order", f"length {len(order)} >= {MIN_ORDER_LENGTH}")
    return True


def check_order_members(order, tools, report):
    """不变量 2：set(order) == set(tools.keys())，多一个少一个都是错。"""
    if not isinstance(order, list) or not isinstance(tools, dict):
        report.fail("order", "cannot compare with tools: order or tools has the wrong type")
        return False
    names = set(item for item in order if isinstance(item, str))
    keys = set(tools)
    absent_from_order = sorted(keys - names)  # tools 有、order 没有
    unknown_in_order = sorted(names - keys)  # order 有、tools 没有
    passed = True
    if absent_from_order:
        report.fail("order", f"missing tool name(s) that exist in tools: {absent_from_order}")
        passed = False
    if unknown_in_order:
        report.fail("order", f"unknown name(s) not present in tools: {unknown_in_order}")
        passed = False
    if passed:
        report.ok("order", f"set matches tools ({len(keys)} name(s))")
    return passed


def check_order_unique(order, report):
    """不变量 3：order 无重复。"""
    if not isinstance(order, list):
        report.fail("order", "missing or not an array (got %s)" % type_name(order))
        return False
    bad_index = next((i for i, item in enumerate(order) if not isinstance(item, str)), None)
    if bad_index is not None:
        report.fail(f"order[{bad_index}]", f"not a string (got {type_name(order[bad_index])})")
        return False
    seen = set()
    duplicates = []
    for item in order:
        if item in seen and item not in duplicates:
            duplicates.append(item)
        seen.add(item)
    if duplicates:
        report.fail("order", f"duplicate entrie(s): {duplicates}")
        return False
    report.ok("order", "no duplicate entries")
    return True


def check_descriptions(tools, report):
    """不变量 4：每个 tools[k].description strip 后非空。"""
    if not isinstance(tools, dict):
        report.fail("tools", f"missing or not an object (got {type_name(tools)})")
        return False
    passed = True
    checked = 0
    for name in sorted(tools):
        entry = tools[name]
        if not isinstance(entry, dict):
            report.fail(f"tools.{name}", f"must be an object (got {type_name(entry)})")
            passed = False
            continue
        checked += 1
        path = f"tools.{name}.description"
        description = entry.get("description")
        if description is None:
            report.fail(path, "empty (field missing)")
            passed = False
        elif not isinstance(description, str):
            report.fail(path, f"must be a string (got {type_name(description)})")
            passed = False
        elif not description.strip():
            report.fail(path, "empty (whitespace-only)")
            passed = False
    if passed:
        report.ok("tools[*].description", f"{checked} tool(s) non-empty after strip")
    return passed


def check_parameters(tools, report):
    """不变量 5：每个 tools[k].parameters 是 object 且 type == "object"。"""
    if not isinstance(tools, dict):
        report.fail("tools", f"missing or not an object (got {type_name(tools)})")
        return False
    passed = True
    checked = 0
    for name in sorted(tools):
        entry = tools[name]
        path = f"tools.{name}.parameters"
        parameters = entry.get("parameters") if isinstance(entry, dict) else None
        if not isinstance(parameters, dict):
            report.fail(path, f"must be an object (got {type_name(parameters)})")
            passed = False
            continue
        checked += 1
        declared = parameters.get("type")
        if declared != "object":
            rendered = json.dumps(declared, ensure_ascii=False)
            report.fail(f"{path}.type", f'expected "object", got {rendered}')
            passed = False
    if passed:
        report.ok("tools[*].parameters", f'{checked} tool(s) are objects with "type": "object"')
    return passed


def check_captured_at(data, now, report):
    """不变量 6：captured_at 不得超前 now + 30 天。"""
    raw = data.get("captured_at") if isinstance(data, dict) else None
    if raw is None or not isinstance(raw, str) or not raw.strip():
        report.fail("captured_at", f"missing or not a non-empty string (got {type_name(raw)})")
        return False
    try:
        captured = parse_timestamp(raw)
    except ValueError as exc:
        report.fail("captured_at", str(exc))
        return False
    limit = now + dt.timedelta(days=CLOCK_SKEW_DAYS)
    if captured > limit:
        report.fail(
            "captured_at",
            f"{raw} is > now+{CLOCK_SKEW_DAYS}d (now={now.isoformat()}, limit={limit.isoformat()})",
        )
        return False
    report.ok(
        "captured_at",
        f"{raw} within now+{CLOCK_SKEW_DAYS}d (now={now.isoformat()})",
    )
    return True


def check_source_sha256(data, report):
    """不变量 7：source.sha256 与按契约重算的结果一致。"""
    source = data.get("source") if isinstance(data, dict) else None
    if not isinstance(source, dict):
        report.fail("source", f"missing or not an object (got {type_name(source)})")
        return False
    tools = data.get("tools")
    if not isinstance(tools, dict):
        report.fail("source.sha256", "cannot recompute: tools is missing or not an object")
        return False
    declared = source.get("sha256")
    expected = canonical_tools_sha256(tools)
    if not isinstance(declared, str) or not declared.strip():
        report.fail("source.sha256", f"missing or not a non-empty string (got {type_name(declared)})")
        return False
    if declared.strip().lower() != expected:
        report.fail("source.sha256", f"mismatch: declared {declared} != recomputed {expected}")
        return False
    report.ok("source.sha256", f"matches recomputed {expected[:16]}…")
    return True


def check_tool_text(data, report):
    """不变量 8：tools 内任何字符串不得含 Today's date: / Working directory:。"""
    tools = data.get("tools") if isinstance(data, dict) else None
    if not isinstance(tools, dict):
        return False
    hits = 0
    for path, text in walk_strings(tools, "tools"):
        for marker in TOOL_FORBIDDEN_SUBSTRINGS:
            if marker in text:
                hits += 1
                report.fail(path, f"forbidden substring {marker!r}")
    if hits:
        return False
    report.ok("tools[*]", "no runtime-tail substring (Today's date: / Working directory:)")
    return True


# --------------------------------------------------------------------------
# §3.2 prompt 规则
# --------------------------------------------------------------------------


def check_prompt(text, report):
    """§3.2-1/2/3：长度、禁用子串、尾部空白。"""
    passed = True
    length = len(text)
    if length < MIN_PROMPT_CHARS:
        report.fail("prompt.txt", f"length {length} < required {MIN_PROMPT_CHARS} characters")
        passed = False
    else:
        report.ok("prompt.txt", f"length {length} >= {MIN_PROMPT_CHARS} characters")

    offenders = [marker for marker in PROMPT_FORBIDDEN_SUBSTRINGS if marker in text]
    if offenders:
        for marker in offenders:
            report.fail("prompt.txt", f"forbidden substring {marker!r}")
        passed = False
    else:
        report.ok("prompt.txt", "no runtime tail (Today's date: / Working directory: / Platform: / <env>)")

    # 契约 §3.2-3（2026-11-17 修正）：必须以「恰好一个」\n 结尾；
    # 禁止尾部空行、行尾空格、行尾制表符。原文「不得以空白结尾」字面执行会
    # 把合法的 POSIX 单换行结尾也判死，已改。
    if not text.endswith("\n"):
        report.fail("prompt.txt", "must end with exactly one newline (file has no trailing newline)")
        passed = False
    elif text.endswith("\n\n"):
        report.fail("prompt.txt", "trailing blank line(s) at end of file (expected exactly one \\n)")
        passed = False
    elif text[:-1].endswith((" ", "\t")) or text[:-1].rstrip("\n") != text[:-1].rstrip():
        tail = text[len(text.rstrip()):]
        report.fail("prompt.txt", f"trailing whitespace before final newline {tail!r}")
        passed = False
    else:
        report.ok("prompt.txt", "ends with exactly one newline, no trailing blanks")
    return passed


# --------------------------------------------------------------------------
# 告警（--strict 时 fail）
# --------------------------------------------------------------------------


def check_known_names_present(tools, report):
    """契约 §3.4：默认不比对上游名单。

    experimental_image_generate 等名字是按 §2.3 故意移出的，不产生告警，
    --strict 也必须能通过真实资产。需要「上游有而资产没有」的提醒时，
    走 `--compare-upstream` 显式开启（见 compare_upstream）。
    """
    if not isinstance(tools, dict):
        return
    report.ok("order", f"{len(tools)} tool(s) declared; upstream comparison disabled (contract §3.4)")


def load_upstream_names(source):
    """读上游名单：`-` = 内置 §4 集合；也接受 JSON 数组或含 order/tools 的对象。

    解析失败一律抛 ValueError（或 OSError / json.JSONDecodeError），由 main 判成
    参数错误 exit 2 —— 名单读不出来是调用方的问题，不是资产的问题。
    """
    if source == "-":
        return list(KNOWN_OFFICIAL_NAMES)
    payload = json.loads(Path(source).read_text(encoding="utf-8"))
    if isinstance(payload, list):
        names = [item for item in payload if isinstance(item, str)]
    elif isinstance(payload, dict):
        if isinstance(payload.get("order"), list):
            names = [item for item in payload["order"] if isinstance(item, str)]
        elif isinstance(payload.get("tools"), dict):
            names = list(payload["tools"])
        else:
            raise ValueError("JSON object must carry an 'order' array or a 'tools' object")
    else:
        raise ValueError("JSON must be an array of names or an object with order/tools")
    if not names:
        raise ValueError("no tool names found")
    return names


def compare_upstream(upstream_names, tools, report):
    """契约 §3.4：显式开启的「上游有、资产没有」提醒（默认只 warn，--strict 时 fail）。"""
    if not isinstance(tools, dict):
        return
    missing = [name for name in upstream_names if name not in tools]
    if missing:
        report.warn("order", "upstream name(s) absent from assets: " + ", ".join(missing))
    else:
        report.ok("order", f"all {len(upstream_names)} upstream name(s) present in assets")


def warn_env_traces(tools, report):
    if not isinstance(tools, dict):
        return
    hits = 0
    for path, text in walk_strings(tools, "tools"):
        for marker in ENV_TRACE_MARKERS:
            if marker in text:
                hits += 1
                report.warn(path, f"environment trace {marker!r} in tool text")
    if not hits:
        report.ok("tools[*]", "no environment trace (/home/runner, C:\\Users, /home/)")


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def validate(assets_dir, now=None, strict=False, stream=None, upstream_names=None,
             tools_only=False):
    """校验一个资产目录，返回退出码（0/1）。

    tools_only=True 时跳过 prompt.txt 检查：public runner 仓库按契约 §8 不存放
    prompt 全文（只存 prompt-meta.json 的 sha/字节数），因此该目录没有 prompt.txt
    属于设计如此，不是缺文件。
    """
    assets_dir = Path(assets_dir)
    now = now or dt.datetime.now(dt.timezone.utc)
    report = Report(strict=strict, stream=stream)
    print(
        f"validate_assets: {assets_dir.as_posix()}  now={now.isoformat()}  strict={'on' if strict else 'off'}",
        file=stream or sys.stdout,
    )

    prompt_sha = None

    # ---- official_tools.json
    print("  -- official_tools.json", file=stream or sys.stdout)
    tools_path = assets_dir / "official_tools.json"
    data = None
    if not tools_path.is_file():
        report.fail("official_tools.json", f"missing file: {tools_path.as_posix()}")
    else:
        try:
            raw = tools_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            report.fail("official_tools.json", f"not valid UTF-8: {exc}")
            raw = None
        except OSError as exc:
            report.fail("official_tools.json", f"cannot read: {exc}")
            raw = None
        if raw is not None:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                report.fail(
                    "official_tools.json",
                    f"invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}",
                )
                data = None

    if isinstance(data, dict):
        order = data.get("order")
        tools = data.get("tools")
        check_order_length(order, report)
        check_order_members(order, tools, report)
        check_order_unique(order, report)
        check_descriptions(tools, report)
        check_parameters(tools, report)
        check_captured_at(data, now, report)
        check_source_sha256(data, report)
        check_tool_text(data, report)
        check_known_names_present(tools, report)
        if upstream_names is not None:
            compare_upstream(upstream_names, tools, report)
        warn_env_traces(tools, report)
    elif data is not None:
        report.fail("official_tools.json", f"top level must be a JSON object (got {type_name(data)})")

    # ---- prompt.txt（public runner 仓库按契约 §8 不存全文，tools_only 时跳过）
    if tools_only:
        print("  -- prompt.txt (skipped: --tools-only, contract §8)", file=stream or sys.stdout)
    else:
        print("  -- prompt.txt", file=stream or sys.stdout)
        prompt_path = assets_dir / "prompt.txt"
        if not prompt_path.is_file():
            report.fail("prompt.txt", f"missing file: {prompt_path.as_posix()}")
        else:
            try:
                blob = prompt_path.read_bytes()
            except OSError as exc:
                report.fail("prompt.txt", f"cannot read: {exc}")
                blob = None
            if blob is not None:
                if blob.startswith(b"\xef\xbb\xbf"):
                    report.fail("prompt.txt", "UTF-8 BOM present (contract §3 requires BOM-less UTF-8)")
                    blob = blob[3:]
                try:
                    text = blob.decode("utf-8")
                except UnicodeDecodeError as exc:
                    report.fail("prompt.txt", f"not valid UTF-8: {exc}")
                    text = None
                if text is not None:
                    check_prompt(text, report)
                    prompt_sha = hashlib.sha256(blob).hexdigest()

    status = "FAIL" if report.errors else "PASS"
    stream = stream or sys.stdout
    print(
        f"RESULT: {status} — {report.ok_count} ok, {len(report.errors)} error(s), "
        f"{len(report.warnings)} warning(s)",
        file=stream,
    )
    if prompt_sha:
        print(f"prompt.txt sha256={prompt_sha}", file=stream)
    return 1 if report.errors else 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="validate_assets.py",
        description="按 docs/asset-format.md 校验 assets/ 目录（只读）。",
    )
    parser.add_argument("--assets", required=True, metavar="DIR", help="含 official_tools.json 与 prompt.txt 的目录")
    parser.add_argument("--now", default=None, metavar="ISO8601", help="覆盖当前时间，用于复现 captured_at 窗口判定")
    parser.add_argument("--strict", action="store_true", help="把 warning 升级为 error")
    parser.add_argument(
        "--compare-upstream",
        default=None,
        metavar="FILE",
        help="显式比对上游名单：`-` = 内置 §4 集合，或 JSON 数组 / 含 order、tools 的对象（契约 §3.4，默认关闭）",
    )
    parser.add_argument(
        "--tools-only",
        action="store_true",
        help="跳过 prompt.txt 检查（public runner 仓库按契约 §8 只存 prompt 的 sha/字节数）",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    now = dt.datetime.now(dt.timezone.utc)
    if args.now is not None:
        try:
            now = parse_timestamp(args.now)
        except ValueError as exc:
            print(f"--now: {exc}", file=sys.stderr)
            return 2

    upstream_names = None
    if args.compare_upstream is not None:
        try:
            upstream_names = load_upstream_names(args.compare_upstream)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"--compare-upstream: {exc}", file=sys.stderr)
            return 2

    assets_dir = Path(args.assets)
    if not assets_dir.is_dir():
        print(f"--assets: not a directory: {assets_dir.as_posix()}", file=sys.stderr)
        return 2

    return validate(assets_dir, now=now, strict=args.strict, upstream_names=upstream_names,
                    tools_only=args.tools_only)


if __name__ == "__main__":
    sys.exit(main())
