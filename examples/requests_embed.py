# -*- coding: utf-8 -*-
"""把 ProxyPool 接进 requests.post —— 直接对应 GetProjectFinanceListForC 这种调用。

这个文件只依赖 `requests`，可以整个复制进你自己的项目；
装了 proxypool 客户端也能共存（本文件不 import 它）。

────────────────────────────────────────────────────────────────────
三种用法，选一种：

【用法 1】只有一个节点，地址固定 —— 最省事
    在原来的 requests.post 里加一行 proxies 就行：

        PROXY = "http://<服务器公网IP>:18801"
        response = requests.post(
            'https://ssl.gongyi.qq.com/gygw-app/ed/finance/GetProjectFinanceListForC',
            params=params,
            cookies=cookies,
            headers=headers,
            json=json_data,
            timeout=4,
            proxies={"http": PROXY, "https": PROXY},   # ← 就加这一行
        )

【用法 2】从 manager 取，自动轮换 + 失败换节点 —— 推荐
    把原来的调用换成本文件的 post_finance_list(...)，参数逐个对应：

        from requests_embed import post_finance_list

        response = post_finance_list(
            params=params,
            cookies=cookies,
            headers=headers,
            json_data=json_data,
            timeout=4,
            proxy="http://<服务器公网IP>:8000",   # manager 地址
            sticky=True,                          # 见下方「粘性」说明
        )

【用法 3】只想要一个代理地址，自己塞进现有代码
        from requests_embed import pick_proxy
        PROXY = pick_proxy("http://<服务器公网IP>:8000")   # -> http://1.2.3.4:18801

────────────────────────────────────────────────────────────────────
⚠️ 三个必踩的坑（都已在下面的实现里处理）：

1. **`proxies` 必须同时给 `http` 和 `https` 两个键。**
   目标站是 https，requests 只会去取 `https` 键；只写 `http` 键的话
   **请求会静默直连、不报任何错**（实测：http 键指向死端口，请求照样 200）。

2. **`trust_env` 要关掉。** 机器上若有 HTTP_PROXY/HTTPS_PROXY 环境变量，
   requests 会把它们和你的 proxies 合并，叠加成两层代理，症状是偶发 404。

3. **带 cookie / 登录态的接口不要每请求换 IP。** 出口 IP 中途变化容易触发
   目标站的风控。这类接口用 `sticky=True`（固定一个节点，失败才换）；
   只有在接口无状态、纯为分散压力时才用 `sticky=False` 做每请求轮换。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

__all__ = [
    "API_URL",
    "proxy_param",
    "pick_proxy",
    "list_proxies",
    "post_finance_list",
    "new_session",
]

API_URL = "https://ssl.gongyi.qq.com/gygw-app/ed/finance/GetProjectFinanceListForC"

#: 环境变量兜底，避免把地址硬编码进代码
ENV_MANAGER = "PROXYPOOL_MANAGER"
ENV_STATIC = "PROXYPOOL_PROXY"

#: 只有连接类错误和这些状态码才值得换节点重试；4xx 是业务错误，重试没意义
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


# ─────────────────────────────────────────────────────────── 基础工具
def proxy_param(url: str) -> Dict[str, str]:
    """把代理地址变成 requests 的 proxies 参数。

    两个键都要给 —— 缺 https 键时 requests 对 https 目标会静默直连。
    """
    if not url:
        raise ValueError("代理地址为空")
    if "://" not in url:
        url = "http://" + url
    return {"http": url, "https": url}


def new_session(trust_env: bool = False) -> requests.Session:
    """独立 session；默认不吃宿主机的 HTTP_PROXY 环境变量。"""
    sess = requests.Session()
    sess.trust_env = trust_env
    return sess


def list_proxies(
    manager: str,
    count: int = 200,
    region: Optional[str] = None,
    timeout: float = 5.0,
) -> List[Tuple[str, str, Optional[float]]]:
    """问 manager 要代理列表，返回 [(node_id, url, latency_ms), ...]，延迟小的在前。

    只用 requests 直接打管理接口，不依赖 proxypool 包。
    """
    manager = (manager or "").rstrip("/")
    if not manager:
        raise ValueError("manager 地址为空")
    params: Dict[str, Any] = {"count": count}
    if region:
        params["region"] = region
    sess = new_session()
    try:
        resp = sess.get(manager + "/api/proxies", params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    finally:
        sess.close()

    out: List[Tuple[str, str, Optional[float]]] = []
    for item in data.get("proxies") or []:
        url = item.get("url") or "http://{0}:{1}".format(item.get("host"), item.get("port"))
        out.append((item.get("node_id") or "", url, item.get("latency_ms")))
    out.sort(key=lambda row: (row[2] is None, row[2] or 0.0))
    return out


def pick_proxy(
    manager: Optional[str] = None,
    region: Optional[str] = None,
    prefer: str = "fastest",
) -> str:
    """取一个代理地址字符串。manager 为空时读环境变量 PROXYPOOL_MANAGER。"""
    manager = manager or os.getenv(ENV_MANAGER) or ""
    rows = list_proxies(manager, region=region) if manager else []
    if not rows:
        static = (os.getenv(ENV_STATIC) or "").strip()
        if static:
            return static
        raise RuntimeError(
            "没有可用代理：请传 manager 地址，或设环境变量 {0} / {1}".format(ENV_MANAGER, ENV_STATIC)
        )
    if prefer == "random":
        return random.choice(rows)[1]
    return rows[0][1]


def _report_bad(manager: str, node_id: str, reason: str) -> None:
    """累计失败到阈值时 manager 会把这个节点摘掉。失败不影响主流程。"""
    if not (manager and node_id):
        return
    sess = new_session()
    try:
        sess.post(
            manager.rstrip("/") + "/api/nodes/{0}/report_bad".format(node_id),
            json={"reason": reason[:500]},
            timeout=3.0,
        )
    except requests.RequestException:
        pass
    finally:
        sess.close()


# ─────────────────────────────────────────────────────────── 主入口
def post_finance_list(
    params: Optional[Dict[str, Any]] = None,
    cookies: Optional[Dict[str, str]] = None,
    headers: Optional[Dict[str, str]] = None,
    json_data: Optional[Dict[str, Any]] = None,
    timeout: float = 4,
    proxy: Optional[str] = None,
    manager: Optional[str] = None,
    region: Optional[str] = None,
    retries: int = 3,
    sticky: bool = True,
    url: str = API_URL,
    verbose: bool = True,
) -> requests.Response:
    """等价于原来的 requests.post(...)，但经代理池发出，失败自动换节点。

    proxy    : 单个固定代理地址（如 http://1.2.3.4:18801）；给了它就不问 manager
    manager  : manager 地址（如 http://1.2.3.4:8000），从池子里取
    sticky   : True = 整个会话固定用一个节点（带 cookie/登录态时用这个）
               False = 每次请求随机换（接口无状态时用）
    retries  : 换节点的次数上限
    """
    # 1) 决定候选代理
    fixed = (proxy or "").strip()
    mgr = (manager or os.getenv(ENV_MANAGER) or "").strip()

    if fixed:
        candidates: List[Tuple[str, str]] = [("", fixed)]
    elif mgr:
        rows = list_proxies(mgr, region=region)
        if not rows:
            raise RuntimeError("manager 里没有可用节点：{0}".format(mgr))
        candidates = [(node_id, u) for node_id, u, _ in rows]
        if sticky:
            candidates = candidates[:1]
    else:
        static = (os.getenv(ENV_STATIC) or "").strip()
        if not static:
            raise RuntimeError(
                "没给代理：请传 proxy= / manager=，或设 {0} / {1}".format(ENV_MANAGER, ENV_STATIC)
            )
        candidates = [("", static)]

    # 2) 逐个尝试
    tried: List[str] = []
    last_error: Optional[BaseException] = None

    for attempt in range(1, max(1, retries) + 1):
        available = [c for c in candidates if c[1] not in tried] or list(candidates)
        node_id, proxy_url = random.choice(available)
        tried.append(proxy_url)

        sess = new_session()
        started = time.perf_counter()
        try:
            resp = sess.post(
                url,
                params=params or {},
                cookies=cookies or {},
                headers=headers or {},
                json=json_data if json_data is not None else {},
                timeout=timeout,
                proxies=proxy_param(proxy_url),
            )
            cost = (time.perf_counter() - started) * 1000
            # 进度打 stdout 而不是 stderr：PowerShell 5.1 会把原生命令的 stderr
            # 渲染成一整块 NativeCommandError「错误」，看着像脚本炸了。
            if verbose:
                print(
                    "[proxypool] 第 {0}/{1} 次 | {2} | HTTP {3} | {4:.0f} ms".format(
                        attempt, retries, proxy_url, resp.status_code, cost
                    ),
                )

            # 只对「服务端/限流」类状态码换节点重试；4xx 是业务错误，直接返回
            if resp.status_code in RETRY_STATUS and attempt < retries:
                last_error = RuntimeError("HTTP {0}".format(resp.status_code))
                if mgr:
                    _report_bad(mgr, node_id, "HTTP {0}".format(resp.status_code))
                continue
            return resp

        except requests.RequestException as exc:
            cost = (time.perf_counter() - started) * 1000
            last_error = exc
            if verbose:
                print(
                    "[proxypool] 第 {0}/{1} 次 | {2} | 失败 {3} | {4:.0f} ms | {5}".format(
                        attempt, retries, proxy_url, type(exc).__name__, cost, str(exc)[:80]
                    ),
                )
            if mgr:
                _report_bad(mgr, node_id, "{0}: {1}".format(type(exc).__name__, exc))
            continue
        finally:
            sess.close()

    raise RuntimeError("{0} 次尝试全部失败，最后一次：{1}".format(retries, last_error))


# ─────────────────────────────────────────────────────────── 自检
def _main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="代理池接入自检")
    ap.add_argument("--proxy", default=None, help="固定代理地址，如 http://1.2.3.4:18801")
    ap.add_argument("--manager", default=None, help="manager 地址，如 http://1.2.3.4:8000")
    ap.add_argument("--region", default=None)
    ap.add_argument("--timeout", type=float, default=8.0, help="自检用的超时（线上默认 4）")
    ap.add_argument("--sticky", action="store_true", default=True)
    ap.add_argument("--rotate", dest="sticky", action="store_false", help="每次请求换节点")
    args = ap.parse_args(argv)

    mgr = args.manager or os.getenv(ENV_MANAGER)
    if mgr:
        print("manager = {0}".format(mgr))
        rows = list_proxies(mgr, region=args.region)
        print("池中节点 {0} 个：".format(len(rows)))
        for node_id, u, lat in rows:
            print("   {0:<10} {1:<28} latency={2}".format(node_id, u, lat))

    print("\n--- 真实请求 {0} ---".format(API_URL))
    try:
        resp = post_finance_list(
            params={"pageSize": 1, "pageIndex": 0},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://ssl.gongyi.qq.com/"},
            json_data={"pageSize": 1, "pageIndex": 0},
            timeout=args.timeout,
            proxy=args.proxy,
            manager=mgr,
            region=args.region,
            sticky=args.sticky,
        )
    except Exception as exc:
        print("失败：{0}: {1}".format(type(exc).__name__, exc))
        return 1
    print("HTTP {0} | 响应前 200 字：".format(resp.status_code))
    print(resp.text[:200])
    return 0 if resp.status_code < 400 else 1


if __name__ == "__main__":
    raise SystemExit(_main())
