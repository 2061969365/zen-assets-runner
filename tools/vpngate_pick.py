#!/usr/bin/env python3
"""在 CI 里挑一个可用的 VPN Gate 节点并拉起 sing-box。

为什么单独一个文件：逻辑要读 base64 的 OpenVPN 配置、反复起停子进程、
经代理取 IP，塞进 YAML 的 heredoc 里缩进容易出错，也不好单独测。

前置（由 workflow 准备好，工作目录就是 $W/vp）：
  ./nodes.csv              VPN Gate 官方快照
  ./sing-box               sing-box 可执行
  ./vpngate_to_singbox.py  把 .ovpn 转成 sing-box 配置

产出：
  ./sb.json                选中的 sing-box 配置
  ./proxy.env              形如 http_proxy=http://127.0.0.1:18080，给 $GITHUB_ENV 用
  ./sb.log                 sing-box 运行日志

退出码：0 = 挑到可用节点；1 = 一个都不通。
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.getcwd()

CSV = os.path.join(WORK, "nodes.csv")
SINGBOX = os.path.join(WORK, "sing-box")
CONVERTER = os.path.join(WORK, "vpngate_to_singbox.py")
CONFIG = os.path.join(WORK, "sb.json")
PROXY_ENV = os.path.join(WORK, "proxy.env")
SB_LOG = os.path.join(WORK, "sb.log")

MIXED_PORT = 18080
MAX_NODES = 20
PROBE_URL = "https://api.ipify.org"
STARTUP_WAIT = 4.0


def parse_nodes(path: str) -> list[tuple[int, str, str]]:
    """VPN Gate 快照：首行 *vpn_servers，其余 # 开头是注释/表头，数据行逗号分隔。

    返回 [(score, hostname, base64_ovpn)]，按 score 降序。
    """
    rows: list[tuple[int, str, str]] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("*") or line.startswith("#"):
                continue
            parts = line.split(",")
            # 第 15 列（index 14）是 base64 OpenVPN 配置；列表头有 15 列
            if len(parts) < 15:
                continue
            host = parts[0]
            # public-vpn-* 是 VPN Gate 的公共中继，握手经常被 reset（实测过一整轮全 fails）；
            # 直接跳过，优先用真实 ISP 节点。
            if host.startswith("public-vpn-"):
                continue
            try:
                score = int(parts[2] or 0)
            except ValueError:
                score = 0
            rows.append((score, host, parts[14]))
    rows.sort(key=lambda r: r[0], reverse=True)
    return rows


def write_ovpn(b64: str) -> bool:
    try:
        raw = base64.b64decode(b64, validate=False)
    except Exception:
        return False
    if b"remote " not in raw:
        return False
    with open(os.path.join(WORK, "node.ovpn"), "wb") as fh:
        fh.write(raw)
    return True


def convert() -> bool:
    # --mixed 收的是 HOST:PORT，不是裸端口（传 "18080" 会生成没有 inbound 的配置，
    # sing-box 起来但不监听，探测表现为 connection refused）。
    proc = subprocess.run(
        [sys.executable, CONVERTER, "--input", "node.ovpn", "--output", CONFIG,
         "--mixed", f"127.0.0.1:{MIXED_PORT}"],
        cwd=WORK, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"  convert failed rc={proc.returncode}: {(proc.stderr or '')[-300:]}")
        return False
    return os.path.exists(CONFIG) and os.path.getsize(CONFIG) > 0


def wait_port(port: int, timeout: float = 12.0) -> bool:
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(1)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.5)
    return False


def start_singbox() -> subprocess.Popen:
    log = open(SB_LOG, "w")
    return subprocess.Popen([SINGBOX, "run", "-c", CONFIG], cwd=WORK,
                            stdout=log, stderr=subprocess.STDOUT)


def probe() -> str | None:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({
            "http": f"http://127.0.0.1:{MIXED_PORT}",
            "https": f"http://127.0.0.1:{MIXED_PORT}",
        })
    )
    try:
        with opener.open(PROBE_URL, timeout=25) as resp:
            return resp.read().decode("utf-8", "replace").strip()
    except Exception as exc:  # noqa: BLE001 - 任何异常都只意味着这个节点不行
        print(f"  probe failed: {exc}")
        return None


def main() -> int:
    for path in (CSV, SINGBOX, CONVERTER):
        if not os.path.exists(path):
            print(f"::error:: 缺少 {path}")
            return 1

    rows = parse_nodes(CSV)
    print(f"候选节点 {len(rows)} 个，试前 {min(len(rows), MAX_NODES)} 个")
    if not rows:
        print("::error:: nodes.csv 里没有可用数据行")
        return 1

    for score, host, b64 in rows[:MAX_NODES]:
        print(f"--- {host} score={score}")
        if not write_ovpn(b64):
            continue
        if not convert():
            continue
        proc = start_singbox()
        if not wait_port(MIXED_PORT):
            print("  sing-box 没监听，日志尾部：")
            try:
                with open(SB_LOG, encoding="utf-8", errors="replace") as fh:
                    print("\n".join(fh.read().splitlines()[-6:]))
            except OSError:
                pass
            proc.terminate()
            continue
        ip = probe()
        if ip:
            with open(PROXY_ENV, "w", encoding="utf-8") as fh:
                fh.write(f"http_proxy=http://127.0.0.1:{MIXED_PORT}\n")
                fh.write(f"https_proxy=http://127.0.0.1:{MIXED_PORT}\n")
                fh.write(f"ALL_PROXY=http://127.0.0.1:{MIXED_PORT}\n")
                fh.write("NO_PROXY=127.0.0.1,localhost\n")
                fh.write("no_proxy=127.0.0.1,localhost\n")
            print(f"[OK] {host} -> 出口 IP {ip}（sing-box pid={proc.pid} 留活）")
            return 0
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print("::error:: 试过的节点全部不通")
    return 1


if __name__ == "__main__":
    sys.exit(main())
