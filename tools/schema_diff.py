#!/usr/bin/env python3
"""tools/schema_diff.py — 工具定义的路径级语义 diff。

背景：`refresh-assets` 的 canonical diff 只能报「read.parameters 变了」，
没法回答「哪个属性、从什么、变成什么」。本脚本把两份资产形态的 JSON
递归展开成变更条目，路径统一用 `.` 连接、数组下标用 `[i]`。

输入（自动识别，无需开关）：
  * 资产形态  {"order": [...], "tools": {name: {description, parameters, strict}}}
  * 裸 map    {name: {description, parameters}}

用法：
    python tools/schema_diff.py --old OLD.json --new NEW.json --out DIFF.json [--markdown]

退出码：
    0  两份输入在 order + tools 上完全一致（不写 stdout 正文）
    1  有漂移（工具增删 / 字段变化 / order 顺序变化）
    2  参数、文件或解析错误

设计约束：
  * 只用标准库（json / argparse / pathlib / difflib / unittest 等），不引 jsonschema、deepdiff。
  * 输出顺序稳定：dict 键按字典序遍历，数组按下标遍历，同一对输入每次结果逐字节一致。
  * 长字符串（description 这类）不整段进 JSON：给 old_bytes/new_bytes +
    前 120 字符 + 首个差异的字符偏移。

变更条目形状（kind ∈ added/removed/changed）：
    {"kind": "changed", "path": "tools.read.parameters.properties.path.type",
     "old": "string", "new": ["string", "null"]}
    {"kind": "added", "path": "tools.read.parameters.properties.offset", "new": {...}}
    {"kind": "removed", "path": "tools.grep.parameters.required[2]", "old": "pattern"}
    {"kind": "changed", "path": "tools.subagent.description",
     "old_bytes": 1834, "new_bytes": 1703, "old_chars": 1834, "new_chars": 1703,
     "old_head": "...", "new_head": "...", "first_diff": 412}

数组下标的语义：added 用新数组下标，removed / changed 用旧数组下标。
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

VERSION = 1

# 超过这个字符数的字符串不再原样输出，改成「字节数 + 头部 + 首个差异偏移」。
LONG_STRING_CHARS = 120
# 头部保留的字符数（需求：前 120 字符）。
HEAD_CHARS = 120
# 加入/删除的容器值里，超过这个长度的字符串就地折叠，避免把整段 description 塞进 JSON。
INLINE_STRING_CHARS = 120

EXIT_SAME = 0
EXIT_DRIFT = 1
EXIT_ERROR = 2

TOOLS_PREFIX = "tools."


class DiffError(Exception):
    """输入不可用（读不到、不是 JSON、形状不认识）。"""


# --------------------------------------------------------------------------
# 输入
# --------------------------------------------------------------------------


def _canon(value):
    """canonical JSON：既用于相等判断（区分 1 / 1.0 / true），也用于稳定序列化。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _looks_like_tool_map(mapping):
    if not isinstance(mapping, dict):
        return False
    if not mapping:
        return True
    for value in mapping.values():
        if not isinstance(value, dict):
            return False
        if "parameters" not in value and "description" not in value:
            return False
    return True


def normalize(data, source="<input>"):
    """把两种输入形状归一到 {"shape", "order", "tools"}。"""
    if not isinstance(data, dict):
        raise DiffError(f"{source}: top-level JSON must be an object")

    tools = data.get("tools")
    if isinstance(tools, dict) and _looks_like_tool_map(tools):
        order = data.get("order")
        if order is not None and not isinstance(order, list):
            raise DiffError(f"{source}: 'order' must be an array of tool names")
        return {"shape": "asset", "order": order, "tools": tools}

    if _looks_like_tool_map(data):
        return {"shape": "map", "order": None, "tools": data}

    raise DiffError(
        f"{source}: unrecognized shape; expected "
        '{"order": [...], "tools": {...}} or {name: {"description", "parameters"}}'
    )


def load_input(path):
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise DiffError(f"cannot read {p}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DiffError(f"invalid JSON in {p}: {exc}") from exc
    return {"path": str(p), **normalize(data, str(p))}


# --------------------------------------------------------------------------
# 路径与取值
# --------------------------------------------------------------------------


def _join(path, key):
    return f"{path}.{key}" if path else str(key)


def _at(path, index):
    return f"{path}[{index}]"


def _json_type(value):
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, bool):
        return "boolean"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def _first_diff(old, new):
    """首个不同字符的下标；一方是另一方前缀时返回较短长度。"""
    limit = min(len(old), len(new))
    for i in range(limit):
        if old[i] != new[i]:
            return i
    return limit


def _string_summary(prefix, value):
    return {
        f"{prefix}_bytes": len(value.encode("utf-8")),
        f"{prefix}_chars": len(value),
        f"{prefix}_head": value[:HEAD_CHARS],
    }


