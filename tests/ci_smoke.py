"""CI 冒烟测试：对已经跑起来的 proxypool 做一次真实的外网连通验证。

用法：
    python tests/ci_smoke.py --manager http://127.0.0.1:8000 --min-nodes 2

检查项：
  1. manager /health 可用；
  2. 池子里至少有 min-nodes 个可用节点（会等一会儿，给节点注册留时间）；
  3. 通过每个节点的 HTTPS 请求（走 CONNECT 隧道）返回 200；
  4. 通过其中一个节点的明文 HTTP 请求能被正确改写并返回 2xx/3xx。

只用标准库 + requests。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

try:
    import requests
except ImportError:  # pragma: no cover
    print("需要 requests：pip install requests", file=sys.stderr)
    raise SystemExit(2)

HTTPS_TARGET = "https://example.com"
HTTP_TARGET = "http://example.com"

# 明确绕过环境变量里的代理，避免把冒烟测试本身代理掉
_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get_json(url: str, timeout: float = 10.0):
    with _direct.open(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def wait_for_manager(manager: str, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            body = _get_json(f"{manager}/health", timeout=5)
            if body.get("status") == "ok":
                return body
        except Exception as exc:  # 还在启动
            last = exc
        time.sleep(2)
    raise RuntimeError(f"manager 未就绪：{last}")


def wait_for_nodes(manager: str, min_nodes: int, timeout: float = 120.0) -> list:
    deadline = time.time() + timeout
    last = []
    while time.time() < deadline:
        try:
            last = _get_json(f"{manager}/api/proxies?count=50").get("proxies", [])
            if len(last) >= min_nodes:
                return last
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError(f"池中节点不足（要 {min_nodes} 个，实际 {len(last)} 个）：{last}")


def check_https(proxy: dict, session: requests.Session) -> None:
    url = proxy["url"]
    proxies = {"http": url, "https": url}
    resp = session.get(HTTPS_TARGET, proxies=proxies, timeout=30)
    print(f"  [HTTPS] {url} (node_id={proxy.get('node_id')}) -> {resp.status_code}")
    if resp.status_code != 200:
        raise AssertionError(f"{url} 的 HTTPS 请求返回 {resp.status_code}")


def check_plain_http(proxy: dict, session: requests.Session) -> None:
    url = proxy["url"]
    proxies = {"http": url, "https": url}
    resp = session.get(HTTP_TARGET, proxies=proxies, timeout=30, allow_redirects=False)
    print(f"  [HTTP ] {url} -> {resp.status_code}")
    if resp.status_code >= 400:
        raise AssertionError(f"{url} 的明文 HTTP 请求返回 {resp.status_code}")


def main() -> int:
    parser = argparse.ArgumentParser(description="proxypool 冒烟测试")
    parser.add_argument("--manager", default="http://127.0.0.1:8000")
    parser.add_argument("--min-nodes", type=int, default=2)
    parser.add_argument("--skip-https", action="store_true", help="跳过外网请求（只验证池子）")
    args = parser.parse_args()

    manager = args.manager.rstrip("/")
    print(f"manager = {manager}")

    print("[1] 等 manager 就绪……")
    health = wait_for_manager(manager)
    print(f"    ok: {health}")

    print(f"[2] 等池子里出现至少 {args.min_nodes} 个节点……")
    proxies = wait_for_nodes(manager, args.min_nodes)
    print(f"    拿到 {len(proxies)} 个：{[p['url'] for p in proxies]}")

    if args.skip_https:
        print("已跳过外网请求检查。")
        return 0

    session = requests.Session()
    session.trust_env = False
    print("[3] 逐个节点验证 HTTPS（CONNECT 隧道）……")
    for proxy in proxies:
        check_https(proxy, session)

    print("[4] 验证明文 HTTP（请求行改写）……")
    check_plain_http(proxies[0], session)

    print("\n冒烟测试全部通过。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, RuntimeError) as exc:
        print(f"\n冒烟测试失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
