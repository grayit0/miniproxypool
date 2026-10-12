"""ProxyPool Python 客户端 —— 单文件、零配置、开箱即用。

三种用法：

1) 一行拿到可用代理（会从环境变量 PROXYPOOL_MANAGER 自动找 manager）：
    from proxypool_client import session
    with session() as s:
        print(s.get("https://httpbin.org/ip").json())

2) 只要代理地址，自己拿去用：
    from proxypool_client import ProxyPool
    pool = ProxyPool()
    print(pool.proxy_url())                 # http://1.2.3.4:8888
    requests.get("https://example.com", proxies=pool.get_proxy().proxies)

3) 当命令行工具用（不需要写代码）：
    python proxypool_client.py pick          # 打印一个可用代理地址
    python proxypool_client.py env --shell bash   # 打印可直接 source 的环境变量
    python proxypool_client.py run -- curl https://httpbin.org/ip
    python proxypool_client.py check         # 走池子发一次请求，验证是否真的能出网

所有失败都会自动换节点并调 manager 的 report_bad 把坏节点摘掉。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

import requests

__version__ = "2.0.0"

ENV_MANAGER = "PROXYPOOL_MANAGER"
ENV_TOKEN = "PROXYPOOL_ADMIN_TOKEN"
ENV_REGION = "PROXYPOOL_REGION"
DEFAULT_MANAGER = "http://127.0.0.1:8000"

log = logging.getLogger("proxypool.client")


class ProxyPoolError(RuntimeError):
    """与 manager 通信失败，或池中没有可用代理。"""


def default_manager_url() -> str:
    """manager 地址：环境变量优先，其次本机 8000 端口。"""
    return (os.getenv(ENV_MANAGER) or "").strip() or DEFAULT_MANAGER


def _no_proxy_session(admin_token: Optional[str] = None) -> requests.Session:
    """管理面用的 session。

    默认无视宿主机的 HTTP_PROXY/HTTPS_PROXY —— 否则「取代理列表」这个动作本身
    就会被别的代理劫持，还会因为 keep-alive 连接复用产生莫名其妙的 404。
    """
    sess = requests.Session()
    sess.trust_env = False
    if admin_token:
        sess.headers["X-Admin-Token"] = admin_token
    return sess


# ---------------------------------------------------------------------- 数据模型
@dataclass
class Proxy:
    node_id: str
    host: str
    port: int
    scheme: str = "http"
    region: str = ""
    latency_ms: Optional[float] = None
    tags: List[str] = field(default_factory=list)
    last_seen: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def proxies(self) -> Dict[str, str]:
        """直接喂给 requests 的 proxies 参数。"""
        return {"http": self.url, "https": self.url}

    def __str__(self) -> str:  # pragma: no cover
        return self.url

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Proxy":
        return cls(
            node_id=data.get("node_id", ""),
            host=data.get("host", ""),
            port=int(data.get("port") or 0),
            scheme=data.get("scheme") or "http",
            region=data.get("region") or "",
            latency_ms=data.get("latency_ms"),
            tags=list(data.get("tags") or []),
            last_seen=data.get("last_seen"),
            raw=dict(data),
        )


# ---------------------------------------------------------------------- 客户端
class ProxyPool:
    """manager 的瘦客户端。线程安全（requests.Session 自带连接池）。"""

    def __init__(
        self,
        manager_url: Optional[str] = None,
        timeout: float = 5.0,
        admin_token: Optional[str] = None,
        trust_env: bool = False,
    ) -> None:
        self.manager_url = (manager_url or default_manager_url()).rstrip("/")
        self.timeout = timeout
        self._session = _no_proxy_session(admin_token or (os.getenv(ENV_TOKEN) or None))
        self._session.trust_env = trust_env

    # ------------------------------------------------------------ 上下文管理
    def __enter__(self) -> "ProxyPool":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self._session.close()

    def __repr__(self) -> str:  # pragma: no cover
        return f"ProxyPool({self.manager_url!r})"

    # ------------------------------------------------------------------ 内部
    def _call(self, method: str, path: str, **kwargs) -> Any:
        url = self.manager_url + path
        kwargs.setdefault("timeout", self.timeout)
        try:
            resp = self._session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise ProxyPoolError(
                f"连不上 manager（{self.manager_url}）：{exc}\n"
                f"提示：用 --manager 或环境变量 {ENV_MANAGER} 指定地址。"
            ) from exc
        if resp.status_code >= 400:
            raise ProxyPoolError(
                f"manager 返回 {resp.status_code}：{method} {resp.url} -> {resp.text[:200]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise ProxyPoolError(
                f"manager 返回了非 JSON 内容：{method} {resp.url} -> {resp.text[:200]}"
            ) from exc

    # ------------------------------------------------------------------ 只读
    def health(self) -> Dict[str, Any]:
        return self._call("GET", "/health")

    def stats(self) -> Dict[str, Any]:
        return self._call("GET", "/api/stats")

    def list_nodes(self) -> List[Dict[str, Any]]:
        return self._call("GET", "/api/nodes").get("nodes", [])

    def list_proxies(
        self,
        count: int = 20,
        region: Optional[str] = None,
        scheme: Optional[str] = None,
    ) -> List[Proxy]:
        params: Dict[str, Any] = {"count": count}
        if region:
            params["region"] = region
        if scheme:
            params["scheme"] = scheme
        data = self._call("GET", "/api/proxies", params=params)
        return [Proxy.from_dict(item) for item in data.get("proxies", [])]

    def get_proxy(
        self,
        region: Optional[str] = None,
        exclude: Iterable[str] = (),
        prefer: str = "random",
    ) -> Proxy:
        """取一个可用代理。

        prefer: "random"（默认）/ "fastest"（探测延迟最小）
        exclude: 本次要排除的 node_id（通常是已经失败过的）
        """
        blocked: Set[str] = set(exclude)
        candidates = [
            p for p in self.list_proxies(count=200, region=region) if p.node_id not in blocked
        ]
        if not candidates:
            raise ProxyPoolError("池中没有可用代理（节点都掉线了？）")
        if prefer == "fastest":
            return min(candidates, key=lambda p: (p.latency_ms is None, p.latency_ms or 0.0))
        return random.choice(candidates)

    def proxy_url(self, region: Optional[str] = None) -> str:
        return self.get_proxy(region=region).url

    # ------------------------------------------------------------------ 上报
    def report_bad(self, node_id: str, reason: str = "") -> Dict[str, Any]:
        try:
            return self._call(
                "POST", f"/api/nodes/{node_id}/report_bad", json={"reason": reason[:500]}
            )
        except ProxyPoolError as exc:
            log.debug("上报坏节点失败（忽略）：%s", exc)
            return {"ok": False}

    # ------------------------------------------------------------ 上游池管理
    def add_upstreams(self, urls: Sequence[str], source: str = "client") -> Dict[str, Any]:
        return self._call("POST", "/api/upstreams", json={"urls": list(urls), "source": source})

    def list_upstreams(self) -> List[Dict[str, Any]]:
        return self._call("GET", "/api/upstreams").get("upstreams", [])

    # ------------------------------------------------------------ 开箱即用入口
    def session(self, **kwargs) -> "RotatingSession":
        """返回一个配好代理轮换的 requests 会话。"""
        kwargs.setdefault("region", (os.getenv(ENV_REGION) or None))
        return RotatingSession(self, **kwargs)


# 向后兼容：老代码里叫 ProxyPoolClient
ProxyPoolClient = ProxyPool


class RotatingSession:
    """带自动轮换 + 失败重试的 requests 会话。

    每失败一次就换一个代理，并把坏节点上报给 manager；重试次数用尽才抛异常。
    """

    def __init__(
        self,
        pool: ProxyPool,
        retries: int = 5,
        prefer: str = "random",
        report: bool = True,
        region: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 15.0,
        trust_env: bool = False,
        retry_status: Iterable[int] = (),
    ) -> None:
        self.pool = pool
        self.retries = max(1, retries)
        self.prefer = prefer
        self.report = report
        self.region = region
        self.timeout = timeout
        self.retry_status = set(retry_status)
        self.session = requests.Session()
        # 只走池里的代理，别被环境变量里的代理再套一层
        self.session.trust_env = trust_env
        if headers:
            self.session.headers.update(headers)
        self.last_proxy: Optional[Proxy] = None

    def __enter__(self) -> "RotatingSession":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self.session.close()

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        tried: Set[str] = set()
        last_error: Optional[Any] = None

        for attempt in range(1, self.retries + 1):
            try:
                proxy = self.pool.get_proxy(region=self.region, exclude=tried, prefer=self.prefer)
            except ProxyPoolError as exc:
                raise ProxyPoolError(
                    f"取代理失败（第 {attempt} 次）：{exc}；上次错误：{last_error}"
                ) from exc

            tried.add(proxy.node_id)
            self.last_proxy = proxy
            try:
                resp = self.session.request(method, url, proxies=proxy.proxies, **kwargs)
            except requests.RequestException as exc:
                last_error = exc
                log.warning(
                    "第 %d/%d 次请求经代理 %s 失败：%s", attempt, self.retries, proxy.url, exc
                )
                if self.report:
                    self.pool.report_bad(proxy.node_id, f"{type(exc).__name__}: {exc}")
                continue

            if resp.status_code in self.retry_status and attempt < self.retries:
                last_error = f"HTTP {resp.status_code}"
                log.warning(
                    "第 %d/%d 次请求经代理 %s 返回 %d，换节点重试",
                    attempt, self.retries, proxy.url, resp.status_code,
                )
                if self.report:
                    self.pool.report_bad(proxy.node_id, f"HTTP {resp.status_code}")
                continue
            return resp

        raise ProxyPoolError(f"{self.retries} 次重试全部失败，最后一次错误：{last_error}")

    def get(self, url: str, **kwargs) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self.request("POST", url, **kwargs)


def session(manager_url: Optional[str] = None, **kwargs) -> RotatingSession:
    """开箱即用入口：`with session() as s: s.get(...)`。

    manager 地址优先取参数，其次读环境变量 PROXYPOOL_MANAGER。
    """
    pool_kwargs = {}
    for key in ("timeout", "admin_token", "trust_env"):
        if key in kwargs:
            pool_kwargs[key] = kwargs.pop(key)
    return ProxyPool(manager_url, **pool_kwargs).session(**kwargs)


# ---------------------------------------------------------------------- 命令行
def _print_proxies(proxies: Sequence[Proxy], as_json: bool) -> None:
    if as_json:
        print(json.dumps([p.raw for p in proxies], ensure_ascii=False, indent=2))
        return
    if not proxies:
        print("池中没有可用代理。")
        return
    print(f"{'node_id':<16}{'地址':<28}{'region':<12}{'延迟(ms)':<10}")
    print("-" * 66)
    for p in proxies:
        lat = "-" if p.latency_ms is None else f"{p.latency_ms:.0f}"
        print(f"{p.node_id:<16}{p.url:<28}{p.region or '-':<12}{lat:<10}")


def _cmd_list(args: argparse.Namespace) -> int:
    with ProxyPool(args.manager, timeout=args.timeout) as pool:
        _print_proxies(
            pool.list_proxies(count=args.count, region=args.region, scheme=args.scheme), args.json
        )
    return 0


def _cmd_pick(args: argparse.Namespace) -> int:
    with ProxyPool(args.manager, timeout=args.timeout) as pool:
        proxy = pool.get_proxy(region=args.region, prefer=args.prefer)
    print(proxy.url)
    return 0


def _cmd_env(args: argparse.Namespace) -> int:
    with ProxyPool(args.manager, timeout=args.timeout) as pool:
        proxy = pool.get_proxy(region=args.region, prefer=args.prefer)
    url = proxy.url
    if args.shell == "powershell":
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            print(f'$env:{key}="{url}"')
        print(f'$env:NO_PROXY="{args.no_proxy}"')
    elif args.shell == "cmd":
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            print(f"set {key}={url}")
        print(f"set NO_PROXY={args.no_proxy}")
    else:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            print(f"export {key}={url}")
        print(f"export NO_PROXY={args.no_proxy}")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    with ProxyPool(args.manager, timeout=args.timeout) as pool:
        session_obj = pool.session(region=args.region, retries=args.retries, prefer=args.prefer)
        with session_obj as sess:
            started = time.perf_counter()
            resp = sess.get(args.url)
            elapsed = (time.perf_counter() - started) * 1000
    used = session_obj.last_proxy
    print(f"状态码 : {resp.status_code}")
    print(f"经代理 : {used.url if used else '-'}（node_id={used.node_id if used else '-'}）")
    print(f"耗时   : {elapsed:.0f} ms")
    body = resp.text.strip()
    if args.show_body:
        print("响应体 :")
        print(body[:2000])
    elif "ip" in body[:200]:
        try:
            print(f"出口 IP: {resp.json().get('origin') or resp.json().get('ip')}")
        except ValueError:
            pass
    return 0 if resp.status_code < 400 else 1


def _cmd_stats(args: argparse.Namespace) -> int:
    with ProxyPool(args.manager, timeout=args.timeout) as pool:
        data = pool.stats()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def _cmd_bench(args: argparse.Namespace) -> int:
    with ProxyPool(args.manager, timeout=args.timeout) as pool:
        proxies = pool.list_proxies(count=200, region=args.region)
        if not proxies:
            print("池中没有可用代理。")
            return 1
        rows = []
        for proxy in proxies:
            costs = []
            ok = True
            for _ in range(max(1, args.n)):
                try:
                    started = time.perf_counter()
                    r = requests.get(
                        args.url,
                        proxies=proxy.proxies,
                        timeout=args.timeout,
                        headers={"Connection": "close"},
                    )
                    costs.append((time.perf_counter() - started) * 1000)
                    ok = ok and r.status_code < 400
                except requests.RequestException:
                    ok = False
                    break
            avg = sum(costs) / len(costs) if costs else float("inf")
            rows.append((avg, proxy, ok))
    rows.sort(key=lambda item: item[0])
    print(f"{'node_id':<16}{'地址':<28}{'平均(ms)':<12}{'结果':<6}")
    print("-" * 62)
    for avg, proxy, ok in rows:
        cost = "-" if avg == float("inf") else f"{avg:.0f}"
        print(f"{proxy.node_id:<16}{proxy.url:<28}{cost:<12}{'OK' if ok else 'FAIL':<6}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("用法：proxypool run -- <命令>", file=sys.stderr)
        return 2
    with ProxyPool(args.manager, timeout=args.timeout) as pool:
        proxy = pool.get_proxy(region=args.region, prefer=args.prefer)
    env = dict(os.environ)
    url = proxy.url
    env.update(
        {
            "HTTP_PROXY": url,
            "HTTPS_PROXY": url,
            "ALL_PROXY": url,
            "http_proxy": url,
            "https_proxy": url,
            "NO_PROXY": args.no_proxy,
            "no_proxy": args.no_proxy,
        }
    )
    print(f"[proxypool] 经 {url}（node_id={proxy.node_id}）执行：{' '.join(command)}", file=sys.stderr)
    try:
        return subprocess.call(command, env=env)
    except FileNotFoundError as exc:
        print(f"命令不存在：{exc}", file=sys.stderr)
        return 127


def _cmd_watch(args: argparse.Namespace) -> int:
    """简单看板：循环打印池子状态。"""
    try:
        while True:
            try:
                with ProxyPool(args.manager, timeout=args.timeout) as pool:
                    nodes = pool.stats().get("nodes", {})
                print(
                    f"\r[{time.strftime('%H:%M:%S')}] 存活 {nodes.get('alive', 0)}/{nodes.get('total', 0)}"
                    f" | 活跃连接 {nodes.get('active_conns', 0)} | 累计 {nodes.get('served', 0)}",
                    end="",
                    flush=True,
                )
            except ProxyPoolError as exc:
                print(f"\r[{time.strftime('%H:%M:%S')}] manager 不可用：{exc}", end="", flush=True)
            time.sleep(max(1.0, args.interval))
    except KeyboardInterrupt:
        print()
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="proxypool",
        description="ProxyPool 客户端 —— 从 manager 取代理并用起来",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  proxypool list --count 5\n"
            "  proxypool pick\n"
            "  eval $(proxypool env)\n"
            "  proxypool run -- curl -s https://httpbin.org/ip\n"
            "  proxypool check\n"
        ),
    )
    parser.add_argument(
        "--manager",
        default=None,
        help=f"manager 地址（默认读环境变量 {ENV_MANAGER}，再退回 {DEFAULT_MANAGER}）",
    )
    parser.add_argument("--timeout", type=float, default=5.0, help="请求 manager 的超时（秒）")
    parser.add_argument("--version", action="version", version=f"proxypool-client {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    def add_region(p: argparse.ArgumentParser) -> None:
        p.add_argument("--region", default=None, help="只选某个 region 的节点")

    p = sub.add_parser("list", help="列出可用代理")
    p.add_argument("--count", type=int, default=20, help="最多列出多少个")
    p.add_argument("--scheme", default=None, help="按协议过滤，如 http")
    p.add_argument("--json", action="store_true", help="输出原始 JSON")
    add_region(p)
    p.set_defaults(func=_cmd_list)

    p = sub.add_parser("pick", help="打印一个可用代理地址")
    p.add_argument("--prefer", choices=("random", "fastest"), default="random")
    add_region(p)
    p.set_defaults(func=_cmd_pick)

    p = sub.add_parser("env", help="打印可直接生效的代理环境变量")
    p.add_argument("--shell", choices=("bash", "powershell", "cmd"), default="bash")
    p.add_argument("--prefer", choices=("random", "fastest"), default="random")
    p.add_argument("--no-proxy", default="localhost,127.0.0.1", help="NO_PROXY 取值")
    add_region(p)
    p.set_defaults(func=_cmd_env)

    p = sub.add_parser("check", help="走池子发一次请求，验证是否真能出网")
    p.add_argument("--url", default="https://httpbin.org/ip")
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--prefer", choices=("random", "fastest"), default="random")
    p.add_argument("--show-body", action="store_true")
    add_region(p)
    p.set_defaults(func=_cmd_check)

    p = sub.add_parser("bench", help="逐个节点测延迟")
    p.add_argument("--url", default="https://httpbin.org/ip")
    p.add_argument("--n", type=int, default=2, help="每个节点测几次")
    add_region(p)
    p.set_defaults(func=_cmd_bench)

    p = sub.add_parser("stats", help="打印 manager 的池子状态")
    p.set_defaults(func=_cmd_stats)

    p = sub.add_parser("watch", help="循环刷新池子状态")
    p.add_argument("--interval", type=float, default=2.0)
    p.set_defaults(func=_cmd_watch)

    p = sub.add_parser("run", help="用池里的代理执行一条命令")
    p.add_argument("--prefer", choices=("random", "fastest"), default="random")
    p.add_argument("--no-proxy", default="localhost,127.0.0.1")
    p.add_argument("command", nargs=argparse.REMAINDER, help="命令，建议写成：run -- <命令>")
    add_region(p)
    p.set_defaults(func=_cmd_run)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [proxypool] %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ProxyPoolError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
