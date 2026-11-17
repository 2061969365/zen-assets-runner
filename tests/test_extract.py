#!/usr/bin/env python3
"""``tools/extract_assets.py`` 的单元测试 + 真实 fixture 端到端测试。

fixture 由 ``tests/fixtures/build_fixtures.py`` 从 zen-direct-v2 的真实抓包生成，
每条都是 ``tools/sink.py`` 的落盘形状。测试自己组装"场景目录"（子集 + 派生记录），
所以既保持 fixture 是真实记录，又能覆盖 "0 候选 / 缺 system / 工具更多 / UA 不符"。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(ROOT, "tools")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
SCRIPT = os.path.join(TOOLS_DIR, "extract_assets.py")

sys.path.insert(0, TOOLS_DIR)
import extract_assets as ea  # noqa: E402  （路径注入后才能导入）

CLI_VERSION = "2.2.5"
TITLE = "capture_001_title.json"
MAIN = "capture_002_main.json"
RETRY = "capture_003_retry.json"
NOSYSTEM = "capture_004_nosystem.json"
BADUA = "capture_005_badua.json"

#: 抓包机的稳定 24 hex（fixture 里的 x-opencode-project），资产里一个字节都不许出现
PROJECT_HASH = "5f1c0a9b8d7e6f4a3b2c1d0e"

MAIN_ORDER = [
    "edit",
    "glob",
    "grep",
    "question",
    "read",
    "shell",
    "skill",
    "subagent",
    "webfetch",
    "websearch",
    "write",
    "execute",
]


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as handle:
        return json.load(handle)


def body_of(record):
    return json.loads(record["body"])


def system_of(record):
    return body_of(record)["messages"][0]["content"]


class ExtractorTestCase(unittest.TestCase):
    """公共脚手架：临时场景目录 + 直接调用 / CLI 调用两种入口。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zen-assets-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    # -- 场景组装 ---------------------------------------------------------

    def scenario(self, fixtures=(), records=()):
        """把 fixture 子集 + 派生记录拼成一个 sink 输出目录。"""
        path = tempfile.mkdtemp(dir=self.tmp, prefix="captures-")
        for name in fixtures:
            shutil.copyfile(os.path.join(FIXTURES, name), os.path.join(path, name))
        for record in records:
            target = os.path.join(
                path, "{0:03d}-inference_v1_chat_completions.json".format(record["seq"])
            )
            with open(target, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(record, handle, ensure_ascii=False, indent=1)
        return path

    def with_body(self, record, body):
        """保留 sink 记录外壳（seq/ts/headers），只换 body。"""
        clone = json.loads(json.dumps(record))
        clone["body"] = json.dumps(body, ensure_ascii=False, indent=1)
        clone["body_len"] = len(clone["body"].encode("utf-8"))
        return clone

    def wrap_tools(self, names):
        """按真定义合成 chat/completions 的 tools 数组。"""
        definitions = load_fixture("tools_defs.json")
        tools = []
        for name in names:
            payload = definitions[name]
            function = {
                "name": name,
                "description": payload["description"],
                "parameters": payload["parameters"],
            }
            tools.append({"type": "function", "function": function})
        return tools

    # -- 两种入口 ---------------------------------------------------------

    def extract(self, captures, cli_version=CLI_VERSION):
        out = tempfile.mkdtemp(dir=self.tmp, prefix="out-")
        return ea.extract_assets(captures, out, cli_version), out

    def run_cli(self, captures, cli_version=CLI_VERSION):
        out = tempfile.mkdtemp(dir=self.tmp, prefix="out-cli-")
        proc = subprocess.run(
            [
                sys.executable,
                SCRIPT,
                "--captures",
                captures,
                "--out",
                out,
                "--cli-version",
                cli_version,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        return proc, out

    def read_outputs(self, out):
        with open(os.path.join(out, "official_tools.json"), "r", encoding="utf-8") as handle:
            document = json.load(handle)
        with open(os.path.join(out, "prompt.txt"), "rb") as handle:
            prompt_bytes = handle.read()
        return document, prompt_bytes


class NormalExtractionTest(ExtractorTestCase):
    def test_normal_extraction_matches_contract(self):
        """正常抽取：标题请求/重试被排除，产出 §2 的全部字段。"""
        captures = self.scenario([TITLE, MAIN, RETRY])
        result, out = self.extract(captures)

        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.status, ea.STATUS_OK)
        self.assertEqual(result.selected_seq, 2)
        self.assertEqual(result.duplicates, 1)

        document, prompt_bytes = self.read_outputs(out)
        self.assertEqual(set(document), set(ea.TOP_LEVEL_KEYS))
        self.assertEqual(document["cli_version"], CLI_VERSION)
        self.assertEqual(document["captured_at"], "2026-10-24T07:39:47Z")
        self.assertEqual(
            document["source"],
            {
                "workflow": "extract",
                "run_url": None,
                "sha256": document["source"]["sha256"],
            },
        )

        # order：显式有序、与 tools 键集合完全一致、无重复
        self.assertEqual(document["order"], MAIN_ORDER)
        self.assertEqual(set(document["order"]), set(document["tools"]))
        self.assertEqual(len(set(document["order"])), len(document["order"]))

        # tools[*]：只留 description/parameters/strict
        for name, payload in document["tools"].items():
            self.assertEqual(set(payload), set(ea.TOOL_KEYS), name)
            self.assertTrue(payload["description"].strip(), name)
            self.assertIsInstance(payload["parameters"], dict, name)
            self.assertEqual(payload["parameters"].get("type"), "object", name)
            self.assertIsInstance(payload["strict"], bool, name)

        # source.sha256 = tools 的 canonical json 的 sha256
        canonical = json.dumps(
            document["tools"], sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.assertEqual(document["source"]["sha256"], hashlib.sha256(canonical).hexdigest())

        self.assertEqual(len(prompt_bytes), result.prompt_bytes)
        self.assertGreater(len(prompt_bytes), 0)

    def test_prompt_tail_is_cut_at_line_start_marker(self):
        """§3.1：在行首 `Today's date: ` 处切断，尾部空行去掉；§3.2-3：恰好一个换行。"""
        system_text = system_of(load_fixture(MAIN))
        marker_index = system_text.index("\nToday's date: ") + 1
        expected_body = system_text[:marker_index].rstrip()

        captures = self.scenario([TITLE, MAIN])
        result, out = self.extract(captures)
        self.assertTrue(result.ok, result.message)

        self.assertEqual(result.split_offset, marker_index)
        self.assertEqual(result.split_marker, "Today's date: ")

        document, prompt_bytes = self.read_outputs(out)
        prompt = prompt_bytes.decode("utf-8")
        self.assertEqual(prompt, expected_body + "\n")
        self.assertEqual(result.prompt_chars, len(expected_body))
        self.assertEqual(result.prompt_bytes, len(prompt_bytes))

        for token in ("Today's date:", "Working directory:", "Platform:", "<env>"):
            self.assertNotIn(token, prompt)
        # fixture 的 system 正文已换成合成文本（契约 §8：公开仓库不携带上游 prompt）
        self.assertTrue(prompt.startswith("You are an AI agent running in a synthetic harness"))
        self.assertEqual(prompt[:-1].rstrip(), expected_body)
        self.assertTrue(prompt.endswith("\n"))
        self.assertFalse(prompt.endswith("\n\n"))

    def test_dedupe_by_body_sha256(self):
        """同一 body 的重试记录只算一条候选。"""
        captures = self.scenario([MAIN, RETRY])
        result, _out = self.extract(captures)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.candidates, 1)
        self.assertEqual(result.duplicates, 1)
        self.assertEqual(result.selected_seq, 2)

    def test_no_request_headers_in_output(self):
        """§6 第 2 层：资产里不出现任何请求头/凭证。"""
        captures = self.scenario([TITLE, MAIN, RETRY])
        result, out = self.extract(captures)
        self.assertTrue(result.ok, result.message)

        blobs = []
        for name in ("official_tools.json", "prompt.txt"):
            with open(os.path.join(out, name), "r", encoding="utf-8") as handle:
                blobs.append(handle.read())
        text = "\n".join(blobs)

        for needle in (
            "<redacted>",
            "x-opencode-project",
            "x-opencode-session",
            "x-session-id",
            "authorization",
            PROJECT_HASH,
            "Bearer ",
            "ses_",
        ):
            self.assertNotIn(needle, text)


class CandidateSelectionTest(ExtractorTestCase):
    def test_title_request_with_zero_tools_is_excluded(self):
        """实测 CLI 先发 tools=0 的标题请求：不能取第一条。"""
        captures = self.scenario([TITLE])
        result, _out = self.extract(captures)

        self.assertEqual(result.status, ea.STATUS_NO_CAPTURE)
        self.assertEqual(result.candidates, 0)
        self.assertTrue(
            any(rej.seq == 1 and "tools count 0" in rej.reason for rej in result.rejections),
            [rej.describe() for rej in result.rejections],
        )

    def test_picks_candidate_with_most_tools(self):
        """多候选：工具数优先，即使 system 更短、seq 更大。"""
        main = load_fixture(MAIN)
        body = body_of(main)
        short_system = system_of(main)[:4000]
        more_tools_body = {
            "model": body.get("model"),
            "messages": [
                {"role": "system", "content": short_system},
                {"role": "user", "content": "Reply with the single word: ok"},
            ],
            "tools": self.wrap_tools(list(MAIN_ORDER) + ["patch"]),
            "stream": True,
        }
        thirteen = self.with_body(main, more_tools_body)
        thirteen["seq"] = 7

        captures = self.scenario([MAIN], records=[thirteen])
        result, out = self.extract(captures)

        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.selected_seq, 7)
        self.assertEqual(len(result.order), 13)
        self.assertIn("patch", result.order)

        document, _prompt = self.read_outputs(out)
        self.assertEqual(document["order"], MAIN_ORDER + ["patch"])
        self.assertEqual(len(result.order), len(document["tools"]))

    def test_tiebreaks_on_longer_system_prompt(self):
        """工具数相同时取 system 更长的那条（且不是 seq 最小的那条）。"""
        main = load_fixture(MAIN)
        system_text = system_of(main)
        padded = system_text.replace(
            "\nToday's date: ",
            "\n\nExtra static paragraph used only for the tie-break test.\nToday's date: ",
            1,
        )
        body = body_of(main)
        body["messages"][0]["content"] = padded
        longer = self.with_body(main, body)
        longer["seq"] = 9

        captures = self.scenario([MAIN], records=[longer])
        result, _out = self.extract(captures)

        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.selected_seq, 9)
        self.assertEqual(result.prompt_chars, len(padded[: padded.index("\nToday's date: ")].rstrip()))

    def test_non_cli_user_agent_is_excluded(self):
        """UA 不是 CLI 形状且没有 x-opencode-client：排除我们自己网关的请求。"""
        captures = self.scenario([BADUA])
        result, _out = self.extract(captures)

        self.assertEqual(result.status, ea.STATUS_NO_CAPTURE)
        self.assertTrue(
            any(
                rej.seq == 5 and "user-agent" in rej.reason and "x-opencode-client" in rej.reason
                for rej in result.rejections
            ),
            [rej.describe() for rej in result.rejections],
        )

    def test_unknown_tool_name_is_rejected_not_silently_accepted(self):
        """未知工具名只报告、不静默接受（整条请求不算候选）。"""
        main = load_fixture(MAIN)
        body = body_of(main)
        body["tools"].append(
            {"type": "function", "function": {"name": "bogus_tool", "description": "x", "parameters": {"type": "object"}}}
        )
        bogus = self.with_body(main, body)
        bogus["seq"] = 6

        captures = self.scenario([], records=[bogus])
        result, _out = self.extract(captures)

        self.assertEqual(result.status, ea.STATUS_NO_CAPTURE)
        self.assertTrue(
            any(rej.seq == 6 and "unknown tool name" in rej.reason for rej in result.rejections),
            [rej.describe() for rej in result.rejections],
        )

    def test_responses_shape_with_input_array_is_supported(self):
        """`/v1/responses` 形状：system 走 input 里的 parts，不是 messages。"""
        main = load_fixture(MAIN)
        static_text = system_of(main)[:4000].rstrip()
        names = ["edit", "glob", "grep", "read", "shell", "webfetch", "websearch", "write"]
        responses_body = {
            "model": main.get("model"),
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": static_text}]},
                {"role": "user", "content": [{"type": "input_text", "text": "ok"}]},
            ],
            "tools": self.wrap_tools(names),
        }
        record = self.with_body(main, responses_body)
        record["seq"] = 3
        record["path"] = "/inference/v1/responses"

        captures = self.scenario([], records=[record])
        result, out = self.extract(captures)

        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.selected_seq, 3)
        self.assertEqual(result.order, names)
        _document, prompt_bytes = self.read_outputs(out)
        self.assertEqual(prompt_bytes.decode("utf-8"), static_text + "\n")


