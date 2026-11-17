"""tests/test_schema_diff.py — `tools/schema_diff.py` 的行为测试（unittest，零第三方依赖）。

跑法：
    python -m unittest discover -s tests -v

设计：
    每个用例只构造「一处语义变化」，断言路径、kind、old/new 取值，
    避免多个变化互相掩盖。所有用例都走真实 CLI（subprocess），
    把退出码 / stdout / 写出的 JSON 一起断言，因为这三者都是对外契约。

覆盖：
    类型变化、属性增删、required 下标增删、enum 增删、items/oneOf/anyOf 嵌套、
    工具整体增删、长描述的单字节级定位、order 顺序变化（单独一类）、
    完全相同的退出码 0、裸 map 自动识别、--markdown 的输出隔离、
    以及 2（参数/文件/解析错误）的几条分支。
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "schema_diff.py"


def make_tool(description="Read something.", properties=None, required=None, **extra):
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties or {},
        "required": required or [],
    }
    tool = {"description": description, "parameters": parameters}
    tool.update(extra)
    return tool


def base_tools():
    """最小但形状完整的两工具集合，用作各用例的起点。"""
    return {
        "read": make_tool(
            description="Read a file from disk.",
            properties={
                "path": {"description": "File to read", "type": "string"},
                "limit": {"description": "Max lines", "type": "integer"},
            },
            required=["path"],
            strict=False,
        ),
        "grep": make_tool(
            description="Search file contents.",
            properties={"pattern": {"type": "string"}},
            required=["pattern"],
        ),
    }


def asset(tools, order=None):
    return {
        "cli_version": "test",
        "captured_at": "2026-01-01T00:00:00Z",
        "source": {"workflow": "bootstrap", "run_url": None, "sha256": "0" * 64},
        "order": list(order) if order is not None else list(tools),
        "tools": tools,
    }


class SchemaDiffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    # -- helpers ---------------------------------------------------------

    def run_diff(self, old_data, new_data, *extra, old_name="old.json", new_name="new.json"):
        old_path = self.dir / old_name
        new_path = self.dir / new_name
        out_path = self.dir / "diff.json"
        old_path.write_text(
            old_data if isinstance(old_data, str) else json.dumps(old_data, ensure_ascii=False),
            encoding="utf-8",
        )
        new_path.write_text(
            new_data if isinstance(new_data, str) else json.dumps(new_data, ensure_ascii=False),
            encoding="utf-8",
        )
        proc = subprocess.run(
            [
                sys.executable,
                str(TOOL),
                "--old",
                str(old_path),
                "--new",
                str(new_path),
                "--out",
                str(out_path),
                *extra,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        diff = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else None
        return proc, diff, out_path

    def paths(self, diff, kind=None):
        return [
            entry["path"]
            for entry in diff["changes"]
            if kind is None or entry["kind"] == kind
        ]

    def entry(self, diff, path):
        matches = [e for e in diff["changes"] if e["path"] == path]
        self.assertEqual(len(matches), 1, f"expected exactly one change at {path}, got {matches}")
        return matches[0]

    # -- 类型变化 --------------------------------------------------------

    def test_type_change_string_to_array(self):
        old_tools = base_tools()
        new_tools = base_tools()
        new_tools["read"]["parameters"]["properties"]["path"] = {
            "description": "File to read",
            "type": ["string", "null"],
        }

        proc, diff, _ = self.run_diff(asset(old_tools), asset(new_tools))

        self.assertEqual(proc.returncode, 1, proc.stderr)
        entry = self.entry(diff, "tools.read.parameters.properties.path.type")
        self.assertEqual(entry["kind"], "changed")
        self.assertEqual(entry["old"], "string")
        self.assertEqual(entry["new"], ["string", "null"])
        self.assertEqual(diff["summary"]["tools_changed"], ["read"])

    def test_type_change_object_to_scalar_is_reported_at_that_path(self):
        old_tools = base_tools()
        new_tools = base_tools()
        new_tools["read"]["parameters"]["properties"]["path"] = "not-a-schema"

        proc, diff, _ = self.run_diff(old_tools, new_tools)  # 裸 map 也要能跑

        self.assertEqual(proc.returncode, 1, proc.stderr)
        entry = self.entry(diff, "tools.read.parameters.properties.path")
        self.assertEqual(entry["kind"], "changed")
        self.assertEqual(entry["old"]["type"], "string")
        self.assertEqual(entry["new"], "not-a-schema")

    # -- 属性增删 --------------------------------------------------------

    def test_property_added(self):
        old_tools = base_tools()
        new_tools = base_tools()
        new_tools["read"]["parameters"]["properties"]["offset"] = {
            "description": "Start line",
            "minimum": 0,
            "type": "integer",
        }

        proc, diff, _ = self.run_diff(asset(old_tools), asset(new_tools))

        self.assertEqual(proc.returncode, 1, proc.stderr)
        entry = self.entry(diff, "tools.read.parameters.properties.offset")
        self.assertEqual(entry["kind"], "added")
        self.assertEqual(entry["new"]["type"], "integer")
        self.assertEqual(self.paths(diff, "removed"), [])

    def test_property_removed(self):
        old_tools = base_tools()
        new_tools = base_tools()
        del new_tools["read"]["parameters"]["properties"]["limit"]

        proc, diff, _ = self.run_diff(asset(old_tools), asset(new_tools))

        self.assertEqual(proc.returncode, 1, proc.stderr)
        entry = self.entry(diff, "tools.read.parameters.properties.limit")
        self.assertEqual(entry["kind"], "removed")
        self.assertEqual(entry["old"]["type"], "integer")

    # -- required 数组 ---------------------------------------------------

    def test_required_element_removed_uses_old_index(self):
        old_tools = base_tools()
        new_tools = base_tools()
        old_tools["grep"]["parameters"]["required"] = ["path", "pattern"]
        new_tools["grep"]["parameters"]["required"] = ["path"]

        proc, diff, _ = self.run_diff(asset(old_tools), asset(new_tools))

        self.assertEqual(proc.returncode, 1, proc.stderr)
        entry = self.entry(diff, "tools.grep.parameters.required[1]")
        self.assertEqual(entry["kind"], "removed")
        self.assertEqual(entry["old"], "pattern")
        self.assertEqual(len(diff["changes"]), 1)

    def test_required_element_inserted_mid_list_reports_only_one_add(self):
        old_tools = base_tools()
        new_tools = base_tools()
        old_tools["grep"]["parameters"]["required"] = ["path", "pattern"]
        new_tools["grep"]["parameters"]["required"] = ["path", "mode", "pattern"]

        proc, diff, _ = self.run_diff(asset(old_tools), asset(new_tools))

        self.assertEqual(proc.returncode, 1, proc.stderr)
        entry = self.entry(diff, "tools.grep.parameters.required[1]")
        self.assertEqual(entry["kind"], "added")
        self.assertEqual(entry["new"], "mode")
        self.assertEqual(len(diff["changes"]), 1, diff["changes"])

    # -- enum ------------------------------------------------------------

    def test_enum_value_added(self):
        old_tools = base_tools()
        new_tools = base_tools()
        old_tools["read"]["parameters"]["properties"]["mode"] = {
            "type": "string",
            "enum": ["read", "write"],
        }
        new_tools["read"]["parameters"]["properties"]["mode"] = {
            "type": "string",
            "enum": ["read", "write", "append"],
        }

        proc, diff, _ = self.run_diff(old_tools, new_tools)

        self.assertEqual(proc.returncode, 1, proc.stderr)
        entry = self.entry(diff, "tools.read.parameters.properties.mode.enum[2]")
        self.assertEqual(entry["kind"], "added")
        self.assertEqual(entry["new"], "append")

    def test_enum_value_removed(self):
        old_tools = base_tools()
        new_tools = base_tools()
        old_tools["read"]["parameters"]["properties"]["mode"] = {
            "type": "string",
            "enum": ["read", "write"],
        }
        new_tools["read"]["parameters"]["properties"]["mode"] = {
            "type": "string",
            "enum": ["read"],
        }

        proc, diff, _ = self.run_diff(old_tools, new_tools)

        self.assertEqual(proc.returncode, 1, proc.stderr)
        entry = self.entry(diff, "tools.read.parameters.properties.mode.enum[1]")
        self.assertEqual(entry["kind"], "removed")
        self.assertEqual(entry["old"], "write")

    # -- 嵌套 items / oneOf / anyOf --------------------------------------

    def test_items_inner_type_change(self):
        old_tools = base_tools()
        new_tools = base_tools()
        old_tools["read"]["parameters"]["properties"]["paths"] = {
            "type": "array",
            "items": {"type": "string"},
        }
        new_tools["read"]["parameters"]["properties"]["paths"] = {
            "type": "array",
            "items": {"type": "integer"},
        }

        proc, diff, _ = self.run_diff(asset(old_tools), asset(new_tools))

        self.assertEqual(proc.returncode, 1, proc.stderr)
        entry = self.entry(diff, "tools.read.parameters.properties.paths.items.type")
        self.assertEqual((entry["old"], entry["new"]), ("string", "integer"))

    def test_one_of_branch_change(self):
        old_tools = base_tools()
        new_tools = base_tools()
        old_tools["read"]["parameters"]["properties"]["q"] = {
            "oneOf": [{"type": "string"}, {"type": "null"}]
        }
        new_tools["read"]["parameters"]["properties"]["q"] = {
            "oneOf": [{"type": "string"}, {"type": "integer"}]
        }

        proc, diff, _ = self.run_diff(asset(old_tools), asset(new_tools))

        self.assertEqual(proc.returncode, 1, proc.stderr)
        entry = self.entry(diff, "tools.read.parameters.properties.q.oneOf[1].type")
        self.assertEqual((entry["old"], entry["new"]), ("null", "integer"))

    def test_any_of_branch_added(self):
        old_tools = base_tools()
        new_tools = base_tools()
        old_tools["read"]["parameters"]["properties"]["q"] = {
            "anyOf": [{"type": "string"}]
        }
        new_tools["read"]["parameters"]["properties"]["q"] = {
            "anyOf": [{"type": "string"}, {"type": "number"}]
        }

        proc, diff, _ = self.run_diff(asset(old_tools), asset(new_tools))

        self.assertEqual(proc.returncode, 1, proc.stderr)
        entry = self.entry(diff, "tools.read.parameters.properties.q.anyOf[1]")
        self.assertEqual(entry["kind"], "added")
        self.assertEqual(entry["new"], {"type": "number"})

    # -- 工具整体增删 ----------------------------------------------------

    def test_tool_added(self):
        old_tools = base_tools()
        new_tools = base_tools()
        new_tools["patch"] = make_tool(
            description="Apply a patch.",
            properties={"patch": {"type": "string"}},
            required=["patch"],
        )

        proc, diff, _ = self.run_diff(
            asset(old_tools), asset(new_tools, order=list(new_tools))
        )

        self.assertEqual(proc.returncode, 1, proc.stderr)
        entry = self.entry(diff, "tools.patch")
        self.assertEqual(entry["kind"], "added")
        self.assertEqual(entry["new"]["description"], "Apply a patch.")
        self.assertEqual(diff["summary"]["tools_added"], ["patch"])
        self.assertEqual(diff["summary"]["tools_changed"], [])
        self.assertTrue(diff["order"]["changed"])

    def test_tool_removed(self):
        old_tools = base_tools()
        new_tools = base_tools()
        del new_tools["grep"]

        proc, diff, _ = self.run_diff(
            asset(old_tools), asset(new_tools, order=list(new_tools))
        )

        self.assertEqual(proc.returncode, 1, proc.stderr)
        entry = self.entry(diff, "tools.grep")
        self.assertEqual(entry["kind"], "removed")
        self.assertEqual(entry["old"]["description"], "Search file contents.")
        self.assertEqual(diff["summary"]["tools_removed"], ["grep"])

    def test_added_tool_with_long_description_is_compacted(self):
        old_tools = base_tools()
        new_tools = base_tools()
        long_desc = "Patch things carefully. " * 30  # 750 chars
        new_tools["patch"] = make_tool(description=long_desc)

        proc, diff, out_path = self.run_diff(old_tools, new_tools)

        self.assertEqual(proc.returncode, 1, proc.stderr)
        entry = self.entry(diff, "tools.patch")
        self.assertEqual(entry["kind"], "added")
        self.assertEqual(entry["new"]["description"]["$type"], "long-string")
        self.assertEqual(entry["new"]["description"]["value_bytes"], len(long_desc.encode()))
        raw = out_path.read_text(encoding="utf-8")
        self.assertNotIn(long_desc, raw)

    # -- 描述只改一个词 --------------------------------------------------

    def test_description_single_word_change_reports_bytes_and_offset(self):
        old_text = (
            "Read a file from disk and return its contents. "
            "The read is bounded by the limit parameter, and the offset "
            "parameter selects the first line. " * 4
        )
        marker = "bounded by the limit parameter"
        self.assertIn(marker, old_text)
        new_text = old_text.replace(marker, "capped by the limit parameter", 1)
        self.assertGreater(len(old_text), 120)

        old_tools = base_tools()
        new_tools = base_tools()
        old_tools["read"]["description"] = old_text
        new_tools["read"]["description"] = new_text

        proc, diff, out_path = self.run_diff(old_tools, new_tools)

        self.assertEqual(proc.returncode, 1, proc.stderr)
        entry = self.entry(diff, "tools.read.description")
        self.assertEqual(entry["kind"], "changed")
        self.assertEqual(entry["old_bytes"], len(old_text.encode("utf-8")))
        self.assertEqual(entry["new_bytes"], len(new_text.encode("utf-8")))
        self.assertEqual(entry["old_chars"], len(old_text))
        self.assertEqual(entry["new_chars"], len(new_text))
        expected_offset = next(
            i for i, (a, b) in enumerate(zip(old_text, new_text)) if a != b
        )
        self.assertEqual(entry["first_diff"], expected_offset)
        self.assertEqual(entry["old_head"], old_text[:120])
        self.assertEqual(entry["new_head"], new_text[:120])
        self.assertNotIn("old", entry)
        self.assertNotIn("new", entry)

        raw = out_path.read_text(encoding="utf-8")
        self.assertNotIn(old_text, raw)
        self.assertNotIn(new_text, raw)

    def test_short_string_change_keeps_raw_values(self):
        old_tools = base_tools()
        new_tools = base_tools()
        old_tools["read"]["description"] = "Read a file."
        new_tools["read"]["description"] = "Read a document."

        proc, diff, _ = self.run_diff(old_tools, new_tools)

        entry = self.entry(diff, "tools.read.description")
        self.assertEqual(entry["old"], "Read a file.")
        self.assertEqual(entry["new"], "Read a document.")
        self.assertNotIn("first_diff", entry)

    # -- strict / 标量 ---------------------------------------------------

    def test_strict_flag_change(self):
        old_tools = base_tools()
        new_tools = base_tools()
        new_tools["read"]["strict"] = True

        proc, diff, _ = self.run_diff(old_tools, new_tools)

        entry = self.entry(diff, "tools.read.strict")
        self.assertEqual((entry["old"], entry["new"]), (False, True))

    # -- order（单独一类）------------------------------------------------

    def test_order_reorder_is_separate_from_tool_content(self):
        tools = base_tools()
        old_order = ["read", "grep"]
        new_order = ["grep", "read"]

        proc, diff, _ = self.run_diff(
            asset(tools, order=old_order), asset(tools, order=new_order)
        )

        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(diff["changes"], [])
        self.assertEqual(diff["summary"]["change_count"], 0)
        self.assertEqual(diff["summary"]["tools_changed"], [])
        self.assertTrue(diff["order"]["changed"])
        self.assertTrue(diff["order"]["reordered"])
        self.assertEqual(diff["order"]["old_common"], ["read", "grep"])
        self.assertEqual(diff["order"]["new_common"], ["grep", "read"])

    def test_same_order_and_tools_exits_zero(self):
        tools = base_tools()

        proc, diff, out_path = self.run_diff(asset(tools), asset(tools))

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertTrue(out_path.exists())
        self.assertEqual(diff["changes"], [])
        self.assertFalse(diff["summary"]["has_drift"])
        self.assertFalse(diff["order"]["changed"])
        self.assertEqual(diff["summary"]["tools_changed"], [])

    # -- 输入形状 --------------------------------------------------------

    def test_bare_map_is_detected_and_order_not_comparable(self):
        tools = base_tools()

        proc, diff, _ = self.run_diff(tools, tools)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(diff["inputs"]["old"]["shape"], "map")
        self.assertEqual(diff["inputs"]["new"]["shape"], "map")
        self.assertFalse(diff["order"]["comparable"])

    def test_asset_shape_detected(self):
        tools = base_tools()

        proc, diff, _ = self.run_diff(asset(tools), asset(tools))

        self.assertEqual(diff["inputs"]["old"]["shape"], "asset")
        self.assertTrue(diff["order"]["comparable"])

    def test_mixed_shapes_still_diff_tools(self):
        old = asset(base_tools())
        new = base_tools()
        new["read"]["description"] = "Read a file."

        proc, diff, _ = self.run_diff(old, new)

        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertFalse(diff["order"]["comparable"])
        self.assertEqual(self.paths(diff), ["tools.read.description"])

    # -- 退出码 2 的分支 -------------------------------------------------

    def test_invalid_json_exits_two(self):
        proc, diff, out_path = self.run_diff("{not json", asset(base_tools()))

        self.assertEqual(proc.returncode, 2)
        self.assertIsNone(diff)
        self.assertIn("invalid JSON", proc.stderr)

    def test_missing_file_exits_two(self):
        old_path = self.dir / "does-not-exist.json"
        new_path = self.dir / "new.json"
        new_path.write_text(json.dumps(asset(base_tools())), encoding="utf-8")
        out_path = self.dir / "diff.json"
        proc = subprocess.run(
            [
                sys.executable,
                str(TOOL),
                "--old",
                str(old_path),
                "--new",
                str(new_path),
                "--out",
                str(out_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

        self.assertEqual(proc.returncode, 2)
        self.assertIn("cannot read", proc.stderr)

    def test_missing_required_argument_exits_two(self):
        proc = subprocess.run(
            [sys.executable, str(TOOL), "--old", "a.json", "--new", "b.json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

        self.assertEqual(proc.returncode, 2)

    def test_unknown_shape_exits_two(self):
        proc, diff, _ = self.run_diff({"read": "not-a-tool"}, {"read": "not-a-tool"})

        self.assertEqual(proc.returncode, 2)
        self.assertIn("unrecognized shape", proc.stderr)

    # -- markdown 输出隔离 ----------------------------------------------

    def test_markdown_absent_without_flag(self):
        old_tools = base_tools()
        new_tools = base_tools()
        new_tools["read"]["description"] = "Read a file."

        proc, diff, _ = self.run_diff(old_tools, new_tools)

        self.assertEqual(proc.stdout, "")
        self.assertNotIn("# Schema diff", proc.stdout)
        self.assertEqual(proc.returncode, 1)

    def test_markdown_printed_with_flag(self):
        old_tools = base_tools()
        new_tools = base_tools()
        new_tools["read"]["description"] = "Read a file from disk, carefully."

        proc, diff, out_path = self.run_diff(old_tools, new_tools, "--markdown")

        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertTrue(proc.stdout.startswith("# Schema diff"))
        self.assertIn("## changes", proc.stdout)
        self.assertIn("tools.read.description", proc.stdout)
        # JSON 文件与 markdown 互不影响
        self.assertNotIn("# Schema diff", out_path.read_text(encoding="utf-8"))

    # -- 稳定性 ----------------------------------------------------------

    def test_output_is_byte_stable_across_runs(self):
        old_tools = base_tools()
        new_tools = base_tools()
        new_tools["read"]["parameters"]["properties"]["offset"] = {"type": "integer"}
        del new_tools["grep"]["parameters"]["properties"]["pattern"]
        new_tools["read"]["parameters"]["properties"]["path"]["type"] = ["string", "null"]

        _, first, first_path = self.run_diff(
            asset(old_tools), asset(new_tools), old_name="old.json", new_name="new.json"
        )
        first_bytes = first_path.read_bytes()
        _, second, second_path = self.run_diff(
            asset(old_tools), asset(new_tools), old_name="old.json", new_name="new.json"
        )

        self.assertGreater(len(first["changes"]), 0)
        self.assertEqual(first_bytes, second_path.read_bytes())
        self.assertEqual(first, second)

    def test_paths_use_dot_and_bracket_notation(self):
        old_tools = base_tools()
        new_tools = base_tools()
        old_tools["grep"]["parameters"]["required"] = ["path", "pattern"]
        new_tools["grep"]["parameters"]["required"] = ["path"]
        new_tools["read"]["description"] = "Read a file."

        _, diff, _ = self.run_diff(old_tools, new_tools)

        paths = set(self.paths(diff))
        self.assertEqual(
            paths,
            {"tools.grep.parameters.required[1]", "tools.read.description"},
        )
        for path in paths:
            self.assertTrue(path.startswith("tools."), path)
            self.assertNotIn("/", path)
            self.assertNotIn("..", path)
            self.assertNotIn("[", path.replace("required[1]", ""))


if __name__ == "__main__":
    unittest.main()