def _compact(value):
    """把容器里的长字符串折叠成摘要，结构保持不变。"""
    if isinstance(value, str):
        if len(value) <= INLINE_STRING_CHARS:
            return value
        summary = _string_summary("value", value)
        summary["$type"] = "long-string"
        return summary
    if isinstance(value, dict):
        return {key: _compact(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_compact(item) for item in value]
    return value


def _attach(entry, side, value):
    """给 added/removed/changed 条目挂上单侧的值。"""
    if isinstance(value, str) and len(value) > LONG_STRING_CHARS:
        entry.update(_string_summary(side, value))
    else:
        entry[side] = _compact(value)


# --------------------------------------------------------------------------
# 递归 diff
# --------------------------------------------------------------------------


def _changed(path, old, new):
    entry = {"kind": "changed", "path": path}
    if (
        isinstance(old, str)
        and isinstance(new, str)
        and max(len(old), len(new)) > LONG_STRING_CHARS
    ):
        entry.update(_string_summary("old", old))
        entry.update(_string_summary("new", new))
        entry["first_diff"] = _first_diff(old, new)
        return entry
    _attach(entry, "old", old)
    _attach(entry, "new", new)
    return entry


def _added(path, value):
    entry = {"kind": "added", "path": path}
    _attach(entry, "new", value)
    return entry


def _removed(path, value):
    entry = {"kind": "removed", "path": path}
    _attach(entry, "old", value)
    return entry


def _diff_lists(old, new, path, out):
    """数组 diff。

    用 SequenceMatcher 对齐，这样「中间插一个元素」只报一条 added，
    而不是把后面所有下标都报成 changed。替换区间内优先递归，
    这样 oneOf/items 里的字段变化仍能落到最深的路径。
    """
    matcher = difflib.SequenceMatcher(
        a=[_canon(item) for item in old],
        b=[_canon(item) for item in new],
        autojunk=False,
    )
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":
            for i in range(i1, i2):
                out.append(_removed(_at(path, i), old[i]))
            continue
        if tag == "insert":
            for j in range(j1, j2):
                out.append(_added(_at(path, j), new[j]))
            continue

        # replace：先按位置递归（用旧下标作为 changed 的路径），再补尾部增删。
        paired = min(i2 - i1, j2 - j1)
        for k in range(paired):
            _diff_trees(old[i1 + k], new[j1 + k], _at(path, i1 + k), out)
        for i in range(i1 + paired, i2):
            out.append(_removed(_at(path, i), old[i]))
        for j in range(j1 + paired, j2):
            out.append(_added(_at(path, j), new[j]))


def _diff_trees(old, new, path, out):
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            child = _join(path, key)
            if key not in old:
                out.append(_added(child, new[key]))
            elif key not in new:
                out.append(_removed(child, old[key]))
            else:
                _diff_trees(old[key], new[key], child, out)
        return

    if isinstance(old, list) and isinstance(new, list):
        if old == new:
            return
        _diff_lists(old, new, path, out)
        return

    if _canon(old) == _canon(new):
        return

    out.append(_changed(path, old, new))


# --------------------------------------------------------------------------
# order（单独一类，不算工具内容变化）
# --------------------------------------------------------------------------


def diff_order(old_order, new_order):
    if old_order is None or new_order is None:
        return {
            "comparable": False,
            "changed": False,
            "reason": "one or both inputs are bare maps without an 'order' array",
        }
    old_set = list(old_order)
    new_set = list(new_order)
    added = [name for name in new_set if name not in set(old_set)]
    removed = [name for name in old_set if name not in set(new_set)]
    common = set(old_set) & set(new_set)
    old_common = [name for name in old_set if name in common]
    new_common = [name for name in new_set if name in common]
    return {
        "comparable": True,
        "changed": bool(added or removed or old_common != new_common),
        "reordered": old_common != new_common,
        "added": added,
        "removed": removed,
        "old": old_set,
        "new": new_set,
        "old_common": old_common,
        "new_common": new_common,
    }


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------


def _tool_buckets(changes):
    added, removed, changed = set(), set(), set()
    for entry in changes:
        path = entry["path"]
        if not path.startswith(TOOLS_PREFIX):
            continue
        rest = path[len(TOOLS_PREFIX) :]
        name = rest.split(".", 1)[0].split("[", 1)[0]
        if not name:
            continue
        if rest == name and entry["kind"] == "added":
            added.add(name)
        elif rest == name and entry["kind"] == "removed":
            removed.add(name)
        else:
            changed.add(name)
    return sorted(added), sorted(removed), sorted(changed - added - removed)


def build_diff(old, new, old_input=None, new_input=None):
    changes = []
    _diff_trees(old["tools"], new["tools"], "tools", changes)
    added, removed, changed = _tool_buckets(changes)
    order = diff_order(old.get("order"), new.get("order"))

    summary = {
        "change_count": len(changes),
        "kind_counts": {
            "added": sum(1 for e in changes if e["kind"] == "added"),
            "removed": sum(1 for e in changes if e["kind"] == "removed"),
            "changed": sum(1 for e in changes if e["kind"] == "changed"),
        },
        "tool_count": {"old": len(old["tools"]), "new": len(new["tools"])},
        "tools_added": added,
        "tools_removed": removed,
        "tools_changed": changed,
        "order_changed": bool(order.get("changed")),
        "has_drift": bool(changes) or bool(order.get("changed")),
    }

    return {
        "schema_diff_version": VERSION,
        "inputs": {
            "old": {
                "path": (old_input or {}).get("path"),
                "shape": old["shape"],
                "tool_count": len(old["tools"]),
            },
            "new": {
                "path": (new_input or {}).get("path"),
                "shape": new["shape"],
                "tool_count": len(new["tools"]),
            },
        },
        "summary": summary,
        "order": order,
        "changes": changes,
    }


# --------------------------------------------------------------------------
# 人读输出（只有 --markdown 时才产生）
# --------------------------------------------------------------------------


def _clip(text, limit=96):
    text = text.replace("`", "'").replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _describe(entry):
    kind = entry["kind"]
    if kind in ("added", "removed"):
        side = "new" if kind == "added" else "old"
        if f"{side}_head" in entry:
            return (
                f"{side}_bytes={entry[f'{side}_bytes']} "
                f"head={_clip(entry[f'{side}_head'])}"
            )
        return f"{side}={_clip(_canon(entry[side]))}"
    if "first_diff" in entry:
        return (
            f"{entry['old_bytes']} → {entry['new_bytes']} bytes, "
            f"first diff @ {entry['first_diff']}"
        )
    return f"{_clip(_canon(entry['old']))} → {_clip(_canon(entry['new']))}"


def render_markdown(diff, old_label=None, new_label=None):
    summary = diff["summary"]
    order = diff["order"]
    old_label = old_label or diff["inputs"]["old"]["path"] or "<old>"
    new_label = new_label or diff["inputs"]["new"]["path"] or "<new>"

    lines = [
        "# Schema diff",
        "",
        f"- old: `{old_label}` ({diff['inputs']['old']['shape']}, "
        f"{summary['tool_count']['old']} tools)",
        f"- new: `{new_label}` ({diff['inputs']['new']['shape']}, "
        f"{summary['tool_count']['new']} tools)",
        f"- tools: {summary['tool_count']['old']} -> {summary['tool_count']['new']} "
        f"(added {len(summary['tools_added'])}, removed {len(summary['tools_removed'])})",
        f"- changes: {summary['change_count']} "
        f"(added {summary['kind_counts']['added']}, "
        f"removed {summary['kind_counts']['removed']}, "
        f"changed {summary['kind_counts']['changed']})",
        f"- tools added: {', '.join(summary['tools_added']) or '—'}",
        f"- tools removed: {', '.join(summary['tools_removed']) or '—'}",
        f"- tools changed: {', '.join(summary['tools_changed']) or '—'}",
        f"- order changed: {'yes' if order.get('changed') else 'no'}",
        "",
        "## order",
        "",
    ]

    if not order.get("comparable"):
        lines.append("- 两份输入的至少一侧是裸 map，没有 `order` 可比")
    elif not order["changed"]:
        lines.append("- 无变化")
    else:
        if order.get("reordered"):
            lines.append(
                f"- 相对顺序变化: {', '.join(order['old_common'])} -> "
                f"{', '.join(order['new_common'])}"
            )
        if order["added"]:
            lines.append(f"- 新进入 order: {', '.join(order['added'])}")
        if order["removed"]:
            lines.append(f"- 离开 order: {', '.join(order['removed'])}")

    lines.extend(["", "## changes", ""])
    if not diff["changes"]:
        lines.append("- 无变化")
    else:
        for entry in diff["changes"]:
            lines.append(f"- {entry['kind']} `{entry['path']}`: {_describe(entry)}")

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="schema_diff.py",
        description="路径级 diff 两份工具定义 JSON（资产形态或裸 map 自动识别）",
    )
    parser.add_argument("--old", required=True, help="旧的一份 JSON")
    parser.add_argument("--new", required=True, help="新的一份 JSON")
    parser.add_argument("--out", required=True, help="结构化 diff 的写出路径")
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="同时把可读版本打到 stdout（不加则 stdout 保持空）",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        old = load_input(args.old)
        new = load_input(args.new)
    except DiffError as exc:
        print(f"schema_diff: {exc}", file=sys.stderr)
        return EXIT_ERROR

    diff = build_diff(old, new, old, new)

    out_path = Path(args.out)
    try:
        if out_path.parent and not out_path.parent.exists():
            out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(diff, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"schema_diff: cannot write {out_path}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.markdown:
        sys.stdout.write(render_markdown(diff, args.old, args.new))
        sys.stdout.flush()

    return EXIT_DRIFT if diff["summary"]["has_drift"] else EXIT_SAME


if __name__ == "__main__":
    sys.exit(main())