class FailureSemanticsTest(ExtractorTestCase):
    def test_empty_capture_dir_is_no_capture(self):
        captures = self.scenario([])
        result, _out = self.extract(captures)
        self.assertEqual(result.status, ea.STATUS_NO_CAPTURE)
        self.assertIn("0 candidate request", result.message)

    def test_missing_capture_dir_is_no_capture(self):
        result, _out = self.extract(os.path.join(self.tmp, "does-not-exist"))
        self.assertEqual(result.status, ea.STATUS_NO_CAPTURE)

    def test_missing_system_message_is_extract_fail(self):
        """有候选但缺 system：EXTRACT_FAIL，且信息能定位到 seq。"""
        captures = self.scenario([NOSYSTEM])
        result, _out = self.extract(captures)

        self.assertEqual(result.status, ea.STATUS_EXTRACT_FAIL)
        self.assertEqual(result.selected_seq, 4)
        self.assertIn("seq=4", result.message)
        self.assertIn("system", result.message)

    def test_empty_cli_version_is_extract_fail(self):
        captures = self.scenario([MAIN])
        result, _out = self.extract(captures, cli_version="   ")
        self.assertEqual(result.status, ea.STATUS_EXTRACT_FAIL)
        self.assertIn("cli-version", result.message)

    def test_prompt_without_runtime_tail_still_passes(self):
        """没有运行时尾部时整段都是静态正文（split_offset 为 None）。"""
        main = load_fixture(MAIN)
        body = body_of(main)
        body["messages"][0]["content"] = system_of(main)[:4000]
        static_only = self.with_body(main, body)
        static_only["seq"] = 8

        captures = self.scenario([], records=[static_only])
        result, out = self.extract(captures)

        self.assertTrue(result.ok, result.message)
        self.assertIsNone(result.split_offset)
        _document, prompt_bytes = self.read_outputs(out)
        expected_body = system_of(main)[:4000].rstrip()
        self.assertEqual(prompt_bytes.decode("utf-8"), expected_body + "\n")
        self.assertEqual(result.prompt_bytes, len(expected_body.encode("utf-8")) + 1)


