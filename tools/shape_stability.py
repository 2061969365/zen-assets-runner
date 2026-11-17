#!/usr/bin/env python3
"""shape_stability.py —— 探针：同一句 prompt 反复抓包，比对每次的工具名集合。

它只回答一个问题：**官方 CLI 自己发出的工具集稳不稳定？**

为什么必须单独测
----------------
同一版本（v2.3.0）、同一句 prompt、相隔 2 秒、各自独立进程：主抓包请求里工具
**12** 个（``edit execute glob grep question read shell skill subagent webfetch
websearch write``，没有 ``patch``，没有 ``experimental_image_generate``），紧接着
的形态探测请求里 **13** 个（多一个 ``experimental_image_generate``）。

如果 CLI 自己都抖，那"客户端工具名集合完全等于官方集合"就不能当网关透传判据。
先把抖动幅度测出来，再谈判据——这个脚本就是量尺。

每次 run 都是一次完全隔离的抓包
-------------------------------
::

    run N:
      1. 起一个只存在于内存的 sink（独立端口：优先 --base-url 的端口 + N-1，
         被占用就退到临时端口）
      2. 独立工作目录（系统临时目录下的 run-NN，跑完删除；--keep-dirs 可留）
      3. 用 OPENCODE_CONFIG_CONTENT 把 CLI 的 baseURL 指到该 sink
      4. 起 CLI 跑同一句 prompt（单 run --timeout 秒，超时记 timeout 继续下一个）
      5. 收 body，抽出 ``tools[*]`` 的名字集合
      6. 停 sink、删目录

比对只认"带 tools 的 body"
--------------------------
CLI 会同时发标题请求（``tools`` 为 0 或缺失）和主请求，所以**不能假设每次只有
一份 body**。标题 body 记进 ``bodies`` 但不进比对；同时单独记"同一次 run 里多份
body 的工具集是否一致"——它自己也是不稳定的证据。

集合 sha256 与 workflow 里 ``shapes/matrix.json`` 的 ``names_sha`` 同算法：
``sha256("\\n".join(sorted(set(names))))``。

隐私边界（硬性）
----------------
只输出工具名、数量、sha256、耗时、退出码、端口、序号。**绝不**输出/落盘 prompt
正文、请求体、请求头、CLI 的 stdout/stderr。落盘前对整份 JSON 做一次 prompt 包含
检查，命中即中止（正常永远不命中，这是保险丝）。

退出码
------
::

    0  STABLE     每次都同一个工具集，且每次 run 内部没有冲突
    1  UNSTABLE   集合变了，或同一次 run 内部前后两份 body 的集合不同
    2  环境/无数据 CLI 不存在、--base-url 非法、端口起不来、一次 tools body 都没收到
                  （NO_DATA 也退 2：没有判据就不算"稳定"）

用法
----
::

    python tools/shape_stability.py --runs 5 --out stability.json
    python tools/shape_stability.py --runs 5 --out stability.json --dry-run
    python tools/shape_stability.py --runs 5 --out stability.json \
        --base-url http://127.0.0.1:19201 --cli opencode --timeout 180
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

EXIT_STABLE = 0
EXIT_UNSTABLE = 1
EXIT_ENV = 2

VERDICT_STABLE = "STABLE"
VERDICT_UNSTABLE = "UNSTABLE"
VERDICT_NO_DATA = "NO_DATA"

DEFAULT_CLI = "opencode"
DEFAULT_PROMPT = "Reply with the single word: ok"
DEFAULT_MODEL = "opencode/deepseek-v4.1-flash-free"
DEFAULT_BASE_URL = "http://127.0.0.1:19201"
DEFAULT_SINK_PATH = "/inference/v1"
DEFAULT_TIMEOUT = 180.0

#: sink 回的 Chat SSE（与 tools/sink.py 一致：让 CLI 自己收尾，不发上游、不耗额度）
SINK_SSE = (
    b'data: {"id":"chatcmpl-sink","object":"chat.completion.chunk",'
    b'"created":0,"model":"sink","choices":[{"index":0,'
    b'"delta":{"role":"assistant","content":"ok"},'
    b'"finish_reason":null}]}\n\n'
    b'data: {"id":"chatcmpl-sink","object":"chat.completion.chunk",'
    b'"created":0,"model":"sink","choices":[{"index":0,"delta":{},'
    b'"finish_reason":"stop"}],'
    b'"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
    b'data: [DONE]\n\n'
)

#: 报告顶层字段（顺序即输出顺序；单测按这张表断言完整性）
REPORT_KEYS = (
    "tool",
    "generated_at",
    "cli",
    "cli_path",
    "cli_version",
    "model",
    "base_url",
    "runs_requested",
    "runs_ok",
    "runs_with_data",
    "verdict",
    "verdict_reason",
    "distinct_sets",
    "count_basis",
    "bodies_total",
    "bodies_with_tools",
    "intra_run_mismatch",
    "sets",
    "diff_between_sets",
    "runs",
)

#: 单 run 字段（单测按这张表断言完整性）
RUN_KEYS = (
    "run",
    "exit",
    "timed_out",
    "bodies",
    "bodies_with_tools",
    "tool_counts",
    "names_sha",
    "body_names_sha",
    "intra_run_consistent",
    "elapsed_s",
    "port",
)


# ---------------------------------------------------------------------------
# 小工具：sha / 名字抽取 / 集合比对
# ---------------------------------------------------------------------------


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_names_sha(names: Iterable[str]) -> str:
    """工具名集合的稳定摘要：排序 + 去重 + ``\\n`` 连接，与 workflow 的 names_sha 一致。"""
    unique = sorted({name for name in names if isinstance(name, str)})
    return sha256_text("\n".join(unique))


def tool_names(body: Any) -> Optional[List[str]]:
    """从请求体抽工具名。

    返回 ``None`` 表示"这份 body 没有 tools 数组"（标题请求）；
    返回 ``[]`` 表示"有 tools 数组但一个可用名字都没有"（畸形）。
    """
    if not isinstance(body, dict):
        return None
    tools = body.get("tools")
    if not isinstance(tools, list):
        return None
    names: List[str] = []
    for entry in tools:
        inner = entry.get("function") if isinstance(entry, dict) else None
        source = inner if isinstance(inner, dict) else entry
        name = source.get("name") if isinstance(source, dict) else None
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def names_from_body_bytes(raw: Any) -> Optional[List[str]]:
    """把 sink 收到的原始 body 解析成名字列表；解析不了按"没有 tools"处理。"""
    if isinstance(raw, dict):
        return tool_names(raw)
    if not isinstance(raw, (str, bytes, bytearray)):
        return None
    try:
        text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    return tool_names(parsed)


def body_set_entry(run: int, names: Sequence[str]) -> Dict[str, Any]:
    """一份 body 的集合记录（内部结构；序列化进 ``sets`` 前会去掉 ``run``）。"""
    unique = sorted({name for name in names if isinstance(name, str)})
    return {
        "run": run,
        "names": unique,
        "sha256": canonical_names_sha(unique),
        "tools": len(unique),
    }


def group_sets(body_sets: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 sha256 分组：次数、工具数、名字、出现在哪些 run。"""
    buckets: Dict[str, Dict[str, Any]] = {}
    for item in body_sets:
        sha = item["sha256"]
        bucket = buckets.get(sha)
        if bucket is None:
            bucket = {
                "sha256": sha,
                "count": 0,
                "tools": item["tools"],
                "names": list(item["names"]),
                "runs": [],
            }
            buckets[sha] = bucket
        bucket["count"] += 1
        if item["run"] not in bucket["runs"]:
            bucket["runs"].append(item["run"])
    ordered = sorted(buckets.values(), key=lambda s: (-s["count"], s["sha256"]))
    for bucket in ordered:
        bucket["runs"].sort()
    return ordered


