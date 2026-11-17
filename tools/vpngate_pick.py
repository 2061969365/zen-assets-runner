#!/usr/bin/env python3
"""在 CI 里拉起一个可用的 VPN Gate 出口（sing-box mixed inbound）。

为什么是「一次打包多个节点」而不是「一个一个试」：
  单个节点试出来的路径太脆——VPN Gate 的免费节点随时 reset，逐试的失败率高到
  跑十几次都挑不出一个。vpngate_to_singbox.py 的 --csv 模式能把一批节点写进
  同一份配置，outbounds 里自带 urltest（tag=auto），由 sing-box 自己在运行时
  选路，所以只要这批里有一个活的，代理端口就是通的。

前置（workflow 已备好，工作目录就是 $W/vp）：
  ./nodes.csv              VPN Gate 官方快照
  ./sing-box               sing-box 可执行
  ./vpngate_to_singbox.py  --csv 模式的转换器

产出：
  ./sb.json     含 N 个 endpoints + mixed inbound(127.0.0.1:18080)
  ./proxy.env   给 $GITHUB_ENV 用的代理变量
  ./sb.log      sing-box 日志

退出码：0 = 代理通了；1 = 这批节点都不通。
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
CONFIG = os.path.join(WORK, "sb.json")
PROXY_ENV = os.path.join(WORK, "proxy.env")
SB_LOG = os.path.join(WORK, "sb.log")

MIXED_PORT = 18080
POOL_SIZE = int(os.environ.get("VPNGATE_POOL", "25"))
PROBE_URL = "https://api.ipify.org"


def build_pool() -> int:
    """把一批节点写成一份带 mixed inbound 的配置，返回 endpoints 数量。"""
    proc = subprocess.run(
        [sys.executable, CONVERTER,
         "--csv", CSV,
         "--output", CONFIG,
         "--limit", str(POOL_SIZE),
         "--no-probe",                       # 不做拨号排序，交给 sing-box 运行时 urltest
         "--mixed", f"127.0.0.1:{MIXED_PORT}"],
        cwd=WORK, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"[convert] rc={proc.returncode}")
        print((proc.stderr or "")[-800:])
        return 0
    print((proc.stdout or "").strip()[-300:])
    with open(CONFIG, encoding="utf-8") as fh:
        cfg = json.load(fh)
    return len(cfg.get("endpoints", []))


def wait_port(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(1)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.5)
    return False


def probe() -> str | None:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({
            "http": f"http://127.0.0.1:{MIXED_PORT}",
            "https": f"http://127.0.0.1:{MIXED_PORT}",
        })
    )
    try:
        with opener.open(PROBE_URL, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace").strip()
    except Exception as exc:  # noqa: BLE001
        print(f"  probe: {exc}")
        return None


def main() -> int:
    for path in (CSV, SINGBOX, CONVERTER):
        if not os.path.exists(path):
            print(f"::error:: 缺少 {path}")
            return 1

    count = build_pool()
    print(f"配置里 endpoints={count}")
    if count == 0:
        print("::error:: 没生成任何 endpoint")
        return 1

    log = open(SB_LOG, "w")
    proc = subprocess.Popen([SINGBOX, "run", "-c", CONFIG], cwd=WORK,
                            stdout=log, stderr=subprocess.STDOUT)
    if not wait_port(MIXED_PORT):
        print("::error:: sing-box 没监听")
        for line in open(SB_LOG, encoding="utf-8", errors="replace").read().splitlines()[-10:]:
            print("  " + line)
        proc.terminate()
        return 1

    # urltest 选路需要几秒，给它三轮机会
    for attempt in range(1, 6):
        ip = probe()
        if ip:
            with open(PROXY_ENV, "w", encoding="utf-8") as fh:
                fh.write(f"http_proxy=http://127.0.0.1:{MIXED_PORT}\n")
                fh.write(f"https_proxy=http://127.0.0.1:{MIXED_PORT}\n")
                fh.write(f"ALL_PROXY=http://127.0.0.1:{MIXED_PORT}\n")
                fh.write("NO_PROXY=127.0.0.1,localhost\n")
                fh.write("no_proxy=127.0.0.1,localhost\n")
            print(f"[OK] 第 {attempt} 次探测通过，出口 IP={ip}（sing-box pid={proc.pid} 留活）")
            return 0
        time.sleep(5)

    print("::error:: 这批节点都没通")
    for line in open(SB_LOG, encoding="utf-8", errors="replace").read().splitlines()[-15:]:
        print("  " + line)
    proc.terminate()
    return 1


if __name__ == "__main__":
    sys.exit(main())