class CliExitCodeTest(ExtractorTestCase):
    def test_success_exit_code_zero(self):
        captures = self.scenario([TITLE, MAIN])
        proc, out = self.run_cli(captures)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=OK", proc.stdout)
        document, _prompt = self.read_outputs(out)
        self.assertEqual(document["order"], MAIN_ORDER)

    def test_no_capture_exit_code_one(self):
        captures = self.scenario([TITLE, BADUA])
        proc, _out = self.run_cli(captures)

        self.assertEqual(proc.returncode, 1)
        self.assertIn("ERROR NO_CAPTURE", proc.stderr)
        # 失败必须能定位到 seq 与原因
        self.assertIn("seq=1", proc.stderr)
        self.assertIn("seq=5", proc.stderr)

    def test_extract_fail_exit_code_one(self):
        captures = self.scenario([NOSYSTEM])
        proc, _out = self.run_cli(captures)

        self.assertEqual(proc.returncode, 1)
        self.assertIn("ERROR EXTRACT_FAIL", proc.stderr)
        self.assertIn("seq=4", proc.stderr)

    def test_cli_writes_report_json(self):
        captures = self.scenario([TITLE, MAIN])
        out = tempfile.mkdtemp(dir=self.tmp, prefix="out-report-")
        report = os.path.join(self.tmp, "report.json")
        proc = subprocess.run(
            [
                sys.executable,
                SCRIPT,
                "--captures",
                captures,
                "--out",
                out,
                "--cli-version",
                CLI_VERSION,
                "--report-json",
                report,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(report, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["selected_seq"], 2)
        self.assertEqual(payload["split_offset"], system_of(load_fixture(MAIN)).index("\nToday's date: ") + 1)
        self.assertEqual(payload["tool_count"], len(MAIN_ORDER))
        self.assertEqual(payload["candidates"], 1)
        self.assertEqual(payload["excluded_total"], 1)  # 只有标题请求被排除
        self.assertTrue(any("tools count 0" in key for key in payload["excluded"]))
        self.assertNotIn("<redacted>", json.dumps(payload))


if __name__ == "__main__":
    unittest.main(verbosity=2)