def diff_between_sets(sets: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """两两差异：谁多出来哪些名字、相对谁多。

    每个候选取向只输出**非空**的那一侧，所以严格超集（13 vs 12）只产出一条；
    两边各有独有名字时两条都会出现。
    """
    ordered = sorted(sets, key=lambda s: (-s["count"], -s["tools"], s["sha256"]))
    diffs: List[Dict[str, Any]] = []
    for left in ordered:
        for right in ordered:
            if left["sha256"] == right["sha256"]:
                continue
            only = sorted(set(left["names"]) - set(right["names"]))
            if not only:
                continue
            diffs.append(
                {
                    "only_in": left["sha256"],
                    "missing_from": right["sha256"],
                    "names": only,
                }
            )
    return diffs


def compute_verdict(
    runs: Sequence[Dict[str, Any]], runs_requested: int
) -> Tuple[str, str]:
    """判定 STABLE / UNSTABLE / NO_DATA，返回 (verdict, reason)。"""
    tools_bodies = sum(int(run.get("bodies_with_tools") or 0) for run in runs)
    if tools_bodies == 0:
        return VERDICT_NO_DATA, "no run produced a body carrying tools"

    mismatched = [run["run"] for run in runs if not run.get("intra_run_consistent", True)]
    if mismatched:
        return (
            VERDICT_UNSTABLE,
            "run(s) {0} sent more than one tool set within the same run".format(
                ", ".join(str(item) for item in mismatched)
            ),
        )

    hashes = {run["names_sha"] for run in runs if run.get("names_sha")}
    if len(hashes) > 1:
        return (
            VERDICT_UNSTABLE,
            "the tool-name set changed between runs ({0} distinct sets)".format(len(hashes)),
        )

    with_data = sum(1 for run in runs if (run.get("bodies_with_tools") or 0) > 0)
    if with_data < runs_requested:
        return (
            VERDICT_NO_DATA,
            "{0}/{1} run(s) produced no tools-bearing body".format(with_data, runs_requested),
        )
    return VERDICT_STABLE, "every run sent the same tool-name set"


def assert_no_prompt_text(payload: str, prompt: str) -> None:
    """保险丝：整份报告里不许出现 prompt 正文（整串和 >=32 字符的片段都不许）。"""
    if not isinstance(prompt, str):
        return
    needle = prompt.strip()
    if not needle:
        return
    if needle in payload:
        raise ValueError("report would contain the prompt text; refusing to write")
    window, step = 32, 16
    if len(needle) < window:
        return
    for start in range(0, len(needle) - window + 1, step):
        piece = needle[start : start + window]
        if piece.strip() and piece in payload:
            raise ValueError("report would contain a prompt fragment; refusing to write")


def find_prompt_keys(node: Any, path: str = "") -> List[str]:
    """递归找出名字里带 ``prompt`` 的键（单测用：报告结构本身也不许有这种键）。"""
    hits: List[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and "prompt" in key.lower():
                hits.append(path + "/" + key)
            hits.extend(find_prompt_keys(value, path + "/" + str(key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            hits.extend(find_prompt_keys(value, "{0}[{1}]".format(path, index)))
    return hits


# ---------------------------------------------------------------------------
# 内存 sink（独立端口、不落盘）
# ---------------------------------------------------------------------------


class Sink:
    """只收 body 的本地明文 sink；body 只留在内存里。"""

    def __init__(self, host: str, preferred_port: int):
        self.host = host
        self.bodies: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        ThreadingHTTPServer.allow_reuse_address = True
        self._server: ThreadingHTTPServer
        try:
            self._server = self._make_server(preferred_port)
            self.port = int(self._server.server_address[1])
            self.requested_port = preferred_port
        except OSError:
            # 优先端口被占：退到临时端口，端口号仍记进报告
            self._server = self._make_server(0)
            self.port = int(self._server.server_address[1])
            self.requested_port = preferred_port
        self._server.daemon_threads = True
        self._thread: Optional[threading.Thread] = None

    def _make_server(self, port: int) -> ThreadingHTTPServer:
        return ThreadingHTTPServer((self.host, port), _make_handler(self))

    def record(self, path: str, raw: bytes) -> None:
        with self._lock:
            self.bodies.append({"path": path, "raw": raw, "ts": time.time()})

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.bodies)

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        try:
            self._server.shutdown()
        except Exception:
            pass
        try:
            self._server.server_close()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=5)


def _make_handler(sink: Sink):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _json(self, code: int, obj: Any) -> None:
            payload = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 （BaseHTTPRequestHandler 约定）
            self._json(200, {"data": [], "object": "list"})

        def do_POST(self) -> None:  # noqa: N802
            try:
                raw = self._read_body()
                sink.record(self.path, raw)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(SINK_SSE)))
                self.end_headers()
                self.wfile.write(SINK_SSE)
            except Exception:
                traceback.print_exc()
                try:
                    self._json(500, {"error": "sink failure"})
                except Exception:
                    pass

        def _read_body(self) -> bytes:
            encoding = (self.headers.get("Transfer-Encoding") or "").lower()
            if "chunked" in encoding:
                return self._read_chunked()
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            return self.rfile.read(length) if length > 0 else b""

        def _read_chunked(self) -> bytes:
            chunks: List[bytes] = []
            while True:
                line = self.rfile.readline(65536)
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    size = int(line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    while True:
                        trailer = self.rfile.readline(65536)
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)  # 丢掉每个 chunk 结尾的 CRLF
            return b"".join(chunks)

        def log_message(self, *args: Any) -> None:
            pass

    return Handler


# ---------------------------------------------------------------------------
# CLI / 端口 / 计划
# ---------------------------------------------------------------------------


def resolve_cli(spec: str) -> Tuple[Optional[List[str]], Optional[str]]:
    """把 ``--cli`` 解析成 argv。

    规则（按优先级）：
      1. 整串就是一个存在的文件 → 直接当可执行文件（Windows 路径带反斜杠也安全）
      2. 否则按 shell 词法拆成 ``[程序, 参数...]``，程序走 PATH 查找
      3. ``*.py`` 自动用当前解释器起（夹具用得到，真 CLI 用不到）
    """
    if not isinstance(spec, str) or not spec.strip():
        return None, "--cli is empty"
    spec = spec.strip()
    if os.path.isfile(spec):
        parts = [spec]
    else:
        try:
            parts = shlex.split(spec)
        except ValueError as exc:
            return None, "--cli is not parseable ({0})".format(exc)
    if not parts:
        return None, "--cli is empty"

    program = parts[0]
    resolved = shutil.which(program) if not os.path.isfile(program) else program
    if resolved is None:
        return None, "cli not found: {0!r} (PATH lookup failed)".format(program)
    argv = [resolved] + list(parts[1:])
    if resolved.lower().endswith(".py"):
        argv = [sys.executable] + argv
    return argv, None


def parse_base_url(base_url: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """把 ``--base-url`` 拆成 (scheme, host, port, path)；非法就报错。"""
    if not isinstance(base_url, str) or not base_url.strip():
        return None, "--base-url is empty"
    try:
        parts = urlsplit(base_url.strip())
    except ValueError as exc:
        return None, "--base-url is not parseable ({0})".format(exc)
    if parts.scheme.lower() != "http":
        return None, "--base-url must be plain http (sink serves no TLS): {0!r}".format(base_url)
    host = parts.hostname or "127.0.0.1"
    try:
        port = parts.port or 80
    except ValueError as exc:
        return None, "--base-url has an invalid port ({0})".format(exc)
    path = parts.path or ""
    if path in ("", "/"):
        path = DEFAULT_SINK_PATH
    path = "/" + path.strip("/")
    return {"scheme": "http", "host": host, "port": port, "path": path}, None


def sink_url(host: str, port: int, path: str) -> str:
    return urlunsplit(("http", "{0}:{1}".format(host, port), path, "", ""))


def probe_cli_version(argv: Sequence[str], timeout: float) -> Optional[str]:
    """跑 ``<cli> --version``，取最后一行非空输出；失败返回 None（不影响判定）。"""
    try:
        proc = subprocess.run(
            list(argv) + ["--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(5.0, min(timeout, 120.0)),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    if not lines:
        lines = [line.strip() for line in (proc.stderr or "").splitlines() if line.strip()]
    return lines[-1] if lines else None


def build_plan(
    runs: int, base: Dict[str, Any], cli_spec: str, model: str, timeout: float, prompt_chars: int
) -> List[Dict[str, Any]]:
    """每次 run 的端口/目录/参数（--dry-run 打印用；执行时按同样规则落地）。"""
    plan: List[Dict[str, Any]] = []
    for index in range(1, runs + 1):
        plan.append(
            {
                "run": index,
                "port": base["port"] + index - 1,
                "host": base["host"],
                "path": base["path"],
                "cli": cli_spec,
                "argv": ["run", "-m", model, "<prompt: {0} chars, not printed>".format(prompt_chars)],
                "timeout": timeout,
            }
        )
    return plan


def format_plan(plan: Sequence[Dict[str, Any]], runs: int, prompt_chars: int) -> str:
    lines = [
        "[dry-run] would run {0} time(s); nothing executed, no port bound, no CLI started".format(runs),
        "[dry-run] per-run isolation: own port (fallback to ephemeral if taken) + own temp dir",
        "[dry-run] prompt: {0} chars, never printed nor written".format(prompt_chars),
    ]
    for item in plan:
        lines.append(
            "[dry-run] run {run}/{runs}: cli={cli!r} port={port} host={host} path={path} "
            "timeout={timeout}s argv={argv}".format(
                run=item["run"],
                runs=runs,
                cli=item["cli"],
                port=item["port"],
                host=item["host"],
                path=item["path"],
                timeout=item["timeout"],
                argv=item["argv"],
            )
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 单 run
# ---------------------------------------------------------------------------


def run_once(
    index: int,
    total: int,
    argv: Sequence[str],
    base: Dict[str, Any],
    prompt: str,
    model: str,
    timeout: float,
    work_root: str,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """跑一次 CLI，返回 (run 记录, 每份带 tools 的 body 的集合记录)。"""
    workdir = os.path.join(work_root, "run-{0:02d}".format(index))
    os.makedirs(workdir, exist_ok=True)

    sink = Sink(base["host"], base["port"] + index - 1)
    sink.start()
    started = time.monotonic()
    exit_code: Optional[int] = None
    timed_out = False
    try:
        config = {"provider": {"opencode": {"options": {"baseURL": sink_url(base["host"], sink.port, base["path"])}}}}
        env = dict(os.environ)
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
        # 夹具用得上（真 CLI 忽略）：让"第几次 run"可被判据感知
        env["SHAPE_STABILITY_RUN"] = str(index)
        env["SHAPE_STABILITY_TOTAL"] = str(total)

        proc = subprocess.Popen(
            list(argv) + ["run", "-m", model, prompt],
            cwd=workdir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            exit_code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
    finally:
        elapsed = time.monotonic() - started
        bodies = sink.snapshot()
        sink.stop()

    body_sets: List[Dict[str, Any]] = []
    tool_counts: List[int] = []
    body_shas: List[str] = []
    for body in bodies:
        names = names_from_body_bytes(body.get("raw"))
        if not names:
            # 标题请求（无 tools / tools 为空）：计入 bodies，不进比对
            continue
        entry = body_set_entry(index, names)
        body_sets.append(entry)
        tool_counts.append(entry["tools"])
        body_shas.append(entry["sha256"])

    intra_consistent = len(set(body_shas)) <= 1
    if body_shas:
        names_sha: Optional[str] = body_shas[0] if intra_consistent else canonical_names_sha(
            name for entry in body_sets for name in entry["names"]
        )
    else:
        names_sha = None

    record = {
        "run": index,
        "exit": exit_code,
        "timed_out": timed_out,
        "bodies": len(bodies),
        "bodies_with_tools": len(body_sets),
        "tool_counts": tool_counts,
        "names_sha": names_sha,
        "body_names_sha": body_shas,
        "intra_run_consistent": intra_consistent,
        "elapsed_s": round(elapsed, 2),
        "port": sink.port,
    }
    return record, body_sets


def cleanup_work_root(work_root: Optional[str]) -> None:
    if not work_root:
        return
    shutil.rmtree(work_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def build_report(
    cli: str,
    cli_path: Optional[str],
    cli_version: Optional[str],
    model: str,
    base_url: str,
    runs_requested: int,
    runs: Sequence[Dict[str, Any]],
    body_sets: Sequence[Dict[str, Any]],
    generated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """汇总成对外报告（键序固定，便于人工读和 diff）。"""
    sets = group_sets(body_sets)
    verdict, reason = compute_verdict(runs, runs_requested)
    return {
        "tool": "shape_stability",
        "generated_at": generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cli": cli,
        "cli_path": cli_path,
        "cli_version": cli_version,
        "model": model,
        "base_url": base_url,
        "runs_requested": runs_requested,
        "runs_ok": sum(1 for run in runs if run.get("exit") == 0 and not run.get("timed_out")),
        "runs_with_data": sum(1 for run in runs if (run.get("bodies_with_tools") or 0) > 0),
        "verdict": verdict,
        "verdict_reason": reason,
        "distinct_sets": len(sets),
        # 计数口径：一次 run 里每份"带 tools 的 body"各计一次，sum(count) == bodies_with_tools
        "count_basis": "bodies_with_tools",
        "bodies_total": sum(int(run.get("bodies") or 0) for run in runs),
        "bodies_with_tools": sum(int(run.get("bodies_with_tools") or 0) for run in runs),
        "intra_run_mismatch": any(not run.get("intra_run_consistent", True) for run in runs),
        "sets": sets,
        "diff_between_sets": diff_between_sets(sets),
        "runs": [
            {key: run.get(key) for key in RUN_KEYS}
            for run in runs
        ],
    }


def verdict_exit_code(verdict: str) -> int:
    if verdict == VERDICT_STABLE:
        return EXIT_STABLE
    if verdict == VERDICT_UNSTABLE:
        return EXIT_UNSTABLE
    return EXIT_ENV


def format_run_line(run: Dict[str, Any]) -> str:
    return (
        "run {run}: exit={exit} timeout={timed_out} bodies={bodies} "
        "tools_bodies={bodies_with_tools} counts={counts} sha={sha} elapsed={elapsed}s port={port}".format(
            run=run["run"],
            exit="timeout" if run["timed_out"] else run["exit"],
            timed_out="yes" if run["timed_out"] else "no",
            bodies=run["bodies"],
            bodies_with_tools=run["bodies_with_tools"],
            counts=run["tool_counts"],
            sha=(run["names_sha"] or "none")[:12],
            elapsed=run["elapsed_s"],
            port=run["port"],
        )
    )


def format_report(report: Dict[str, Any], out_path: Optional[str]) -> str:
    lines: List[str] = [format_run_line(run) for run in report["runs"]]
    lines.append(
        "verdict={verdict} distinct_sets={distinct} bodies={bodies} tools_bodies={tools_bodies} "
        "intra_run_mismatch={mismatch} reason={reason}".format(
            verdict=report["verdict"],
            distinct=report["distinct_sets"],
            bodies=report["bodies_total"],
            tools_bodies=report["bodies_with_tools"],
            mismatch="yes" if report["intra_run_mismatch"] else "no",
            reason=report["verdict_reason"],
        )
    )
    for item in report["sets"]:
        lines.append(
            "set {sha} count={count} tools={tools} runs={runs}".format(
                sha=item["sha256"][:12],
                count=item["count"],
                tools=item["tools"],
                runs=item["runs"],
            )
        )
    for diff in report["diff_between_sets"]:
        lines.append(
            "diff only_in={only} (missing_from={other}): {names}".format(
                only=diff["only_in"][:12],
                other=diff["missing_from"][:12],
                names=", ".join(diff["names"]),
            )
        )
    if out_path:
        lines.append("report -> {0}".format(out_path))
    return "\n".join(lines)


def write_report(path: str, report: Dict[str, Any], prompt: str) -> None:
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    assert_no_prompt_text(payload, prompt)
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="probe: capture the same prompt N times, compare the tool-name sets"
    )
    parser.add_argument("--runs", type=int, default=5, help="要跑几次（>=1）")
    parser.add_argument("--out", required=True, help="报告输出路径（JSON，只含元数据）")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="sink 的基准 URL")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="每次 run 用的同一句 prompt（绝不输出）")
    parser.add_argument("--cli", default=DEFAULT_CLI, help="CLI 可执行文件（名字或路径）")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="传给 CLI 的模型串")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="单 run 超时秒数")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不执行")
    parser.add_argument("--keep-dirs", action="store_true", help="保留每次 run 的临时工作目录（排查用）")
    args = parser.parse_args(argv)

    if args.runs < 1:
        print("ERROR --runs must be >= 1", file=sys.stderr)
        return EXIT_ENV

    base, base_reason = parse_base_url(args.base_url)
    if base is None:
        print("ERROR {0}".format(base_reason), file=sys.stderr)
        return EXIT_ENV

    prompt_chars = len(args.prompt or "")
    plan = build_plan(args.runs, base, args.cli, args.model, args.timeout, prompt_chars)

    if args.dry_run:
        print(format_plan(plan, args.runs, prompt_chars))
        resolved, reason = resolve_cli(args.cli)
        print(
            "[dry-run] cli resolved: {0}{1}".format(
                "yes" if resolved else "no", "" if resolved else " ({0})".format(reason)
            )
        )
        print("[dry-run] exit 0 (dry run never touches the network or the CLI)")
        return EXIT_STABLE

    resolved, reason = resolve_cli(args.cli)
    if resolved is None:
        print("ERROR {0}".format(reason), file=sys.stderr)
        _write_no_data(args, base_reason or reason)
        return EXIT_ENV

    cli_version = probe_cli_version(resolved, args.timeout)
    work_root: Optional[str] = None
    runs: List[Dict[str, Any]] = []
    body_sets: List[Dict[str, Any]] = []
    try:
        work_root = tempfile.mkdtemp(prefix="shape-stability-")
        for index in range(1, args.runs + 1):
            try:
                record, sets = run_once(
                    index,
                    args.runs,
                    resolved,
                    base,
                    args.prompt,
                    args.model,
                    args.timeout,
                    work_root,
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # 单 run 崩掉不许把整轮弄挂
                print(
                    "run {0}: probe error ({1})".format(index, exc.__class__.__name__),
                    file=sys.stderr,
                )
                record = {
                    "run": index,
                    "exit": None,
                    "timed_out": False,
                    "bodies": 0,
                    "bodies_with_tools": 0,
                    "tool_counts": [],
                    "names_sha": None,
                    "body_names_sha": [],
                    "intra_run_consistent": True,
                    "elapsed_s": 0.0,
                    "port": None,
                }
                sets = []
            runs.append(record)
            body_sets.extend(sets)
            print(format_run_line(record), flush=True)
    finally:
        if not args.keep_dirs:
            cleanup_work_root(work_root)
        elif work_root:
            print("kept work dir: {0}".format(work_root), file=sys.stderr)

    report = build_report(
        cli=args.cli,
        cli_path=resolved[0] if resolved else None,
        cli_version=cli_version,
        model=args.model,
        base_url=args.base_url,
        runs_requested=args.runs,
        runs=runs,
        body_sets=body_sets,
    )
    try:
        write_report(args.out, report, args.prompt)
    except ValueError as exc:
        print("ERROR {0}".format(exc), file=sys.stderr)
        return EXIT_ENV

    print()
    print(format_report(report, args.out))
    return verdict_exit_code(report["verdict"])


def _write_no_data(args: argparse.Namespace, reason: str) -> None:
    """环境错误也要留一份结构化报告：verdict=NO_DATA，runs 为空。"""
    report = build_report(
        cli=args.cli,
        cli_path=None,
        cli_version=None,
        model=args.model,
        base_url=args.base_url,
        runs_requested=args.runs,
        runs=[],
        body_sets=[],
    )
    report["verdict"] = VERDICT_NO_DATA
    report["verdict_reason"] = "environment: {0}".format(reason)
    try:
        write_report(args.out, report, args.prompt)
        print("report -> {0} (NO_DATA)".format(args.out), file=sys.stderr)
    except (OSError, ValueError) as exc:
        print("WARN could not write report ({0})".format(exc.__class__.__name__), file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
