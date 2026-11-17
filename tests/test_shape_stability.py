#!/usr/bin/env python3
"""``tools/shape_stability.py`` 的行为测试（unittest，零第三方依赖，零外网）。

跑法::

    python -m unittest discover -s tests -v

两类用例：

1. **纯函数**：集合比对、sha 计算、差异计算、verdict 判定、报告字段完整性。
2. **假 CLI 夹具**：临时目录里现写一个假 ``opencode``（用完即删，不入库），
   它按 ``SHAPE_FAKE_MODE`` 往 sink 发请求，覆盖四种情形：

   * ``same``  —— 每次同一集合            → STABLE / exit 0
   * ``flip``  —— 每次在 12/13 之间跳      → UNSTABLE / exit 1 / distinct_sets=2
   * ``intra`` —— 同一次 run 里两份 body 的集合不同 → 记不稳定证据
   * ``fail``  —— CLI 直接失败（不发请求）  → NO_DATA / exit 2

   另有 ``slow``（配合 --timeout 验证超时保护）、CLI 不存在、``--dry-run``。

隐私断言：夹具用带哨兵的 prompt 跑一遍，断言落盘报告里**一个字符**都不出现。
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "tools"
SCRIPT = TOOLS_DIR / "shape_stability.py"

sys.path.insert(0, str(TOOLS_DIR))
import shape_stability as ss  # noqa: E402  （路径注入后才能导入）

TWELVE = [
    "edit",
    "execute",
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
]

EXTRA = "experimental_image_generate"
THIRTEEN = TWELVE + [EXTRA]

#: 夹具脚本：假装成 opencode，往 OPENCODE_CONFIG_CONTENT 指的 sink 发请求
FAKE_CLI = '''#!/usr/bin/env python3
"""临时假 CLI（测试夹具，并不入库）。"""

import json
import os
import sys
import time
import urllib.request

TWELVE = ["edit", "execute", "glob", "grep", "question", "read", "shell", "skill",
          "subagent", "webfetch", "websearch", "write"]
EXTRA = "experimental_image_generate"
MODE = os.environ.get("SHAPE_FAKE_MODE", "same")
RUN = int(os.environ.get("SHAPE_STABILITY_RUN") or "0")


def base_url():
    raw = os.environ.get("OPENCODE_CONFIG_CONTENT") or "{}"
    return json.loads(raw)["provider"]["opencode"]["options"]["baseURL"]


def post(payload):
    request = urllib.request.Request(
        base_url().rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer fake"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        response.read()


def body(names=None):
    payload = {"model": "fake", "messages": [{"role": "user", "content": "x"}]}
    if names is not None:
        payload["tools"] = [
            {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
            for name in names
        ]
    return payload


def main():
    if "--version" in sys.argv:
        print("9.9.9-fake")
        return 0
    if MODE == "slow":
        time.sleep(60)
        return 0
    if MODE == "fail":
        sys.stderr.write("fake cli refuses to run\\n")
        return 3
    if MODE == "same":
        post(body(None))          # 标题请求：没有 tools
        post(body(TWELVE))
    elif MODE == "flip":
        post(body(None))
        post(body(TWELVE + [EXTRA] if RUN % 2 == 1 else TWELVE))
    elif MODE == "intra":
        post(body(None))
        post(body(TWELVE))
        post(body(TWELVE + [EXTRA]))
    return 0


sys.exit(main())
'''


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def expected_sha(names):
    unique = sorted(set(names))
    return hashlib.sha256("\n".join(unique).encode("utf-8")).hexdigest()


def run_record(run=1, exit_code=0, sha=None, tools=13, intra=True, bodies=2, with_tools=1):
    return {
        "run": run,
        "exit": exit_code,
        "timed_out": False,
        "bodies": bodies,
        "bodies_with_tools": with_tools,
        "tool_counts": [tools] * with_tools,
        "names_sha": sha,
        "body_names_sha": [sha] * with_tools if sha else [],
        "intra_run_consistent": intra,
        "elapsed_s": 1.0,
        "port": 19201,
    }


class NamesShaTest(unittest.TestCase):
    def test_sha_is_order_insensitive(self):
        self.assertEqual(ss.canonical_names_sha(TWELVE), ss.canonical_names_sha(list(reversed(TWELVE))))

    def test_sha_ignores_duplicates(self):
        self.assertEqual(ss.canonical_names_sha(TWELVE + ["edit"]), ss.canonical_names_sha(TWELVE))

    def test_sha_matches_reference_algorithm(self):
        self.assertEqual(ss.canonical_names_sha(THIRTEEN), expected_sha(THIRTEEN))

    def test_sha_changes_with_one_extra_name(self):
        self.assertNotEqual(ss.canonical_names_sha(TWELVE), ss.canonical_names_sha(THIRTEEN))

    def test_sha_ignores_non_string_input(self):
        self.assertEqual(ss.canonical_names_sha(["read", None, 7, "glob"]), ss.canonical_names_sha(["glob", "read"]))


class ToolNamesTest(unittest.TestCase):
    def test_flat_shape(self):
        body = {"tools": [{"name": "read"}, {"name": "grep"}]}
        self.assertEqual(ss.tool_names(body), ["read", "grep"])

    def test_function_wrapper_shape(self):
        body = {"tools": [{"type": "function", "function": {"name": "shell"}}]}
        self.assertEqual(ss.tool_names(body), ["shell"])

    def test_missing_tools_array_is_none(self):
        self.assertIsNone(ss.tool_names({"messages": []}))

    def test_tools_null_is_none(self):
        self.assertIsNone(ss.tool_names({"tools": None}))

    def test_empty_tools_array_is_empty_list(self):
        self.assertEqual(ss.tool_names({"tools": []}), [])

    def test_not_a_dict_is_none(self):
        self.assertIsNone(ss.tool_names(["tools"]))

    def test_skips_unnamed_entries(self):
        body = {"tools": [{"name": "read"}, {"type": "function", "function": {}}, "junk", {"name": "  "}]}
        self.assertEqual(ss.tool_names(body), ["read"])

    def test_names_from_raw_bytes(self):
        raw = json.dumps({"tools": [{"name": "edit"}]}).encode("utf-8")
        self.assertEqual(ss.names_from_body_bytes(raw), ["edit"])

    def test_names_from_invalid_json_is_none(self):
        self.assertIsNone(ss.names_from_body_bytes(b"{not json"))


class GroupAndDiffTest(unittest.TestCase):
    def test_group_counts_and_runs(self):
        entries = [
            ss.body_set_entry(1, THIRTEEN),
            ss.body_set_entry(2, TWELVE),
            ss.body_set_entry(3, THIRTEEN),
        ]
        sets = ss.group_sets(entries)
        self.assertEqual(len(sets), 2)
        self.assertEqual(sets[0]["count"], 2)
        self.assertEqual(sets[0]["tools"], 13)
        self.assertEqual(sets[0]["runs"], [1, 3])
        self.assertEqual(sets[1]["tools"], 12)

    def test_group_bucket_has_no_stray_run_key(self):
        entries = [ss.body_set_entry(1, TWELVE)]
        bucket = ss.group_sets(entries)[0]
        self.assertNotIn("run", bucket)
        self.assertEqual(bucket["names"], sorted(TWELVE))

    def test_diff_superset_reports_only_the_extra_name(self):
        sets = ss.group_sets([ss.body_set_entry(1, THIRTEEN), ss.body_set_entry(2, TWELVE)])
        diffs = ss.diff_between_sets(sets)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0]["names"], [EXTRA])
        self.assertEqual(diffs[0]["only_in"], expected_sha(THIRTEEN))
        self.assertEqual(diffs[0]["missing_from"], expected_sha(TWELVE))

    def test_diff_disjoint_names_appear_in_both_directions(self):
        sets = ss.group_sets([ss.body_set_entry(1, ["read"]), ss.body_set_entry(2, ["write"])])
        diffs = ss.diff_between_sets(sets)
        self.assertEqual(len(diffs), 2)
        self.assertEqual(sorted(item["names"][0] for item in diffs), ["read", "write"])

    def test_diff_identical_sets_is_empty(self):
        sets = ss.group_sets([ss.body_set_entry(1, TWELVE), ss.body_set_entry(2, TWELVE)])
        self.assertEqual(ss.diff_between_sets(sets), [])


class VerdictTest(unittest.TestCase):
    def test_stable_when_every_run_matches(self):
        sha = expected_sha(THIRTEEN)
        runs = [run_record(run=index, sha=sha) for index in (1, 2, 3)]
        self.assertEqual(ss.compute_verdict(runs, 3)[0], ss.VERDICT_STABLE)

    def test_unstable_when_set_changes_between_runs(self):
        runs = [run_record(run=1, sha=expected_sha(THIRTEEN)), run_record(run=2, sha=expected_sha(TWELVE))]
        verdict, reason = ss.compute_verdict(runs, 2)
        self.assertEqual(verdict, ss.VERDICT_UNSTABLE)
        self.assertIn("changed between runs", reason)

    def test_unstable_when_one_run_sends_two_sets(self):
        runs = [run_record(run=1, sha=expected_sha(THIRTEEN), intra=False, with_tools=2)]
        verdict, reason = ss.compute_verdict(runs, 1)
        self.assertEqual(verdict, ss.VERDICT_UNSTABLE)
        self.assertIn("within the same run", reason)

    def test_no_data_when_no_tools_body(self):
        runs = [run_record(run=1, sha=None, with_tools=0, tools=0)]
        self.assertEqual(ss.compute_verdict(runs, 1)[0], ss.VERDICT_NO_DATA)

    def test_no_data_when_runs_are_missing(self):
        runs = [run_record(run=1, sha=expected_sha(TWELVE))]
        verdict, reason = ss.compute_verdict(runs, 3)
        self.assertEqual(verdict, ss.VERDICT_NO_DATA)
        self.assertIn("1/3", reason)

    def test_no_data_when_runs_list_is_empty(self):
        self.assertEqual(ss.compute_verdict([], 5)[0], ss.VERDICT_NO_DATA)

    def test_exit_codes(self):
        self.assertEqual(ss.verdict_exit_code(ss.VERDICT_STABLE), 0)
        self.assertEqual(ss.verdict_exit_code(ss.VERDICT_UNSTABLE), 1)
        self.assertEqual(ss.verdict_exit_code(ss.VERDICT_NO_DATA), 2)


class ReportShapeTest(unittest.TestCase):
    def build(self, runs, entries, requested=None):
        return ss.build_report(
            cli="opencode",
            cli_path="/usr/bin/opencode",
            cli_version="2.3.0",
            model="opencode/deepseek-v4.1-flash-free",
            base_url="http://127.0.0.1:19201",
            runs_requested=requested if requested is not None else len(runs),
            runs=runs,
            body_sets=entries,
            generated_at="2026-01-01T00:00:00Z",
        )

    def test_top_level_keys_are_complete_and_ordered(self):
        report = self.build([], [])
        self.assertEqual(tuple(report.keys()), ss.REPORT_KEYS)

    def test_run_keys_are_complete(self):
        report = self.build([run_record()], [ss.body_set_entry(1, THIRTEEN)])
        self.assertEqual(tuple(report["runs"][0].keys()), ss.RUN_KEYS)

    def test_counts_are_consistent(self):
        runs = [run_record(run=1, sha=expected_sha(THIRTEEN)), run_record(run=2, sha=expected_sha(TWELVE))]
        entries = [ss.body_set_entry(1, THIRTEEN), ss.body_set_entry(2, TWELVE)]
        report = self.build(runs, entries)
        self.assertEqual(report["bodies_total"], 4)
        self.assertEqual(report["bodies_with_tools"], 2)
        self.assertEqual(sum(item["count"] for item in report["sets"]), report["bodies_with_tools"])
        self.assertEqual(report["distinct_sets"], 2)
        self.assertEqual(report["runs_ok"], 2)
        self.assertEqual(report["runs_with_data"], 2)
        self.assertFalse(report["intra_run_mismatch"])
        self.assertEqual(report["verdict"], ss.VERDICT_UNSTABLE)
        self.assertEqual(report["count_basis"], "bodies_with_tools")

    def test_no_prompt_key_anywhere(self):
        report = self.build([run_record()], [ss.body_set_entry(1, THIRTEEN)])
        self.assertEqual(ss.find_prompt_keys(report), [])

    def test_missing_run_fields_become_null(self):
        report = self.build([{"run": 9}], [])
        self.assertIsNone(report["runs"][0]["exit"])
        self.assertEqual(report["runs"][0]["tool_counts"], None)


class PromptLeakTest(unittest.TestCase):
    def test_prompt_text_in_payload_raises(self):
        prompt = "Reply with the single word: ok"
        with self.assertRaises(ValueError):
            ss.assert_no_prompt_text(json.dumps({"note": prompt}), prompt)

    def test_prompt_fragment_in_payload_raises(self):
        prompt = "Reply with the single word: ok, then stop immediately and write nothing else"
        fragment = "then stop immediately and write nothing"[:40]
        with self.assertRaises(ValueError):
            ss.assert_no_prompt_text(json.dumps({"a": fragment}), prompt)

    def test_clean_payload_passes(self):
        ss.assert_no_prompt_text(json.dumps({"sets": TWELVE}), "Reply with the single word: ok")

    def test_empty_prompt_is_noop(self):
        ss.assert_no_prompt_text("anything", "   ")


class BaseUrlTest(unittest.TestCase):
    def test_default_path_is_appended(self):
        base, reason = ss.parse_base_url("http://127.0.0.1:19201")
        self.assertIsNone(reason)
        self.assertEqual(base["port"], 19201)
        self.assertEqual(base["path"], "/inference/v1")

    def test_explicit_path_is_kept(self):
        base, _ = ss.parse_base_url("http://127.0.0.1:19201/custom/v1/")
        self.assertEqual(base["path"], "/custom/v1")

    def test_https_is_rejected(self):
        base, reason = ss.parse_base_url("https://127.0.0.1:19201")
        self.assertIsNone(base)
        self.assertIn("plain http", reason)

    def test_bad_port_is_rejected(self):
        base, reason = ss.parse_base_url("http://127.0.0.1:notaport")
        self.assertIsNone(base)
        self.assertIn("port", reason)

    def test_sink_url_roundtrip(self):
        self.assertEqual(
            ss.sink_url("127.0.0.1", 19201, "/inference/v1"),
            "http://127.0.0.1:19201/inference/v1",
        )


class ResolveCliTest(unittest.TestCase):
    def test_missing_cli_reports_not_found(self):
        argv, reason = ss.resolve_cli("definitely-not-a-real-cli-xyz")
        self.assertIsNone(argv)
        self.assertIn("not found", reason)

    def test_existing_python_file_runs_via_interpreter(self):
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as handle:
            handle.write("print('hi')\n")
            path = handle.name
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        argv, reason = ss.resolve_cli(path)
        self.assertIsNone(reason)
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1], path)

    def test_whole_path_with_spaces_is_not_split(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmpdir, ignore_errors=True))
        path = os.path.join(tmpdir, "my cli.py")
        Path(path).write_text("print('hi')\n", encoding="utf-8")
        argv, reason = ss.resolve_cli(path)
        self.assertIsNone(reason)
        self.assertEqual(argv[-1], path)

    def test_empty_cli_is_rejected(self):
        argv, reason = ss.resolve_cli("   ")
        self.assertIsNone(argv)
        self.assertIn("empty", reason)


class FormatTest(unittest.TestCase):
    def test_run_line_marks_timeout(self):
        record = run_record(exit_code=None)
        record["timed_out"] = True
        line = ss.format_run_line(record)
        self.assertIn("exit=timeout", line)
        self.assertIn("timeout=yes", line)

    def test_plan_never_contains_prompt_text(self):
        base, _ = ss.parse_base_url("http://127.0.0.1:19201")
        plan = ss.build_plan(3, base, "opencode", "model/x", 180.0, 33)
        text = ss.format_plan(plan, 3, 33)
        self.assertIn("would run 3 time(s)", text)
        self.assertIn("run 2/3", text)
        self.assertIn("port=19202", text)
        self.assertNotIn("Reply with the single word", text)
        self.assertEqual(len(plan), 3)


class FakeCliTest(unittest.TestCase):
    """端到端：临时假 CLI + 真 sink + 真报告。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.fake = self.dir / "fake_opencode.py"
        self.fake.write_text(FAKE_CLI, encoding="utf-8")
        self.out = self.dir / "stability.json"

    def probe(self, mode, runs=3, timeout=60, extra=(), prompt=None, cli=None):
        env = dict(os.environ)
        env["SHAPE_FAKE_MODE"] = mode
        argv = [
            sys.executable,
            str(SCRIPT),
            "--runs",
            str(runs),
            "--out",
            str(self.out),
            "--base-url",
            "http://127.0.0.1:{0}".format(free_port()),
            "--cli",
            cli if cli is not None else str(self.fake),
            "--timeout",
            str(timeout),
        ]
        if prompt is not None:
            argv.extend(["--prompt", prompt])
        argv.extend(extra)
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            env=env,
        )

    def read_report(self):
        return json.loads(self.out.read_text(encoding="utf-8"))

    # -- 情形 1：每次同一集合 → STABLE -------------------------------------

    def test_same_set_every_run_is_stable(self):
        proc = self.probe("same", runs=3)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        report = self.read_report()
        self.assertEqual(report["verdict"], "STABLE")
        self.assertEqual(report["distinct_sets"], 1)
        self.assertEqual(report["runs_ok"], 3)
        self.assertEqual(report["runs_with_data"], 3)
        # 每次都有标题请求 + 主请求：bodies 翻倍，带 tools 的只有主请求
        self.assertEqual(report["bodies_total"], 6)
        self.assertEqual(report["bodies_with_tools"], 3)
        self.assertEqual(report["sets"][0]["tools"], 12)
        self.assertEqual(report["sets"][0]["count"], 3)
        self.assertEqual(report["sets"][0]["sha256"], expected_sha(TWELVE))
        self.assertEqual(report["sets"][0]["runs"], [1, 2, 3])
        self.assertEqual(report["runs"][0]["tool_counts"], [12])
        self.assertEqual(report["diff_between_sets"], [])
        self.assertFalse(report["intra_run_mismatch"])
        self.assertIn("STABLE", proc.stdout)

    # -- 情形 2：12/13 来回跳 → UNSTABLE ----------------------------------

    def test_12_vs_13_flip_is_unstable(self):
        proc = self.probe("flip", runs=5)
        self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
        report = self.read_report()
        self.assertEqual(report["verdict"], "UNSTABLE")
        self.assertEqual(report["distinct_sets"], 2)
        self.assertEqual(report["runs_ok"], 5)
        self.assertEqual([item["count"] for item in report["sets"]], [3, 2])
        self.assertEqual([item["tools"] for item in report["sets"]], [13, 12])
        self.assertEqual(len(report["diff_between_sets"]), 1)
        diff = report["diff_between_sets"][0]
        self.assertEqual(diff["names"], [EXTRA])
        self.assertEqual(diff["only_in"], expected_sha(THIRTEEN))
        self.assertEqual(diff["missing_from"], expected_sha(TWELVE))
        # run 序号也得对上：奇数 run 是 13
        self.assertEqual(report["sets"][0]["runs"], [1, 3, 5])
        self.assertEqual(report["sets"][1]["runs"], [2, 4])

    # -- 情形 3：同一次 run 内两份 body 不同 → 不稳定证据 ------------------

    def test_multiple_bodies_in_one_run_are_compared(self):
        proc = self.probe("intra", runs=2)
        self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
        report = self.read_report()
        self.assertTrue(report["intra_run_mismatch"])
        self.assertEqual(report["verdict"], "UNSTABLE")
        for run in report["runs"]:
            self.assertEqual(run["bodies"], 3)
            self.assertEqual(run["bodies_with_tools"], 2)
            self.assertEqual(run["tool_counts"], [12, 13])
            self.assertFalse(run["intra_run_consistent"])
            self.assertEqual(len(run["body_names_sha"]), 2)
            self.assertNotEqual(run["body_names_sha"][0], run["body_names_sha"][1])
        self.assertEqual(report["distinct_sets"], 2)
        self.assertEqual(report["bodies_with_tools"], 4)
        self.assertEqual(report["bodies_total"], 6)

    # -- 情形 4：CLI 失败 / 不存在 → NO_DATA ------------------------------

    def test_failing_cli_is_no_data(self):
        proc = self.probe("fail", runs=2)
        self.assertEqual(proc.returncode, 2, proc.stderr + proc.stdout)
        report = self.read_report()
        self.assertEqual(report["verdict"], "NO_DATA")
        self.assertEqual(report["distinct_sets"], 0)
        self.assertEqual(report["bodies_with_tools"], 0)
        self.assertEqual(report["runs_ok"], 0)
        self.assertEqual([run["exit"] for run in report["runs"]], [3, 3])

    def test_missing_cli_is_environment_error(self):
        proc = self.probe("same", runs=2, cli="definitely-not-a-real-cli-xyz")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not found", proc.stderr)
        report = self.read_report()
        self.assertEqual(report["verdict"], "NO_DATA")
        self.assertEqual(report["runs"], [])
        self.assertIn("environment", report["verdict_reason"])

    def test_timeout_is_recorded_and_next_run_continues(self):
        proc = self.probe("slow", runs=2, timeout=2)
        self.assertEqual(proc.returncode, 2, proc.stderr + proc.stdout)
        report = self.read_report()
        self.assertEqual(report["verdict"], "NO_DATA")
        self.assertEqual(len(report["runs"]), 2)
        for run in report["runs"]:
            self.assertTrue(run["timed_out"])
            self.assertIsNone(run["exit"])
            self.assertEqual(run["bodies"], 0)

    # -- dry-run / 隐私断言 ------------------------------------------------

    def test_dry_run_exits_zero_without_writing(self):
        proc = self.probe("same", runs=4, extra=["--dry-run"])
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("would run 4 time(s)", proc.stdout)
        self.assertIn("run 4/4", proc.stdout)
        self.assertIn("dry run never touches the network", proc.stdout)
        self.assertFalse(self.out.exists())

    def test_dry_run_does_not_need_a_real_cli(self):
        proc = self.probe("same", runs=2, extra=["--dry-run"], cli="definitely-not-a-real-cli-xyz")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("cli resolved: no", proc.stdout)

    def test_report_never_contains_the_prompt(self):
        sentinel = "SENTINEL-PROMPT-7f3a9c-must-not-leak"
        proc = self.probe("flip", runs=2, prompt=sentinel)
        self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
        raw = self.out.read_text(encoding="utf-8")
        self.assertNotIn(sentinel, raw)
        self.assertNotIn("SENTINEL", raw)
        self.assertNotIn(sentinel, proc.stdout)
        self.assertNotIn(sentinel, proc.stderr)
        report = json.loads(raw)
        self.assertEqual(ss.find_prompt_keys(report), [])

    def test_report_never_contains_body_text(self):
        proc = self.probe("same", runs=2)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        raw = self.out.read_text(encoding="utf-8")
        self.assertNotIn('"messages"', raw)
        self.assertNotIn("Authorization", raw)
        self.assertNotIn("Bearer", raw)

    def test_runs_use_distinct_ports(self):
        proc = self.probe("same", runs=3)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        ports = [run["port"] for run in self.read_report()["runs"]]
        self.assertEqual(len(set(ports)), 3, ports)
        self.assertTrue(all(isinstance(port, int) for port in ports))


if __name__ == "__main__":
    unittest.main(verbosity=2)
