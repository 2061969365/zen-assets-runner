#!/usr/bin/env python3
"""在 CI 里拉起一个可用的 VPN Gate 出口（sing-box mixed inbound）。

配置形状必须照抄 zen-vpngate-cloud-test 里已验证过的做法：

    {"log":..., "endpoints":[一个 openvpn-client],
     "outbounds":[{"type":"direct","tag":"direct"}],
     "inbounds":[{"type":"mixed","listen":"127.0.0.1","listen_port":18080}],
     "route":{"final":"<这个 endpoint 的 tag>"}}

为什么不能用转换器自带的 selector/urltest 输出：openvpn-client 是 endpoint 而不是
普通 outbound，urltest 拿不到它的可用状态，实测满屏 "endpoint is not ready yet"，
代理口永远不通（run 44072089302）。单个 endpoint + route.final 直连它才行。

节点来源仍用 vpngate_to_singbox.py --csv：它负责解析 .ovpn、挑 TCP 可用的节点，
这部分不用重写。

前置（workflow 备好，cwd = $W/vp）：
  ./nodes.csv  ./sing-box  ./vpngate_to_singbox.py

产出：./sb.json（最终配置） ./proxy.env ./sb.log
退出码：0 = 代理通了；1 = 都不通。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

WORK = os.getcwd()
CSV = os.path.join(WORK, "nodes.csv")
SINGBOX = os.path.join(WORK, "sing-box")
CONVERTER = os.path.join(WORK, "vpngate_to_singbox.py")
POOL = os.path.join(WORK, "pool.json")
CONFIG = os.path.join(WORK, "sb.json")
PROXY_ENV = os.path.join(WORK, "proxy.env")
SB_LOG = os.path.join(WORK, "sb.log")

MIXED_PORT = 18080
POOL_SIZE = int(os.environ.get("VPNGATE_POOL", "40"))
TRY_NODES = int(os.environ.get("VPNGATE_TRY", "12"))
PROBE_URL = "http://cp.cloudflare.com/generate_204"


def build_pool() -> list[dict]:
    """用转换器的 --csv 模式解析出一批 endpoint（它负责 TCP 可用性过滤）。"""
    proc = subprocess.run(
        [sys.executable, CONVERTER, "--csv", CSV, "--output", POOL,
         "--limit", str(POOL_SIZE), "--no-probe"],
        cwd=WORK, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"[convert] rc={proc.returncode}")
        print((proc.stderr or "")[-800:])
        return []
    print((proc.stdout or "").strip()[-200:])
    with open(POOL, encoding="utf-8") as fh:
        return json.load(fh).get("endpoints", [])


def node_config(endpoint: dict, tag: str) -> dict:
    ep = dict(endpoint)
    ep["tag"] = tag
    return {
        "log": {"level": "warn"},
        "endpoints": [ep],
        "outbounds": [{"type": "direct", "tag": "direct"}],
        "inbounds": [{"type": "mixed", "tag": "in",
                      "listen": "127.0.0.1", "listen_port": MIXED_PORT}],
        "route": {"final": tag},
    }


def wait_port(proc: subprocess.Popen, port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        with socket.socket() as sock:
            sock.settimeout(1)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


def probe_tunnel(timeout: float = 10.0) -> bool:
    """经代理打 generate_204；204/200 说明隧道真通了。"""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({
            "http": f"http://127.0.0.1:{MIXED_PORT}",
            "https": f"http://127.0.0.1:{MIXED_PORT}",
        })
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with opener.open(PROBE_URL, timeout=3) as resp:
                if resp.status in (200, 204):
                    return True
        except Exception:  # noqa: BLE001 - 隧道还没起来
            time.sleep(0.5)
    return False


def stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()


def main() -> int:
    for path in (CSV, SINGBOX, CONVERTER):
        if not os.path.exists(path):
            print(f"::error:: 缺少 {path}")
            return 1

    endpoints = build_pool()
    print(f"候选 endpoint {len(endpoints)} 个，试前 {min(len(endpoints), TRY_NODES)} 个")
    if not endpoints:
        print("::error:: 没解析出任何 endpoint")
        return 1

    for idx, ep in enumerate(endpoints[:TRY_NODES]):
        tag = f"vpn-{idx}"
        cfg = node_config(ep, tag)
        with open(CONFIG, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)

        server = ep.get("server", "?")
        port = ep.get("server_port", "?")
        print(f"--- [{idx}] {server}:{port}")
        log = open(SB_LOG, "w")
        proc = subprocess.Popen([SINGBOX, "run", "-c", CONFIG], cwd=WORK,
                                stdout=log, stderr=subprocess.STDOUT)
        if not wait_port(proc, MIXED_PORT, 8):
            print("    sing-box 没监听")
            stop(proc)
            continue
        if not probe_tunnel():
            print("    隧道不通")
            stop(proc)
            continue

        with open(PROXY_ENV, "w", encoding="utf-8") as fh:
            fh.write(f"http_proxy=http://127.0.0.1:{MIXED_PORT}\n")
            fh.write(f"https_proxy=http://127.0.0.1:{MIXED_PORT}\n")
            fh.write(f"ALL_PROXY=http://127.0.0.1:{MIXED_PORT}\n")
            fh.write("NO_PROXY=127.0.0.1,localhost\n")
            fh.write("no_proxy=127.0.0.1,localhost\n")
        print(f"[OK] {server}:{port} 隧道通了（sing-box pid={proc.pid} 留活）")
        return 0

    print("::error:: 试过的节点隧道都不通")
    return 1


if __name__ == "__main__":
    sys.exit(main())
