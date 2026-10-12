"""ProxyPool 客户端演示。

想要最短的用法，看这个就够了：

    from proxypool_client import session
    with session("http://127.0.0.1:8000") as s:      # 省略参数则读 PROXYPOOL_MANAGER
        print(s.get("https://example.com").status_code)

本文件演示得更全一些：健康检查、列代理、走代理发请求、前后对比统计。

用法：
    python demo.py                          # 默认 manager=http://127.0.0.1:8000
    python demo.py --manager http://127.0.0.1:8000
    python demo.py --url https://www.baidu.com      # 国内网络建议换成这个
"""
from __future__ import annotations

import argparse
import logging
import sys

from proxypool_client import ProxyPoolClient, ProxyPoolError, RotatingSession

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s [client] %(message)s")
log = logging.getLogger("demo")


def show_stats(pool: ProxyPoolClient, title: str) -> None:
    try:
        data = pool.stats()
    except ProxyPoolError as exc:
        print(f"[{title}] 取统计失败：{exc}")
        return
    nodes, ups = data["nodes"], data["upstreams"]
    print(
        f"[{title}] 节点 {nodes['alive']} 可用 / {nodes['total']} 总数"
        f"（死 {nodes['dead']}），活跃连接 {nodes['active_conns']}，已服务 {nodes['served']} 次"
        f"；上游 {ups['ok']}/{ups['total']} 可用"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ProxyPool 客户端演示")
    parser.add_argument("--manager", default="http://127.0.0.1:8000")
    parser.add_argument("--url", default="http://httpbin.org/ip", help="用来验证代理出口的目标地址")
    parser.add_argument("--count", type=int, default=10, help="列出多少个代理")
    parser.add_argument("--region", default=None)
    parser.add_argument("--retries", type=int, default=3)
    args = parser.parse_args(argv)

    pool = ProxyPoolClient(args.manager)
    try:
        print("manager 健康检查：", pool.health())
    except ProxyPoolError as exc:
        print(f"连不上 manager（{args.manager}）：{exc}")
        return 1

    show_stats(pool, "初始")

    proxies = pool.list_proxies(count=args.count, region=args.region)
    if not proxies:
        print("池里还没有可用代理；请先启动 node 容器（docker compose up -d）。")
        return 2
    print(f"\n可用代理（{len(proxies)} 个）：")
    for i, p in enumerate(proxies, 1):
        latency = f"{p.latency_ms:.1f}ms" if p.latency_ms is not None else "-"
        print(f"  {i:>2}. {p.url:<28} node_id={p.node_id:<14} region={p.region or '-':<8} 探测延迟={latency}")

    print(f"\n通过随机代理访问 {args.url} ……")
    try:
        with RotatingSession(pool, retries=args.retries, region=args.region) as sess:
            resp = sess.get(args.url)
        print(f"  状态码：{resp.status_code}")
        body = resp.text.strip()
        print(f"  响应：{body[:300]}")
    except ProxyPoolError as exc:
        print(f"  失败：{exc}")
        show_stats(pool, "结束后")
        return 3

    show_stats(pool, "结束后")
    print("\n演示完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
