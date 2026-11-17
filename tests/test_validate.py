"""tests/test_validate.py — `tools/validate_assets.py` 的行为测试（unittest，零第三方依赖）。

跑法：
    python -m unittest discover -s tests -v

设计：
    正向直接跑仓库里的真实 `assets/`；反例一律"把真实资产复制到临时目录再破坏"，
    这样每个用例只破坏一条不变量，不会因为手写 fixture 自己漂移而给出假绿。

覆盖：契约 §2.2 的 8 条不变量、§3.2 的 3 条 prompt 规则、--now 的 30 天窗口、
已知官方名缺失 / 环境痕迹的 warn→strict 升级、以及 0/1/2 三个退出码。
"""

import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
VALIDATOR = ROOT / "tools" / "validate_assets.py"

# 契约 §8：公开仓库不放真实 prompt。用合成正文，长度达标且不含运行时尾部。
SYNTHETIC_PROMPT = (
    "You are an AI agent running in a coding agent harness. "
    "This synthetic body stands in for the official system prompt so that the "
    "prompt rules stay covered without publishing the real text.\n"
) * 40


def canonical_tools_sha256(tools):
    """与契约 §2.1 相同的 canonical JSON + sha256。"""
    payload = json.dumps(tools, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def run_validator(assets_dir, *extra):
    """跑一次校验，返回 (exit code, stdout+stderr 合并文本)。"""
    proc = subprocess.run(
        [sys.executable, str(VALIDATOR), "--assets", str(assets_dir), *extra],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def parse_rfc3339(text):
    raw = text.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    return dt.datetime.fromisoformat(raw)


def as_rfc3339(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class AssetCase(unittest.TestCase):
    """每个用例一份真实资产的独立副本。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="zen-assets-")
        self.addCleanup(tmp.cleanup)
        self.assets = Path(tmp.name) / "assets"
        shutil.copytree(ASSETS, self.assets)
        # 契约 §8：public runner 仓库不存 prompt 全文，只有 prompt-meta.json。
        # 测试用合成正文顶上，使 prompt 的三条规则仍然被覆盖，同时真实 prompt
        # 不会出现在公开仓库里。
        prompt_path = self.assets / "prompt.txt"
        if not prompt_path.is_file():
            prompt_path.write_bytes(SYNTHETIC_PROMPT.encode("utf-8"))
        self.data = json.loads((self.assets / "official_tools.json").read_text(encoding="utf-8"))
        self.prompt = prompt_path.read_text(encoding="utf-8")

    # ---- 写回helpers -------------------------------------------------
    def save_tools(self, fix_sha=True):
        """写回 official_tools.json；默认重算 sha256，避免破坏用例同时踩到不变量 7。

        用 write_bytes 而不是 write_text：Windows 上 text 模式会把 \\n 翻成 \\r\\n，
        校验脚本按原始字节读，长度/尾部空白断言就会被平台改味。
        """
        if fix_sha:
            self.data["source"]["sha256"] = canonical_tools_sha256(self.data["tools"])
        payload = json.dumps(self.data, ensure_ascii=False, indent=2) + "\n"
        (self.assets / "official_tools.json").write_bytes(payload.encode("utf-8"))

    def save_prompt(self, text):
        (self.assets / "prompt.txt").write_bytes(text.encode("utf-8"))

    def normalize_baseline(self):
        """把临时副本修成"合规基线"：prompt 规整成「恰好一个换行」结尾，tools 按契约重算 sha。

        真实 assets/ 的既有违约只允许 `RealAssetsTest` 去暴露；其余用例断言的是
        "我破坏的那一条"，所以先建立干净基线，否则基线缺陷会把真正的信号淹没。
        """
        self.save_prompt(self.prompt.rstrip() + "\n")
        self.save_tools()

    def expect(self, code_expected, needles=(), extra=()):
        code, out = run_validator(self.assets, *extra)
        self.assertEqual(code, code_expected, f"expected exit {code_expected}, got {code}\n{out}")
        for needle in needles:
            self.assertIn(needle, out, f"output missing {needle!r}\n{out}")
        return out


class RealAssetsTest(AssetCase):
    """正向：仓库里真实的 assets/ 必须通过。

    契约 §8：本仓库不存 prompt 全文，所以固定带 --tools-only；prompt 规则由
    AssetCase 里的合成正文覆盖。
    """

    def test_real_assets_pass_with_zero_exit(self):
        code, out = run_validator(ASSETS, "--tools-only")
        self.assertEqual(code, 0, out)
        self.assertIn("RESULT: PASS", out)

    def test_real_assets_report_every_check_item(self):
        code, out = run_validator(ASSETS, "--tools-only")
        self.assertEqual(code, 0, out)
        for path in (
            "order",
            "tools[*].description",
            "tools[*].parameters",
            "captured_at",
            "source.sha256",
        ):
            self.assertIn(path, out, f"missing report line {path!r}\n{out}")
        self.assertNotIn("[fail]", out)


class InvariantTest(AssetCase):
    """契约 §2.2 的 8 条不变量，每条一个破坏样本。"""

    def test_invariant1_order_shorter_than_eight(self):
        self.data["order"] = self.data["order"][:3]
        self.data["tools"] = {k: v for k, v in self.data["tools"].items() if k in self.data["order"]}
        self.save_tools()
        self.expect(1, ["order", "length 3"])

    def test_invariant2_name_in_tools_missing_from_order(self):
        dropped = self.data["order"].pop(0)
        self.save_tools()
        self.expect(1, ["order", "missing tool name", dropped])

    def test_invariant2_name_in_order_missing_from_tools(self):
        self.data["order"].append("ghost_tool")
        self.save_tools()
        self.expect(1, ["order", "ghost_tool", "not present in tools"])

    def test_invariant3_order_duplicate(self):
        self.data["order"].append(self.data["order"][0])
        self.save_tools()
        self.expect(1, ["order", "duplicate"])

    def test_invariant4_description_whitespace_only(self):
        self.data["tools"]["read"]["description"] = "  \n\t "
        self.save_tools()
        self.expect(1, ["tools.read.description", "empty"])

    def test_invariant5_parameters_not_an_object(self):
        self.data["tools"]["glob"]["parameters"] = "not-an-object"
        self.save_tools()
        self.expect(1, ["tools.glob.parameters", "must be an object"])

    def test_invariant5_parameters_type_not_object(self):
        self.data["tools"]["grep"]["parameters"]["type"] = "array"
        self.save_tools()
        self.expect(1, ["tools.grep.parameters.type", 'expected "object"'])

    def test_invariant6_captured_at_far_future(self):
        self.data["captured_at"] = "2030-01-01T00:00:00Z"
        self.save_tools()
        self.expect(1, ["captured_at", "now+30d"], extra=("--now", "2026-11-17T00:00:00Z"))

    def test_invariant7_source_sha256_mismatch(self):
        self.data["source"]["sha256"] = "0" * 64
        self.save_tools(fix_sha=False)
        self.expect(1, ["source.sha256", "mismatch"])

    def test_invariant8_todays_date_in_tools(self):
        self.data["tools"]["edit"]["description"] += " Today's date: 2026-11-17"
        self.save_tools()
        self.expect(1, ["tools.edit.description", "Today's date:"])

    def test_invariant8_working_directory_nested_in_parameters(self):
        self.data["tools"]["shell"]["parameters"]["properties"]["workdir"]["description"] = (
            "Working directory: /home/runner/work/zen/zen"
        )
        self.save_tools()
        self.expect(
            1,
            ["tools.shell.parameters.properties.workdir.description", "Working directory:"],
        )


class PromptRuleTest(AssetCase):
    """契约 §3.2 的 3 条 prompt 规则。"""

    def test_prompt_rule1_too_short(self):
        self.save_tools()
        self.save_prompt(self.prompt[:500])
        self.expect(1, ["prompt.txt", "length 500"])

    def test_prompt_rule2_forbidden_runtime_tail(self):
        self.save_tools()
        for marker in (
            "Today's date: 2026-11-17",
            "Working directory: /home/runner/work/x",
            "Platform: linux",
            "<env>",
        ):
            with self.subTest(marker=marker):
                self.save_prompt(self.prompt + "\n" + marker)
                self.expect(1, ["prompt.txt", "forbidden substring"])
                self.save_prompt(self.prompt)  # 复原，避免 subTest 之间串味

    def test_prompt_rule3_trailing_blank_lines(self):
        """契约 §3.2-3（2026-11-17 修正）：以恰好一个 \\n 结尾，禁止尾部空行。"""
        self.save_tools()
        base = self.prompt.rstrip("\n")
        cases = [
            (base + "\n", 0, "ends with exactly one newline"),          # 合规
            (base, 1, "no trailing newline"),                            # 缺换行
            (base + "\n\n", 1, "trailing blank line"),                   # 尾部空行
            (base + "  \n", 1, "trailing whitespace"),                   # 行尾空格
            (base + "\t\n", 1, "trailing whitespace"),                   # 行尾制表符
        ]
        for text, code, needle in cases:
            with self.subTest(tail=repr(text[-4:])):
                self.save_prompt(text)
                self.expect(code, [needle])


class ClockWindowTest(AssetCase):
    """--now 对 30 天窗口的影响。"""

    def test_captured_at_inside_window_passes(self):
        captured = parse_rfc3339(self.data["captured_at"])
        self.normalize_baseline()
        self.expect(0, ["captured_at"], extra=("--now", as_rfc3339(captured + dt.timedelta(days=10))))

    def test_captured_at_outside_window_fails(self):
        captured = parse_rfc3339(self.data["captured_at"])
        self.normalize_baseline()
        self.expect(1, ["captured_at", "now+30d"],
                    extra=("--now", as_rfc3339(captured - dt.timedelta(days=31))))


class WarningTest(AssetCase):
    """环境痕迹默认 warn、--strict 时 fail；上游名单默认不比对（契约 §3.4）。"""

    def test_upstream_only_name_is_not_warned(self):
        """experimental_image_generate 按 §2.3 故意移出，不告警、strict 也通过。"""
        self.normalize_baseline()
        out = self.expect(0, [], extra=("--strict",))
        self.assertNotIn("experimental_image_generate", out)
        self.assertIn("upstream comparison disabled", out)

    def test_upstream_name_added_back_does_not_break_strict(self):
        """即便上游名单里有、资产里没有，也不许 fail。"""
        self.normalize_baseline()
        self.data["order"].append("experimental_image_generate")
        self.data["tools"]["experimental_image_generate"] = {
            "description": "Generate an image.",
            "parameters": {"type": "object", "properties": {}},
            "strict": False,
        }
        self.save_tools()
        self.expect(0, [], extra=("--strict",))

    def test_env_trace_warns_then_strict_fails(self):
        self.normalize_baseline()
        self.data["tools"]["read"]["description"] += " (captured under /home/runner/work/zen/zen)"
        self.save_tools()
        self.expect(0, ["[warn]", "tools.read.description", "/home/runner"])
        self.expect(1, ["[fail]", "tools.read.description"], extra=("--strict",))

    def test_windows_user_path_trace_warns(self):
        self.normalize_baseline()
        self.data["tools"]["glob"]["description"] += " (captured under C:\\Users\\runneradmin\\work)"
        self.save_tools()
        self.expect(0, ["[warn]", "tools.glob.description"])

    def test_compare_upstream_builtin_warns_only_when_asked(self):
        """§3.4：只有 --compare-upstream 才提"上游有、资产没有"。"""
        self.normalize_baseline()
        self.expect(
            0,
            ["[warn]", "upstream name(s) absent", "experimental_image_generate"],
            extra=("--compare-upstream", "-"),
        )

    def test_compare_upstream_builtin_strict_turns_into_error(self):
        self.normalize_baseline()
        self.expect(
            1,
            ["[fail]", "experimental_image_generate"],
            extra=("--compare-upstream", "-", "--strict"),
        )

    def test_compare_upstream_reads_json_name_list(self):
        self.normalize_baseline()
        names = self.assets.parent / "upstream_names.json"
        names.write_bytes(json.dumps(["read", "ghost_tool"]).encode("utf-8"))
        self.expect(
            0,
            ["ghost_tool", "upstream name(s) absent"],
            extra=("--compare-upstream", str(names)),
        )

    def test_compare_upstream_missing_file_is_argument_error(self):
        self.normalize_baseline()
        code, out = run_validator(
            self.assets, "--compare-upstream", str(self.assets.parent / "missing.json")
        )
        self.assertEqual(code, 2, out)


class CliTest(AssetCase):
    """退出码语义：0 通过 / 1 校验失败 / 2 参数错误。"""

    def test_missing_prompt_file_is_validation_failure(self):
        (self.assets / "prompt.txt").unlink()
        self.save_tools()
        self.expect(1, ["prompt.txt", "missing file"])

    def test_invalid_json_reports_line_and_column(self):
        (self.assets / "official_tools.json").write_text("{ not json", encoding="utf-8")
        self.expect(1, ["official_tools.json", "invalid JSON", "line"])

    def test_bad_now_is_argument_error(self):
        self.expect(2, ["--now"], extra=("--now", "not-a-date"))

    def test_missing_assets_argument_is_argument_error(self):
        proc = subprocess.run(
            [sys.executable, str(VALIDATOR)],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)

    def test_assets_not_a_directory_is_argument_error(self):
        missing = self.assets.parent / "does-not-exist"
        code, out = run_validator(missing)
        self.assertEqual(code, 2, out)
        self.assertIn("not a directory", out)


class SchemaTest(unittest.TestCase):
    """schema/official_tools.schema.json 自身必须是合法 draft-07，且覆盖真实资产用到的键。

    validate_assets.py 不依赖 schema（手写校验），所以这里只防止 schema 与真实资产
    悄悄脱节：一旦有人给 tool 加了字段却没写进 schema，additionalProperties: false 就会说谎。
    """

    def load_schema(self):
        return json.loads((ROOT / "schema" / "official_tools.schema.json").read_text(encoding="utf-8"))

    def test_schema_is_valid_draft07(self):
        schema = self.load_schema()
        self.assertEqual(schema["$schema"], "http://json-schema.org/draft-07/schema#")
        self.assertEqual(schema["type"], "object")
        for key in ("cli_version", "captured_at", "source", "order", "tools"):
            self.assertIn(key, schema["required"])
        self.assertEqual(schema["properties"]["tools"]["additionalProperties"]["$ref"], "#/definitions/tool")
        self.assertEqual(schema["properties"]["order"]["minItems"], 8)
        self.assertTrue(schema["properties"]["order"]["uniqueItems"])
        self.assertEqual(
            schema["definitions"]["tool"]["properties"]["parameters"]["properties"]["type"]["const"],
            "object",
        )

    def test_schema_covers_every_key_used_by_real_assets(self):
        schema = self.load_schema()
        data = json.loads((ASSETS / "official_tools.json").read_text(encoding="utf-8"))
        self.assertEqual(set(data), set(schema["properties"]))
        self.assertEqual(set(data["source"]), set(schema["properties"]["source"]["properties"]))
        tool_props = set(schema["definitions"]["tool"]["properties"])
        for name, entry in data["tools"].items():
            self.assertLessEqual(set(entry), tool_props, f"tools.{name} has keys beyond the schema")


if __name__ == "__main__":
    unittest.main()
