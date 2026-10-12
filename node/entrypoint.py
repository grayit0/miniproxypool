"""ProxyPool Node 入口。

启动顺序：
1. 跑起本地代理服务（对客户端）。
2. 向 manager 注册，带上「manager 用来探测的地址」和「客户端用来访问的地址」。
3. 起心跳任务，顺便从心跳响应里同步共享上游代理列表。
4. 起一个只读管理端口用于排查。

配置既支持命令行参数，也支持环境变量（Docker 里用环境变量）。
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import socket
import sys

from admin import AdminServer
from proxy import ProxyServer, host_port_key
from registrar import Registrar

VERSION = "1.0.0"
log = logging.getLogger("node")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or "").strip() or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


def build_config(args: argparse.Namespace) -> dict:
    hostname = socket.gethostname()
    proxy_port = args.port or _env_int("PROXY_PORT", 8888)
    egress_host = args.egress_host or _env("EGRESS_HOST", "127.0.0.1")
    egress_port = args.egress_port or _env_int("EGRESS_PORT", 0) or proxy_port
    return {
        "manager_url": args.manager_url or _env("MANAGER_URL", "http://manager:8000"),
        # manager 探测用（同一 docker 网络内可达的地址）
        "host": args.host or _env("NODE_HOST", hostname),
        "proxy_host": _env("PROXY_HOST", "0.0.0.0"),
        "proxy_port": proxy_port,
        # 客户端访问用（一般是宿主机映射出来的地址）
        "egress_host": egress_host,
        "egress_port": egress_port,
        "admin_port": _env_int("ADMIN_PORT", 8000),
        "region": _env("REGION", "local"),
        "tags": [t.strip() for t in _env("TAGS", "").split(",") if t.strip()],
        "upstreams": [u.strip() for u in _env("UPSTREAMS", "").split(",") if u.strip()],
        "max_conns": _env_int("MAX_CONNS", 512),
        "connect_timeout": _env_float("CONNECT_TIMEOUT", 10.0),
        "heartbeat_interval": _env_int("HEARTBEAT_INTERVAL", 10),
        "node_id": _env("NODE_ID") or None,
        "name": _env("NODE_NAME", hostname),
    }


def attach_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            # Windows 上不支持 add_signal_handler，退化成 signal.signal
            with contextlib.suppress(Exception):
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


async def run(cfg: dict) -> None:
    proxy = ProxyServer(
        host=cfg["proxy_host"],
        port=cfg["proxy_port"],
        # 先不传上游：得等自己和自身地址登记完，过滤才生效
        upstreams=None,
        max_conns=cfg["max_conns"],
        connect_timeout=cfg["connect_timeout"],
    )
    proxy.add_self_addrs([
        host_port_key(cfg["host"], cfg["proxy_port"]),
        host_port_key(cfg["egress_host"], cfg["egress_port"]),
        host_port_key(cfg["name"], cfg["proxy_port"]),
        host_port_key("127.0.0.1", cfg["proxy_port"]),
        host_port_key("localhost", cfg["proxy_port"]),
        host_port_key(cfg["proxy_host"], cfg["proxy_port"]),
    ])
    proxy.set_upstreams(cfg["upstreams"])
    await proxy.start()

    registrar = Registrar(
        manager_url=cfg["manager_url"],
        payload={
            "host": cfg["host"],
            "port": cfg["proxy_port"],
            "egress_host": cfg["egress_host"],
            "egress_port": cfg["egress_port"],
            "scheme": "http",
            "region": cfg["region"],
            "tags": cfg["tags"],
            "version": VERSION,
            "meta": {"name": cfg["name"], "python": sys.version.split()[0]},
        },
        node_id=cfg["node_id"],
        heartbeat_interval=cfg["heartbeat_interval"],
        local_upstreams=[u for u in cfg["upstreams"]],
    )
    await registrar.register_with_retry()
    proxy.set_upstreams(registrar.upstreams + registrar.local_upstreams)

    heartbeat_task = asyncio.create_task(registrar.heartbeat_loop(proxy), name="heartbeat")

    admin = AdminServer(
        host="0.0.0.0",
        port=cfg["admin_port"],
        routes={
            "/ping": lambda: {"ok": True, "node_id": registrar.node_id, "version": VERSION},
            "/stats": proxy.snapshot,
            "/upstreams": lambda: {"ok": True, "upstreams": proxy.upstreams},
            "/config": lambda: {k: v for k, v in cfg.items() if k not in ("upstreams",)},
        },
    )
    await admin.start()

    log.info(
        "节点就绪 | node_id=%s | 监听 %s:%s | 对外 %s:%s | region=%s | manager=%s",
        registrar.node_id, cfg["proxy_host"], cfg["proxy_port"],
        cfg["egress_host"], cfg["egress_port"], cfg["region"], cfg["manager_url"],
    )

    stop = asyncio.Event()
    attach_signal_handlers(asyncio.get_running_loop(), stop)
    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    finally:
        log.info("收到退出信号，开始关闭……")
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        with contextlib.suppress(Exception):
            await registrar.unregister()
        await asyncio.gather(admin.stop(), proxy.stop(), return_exceptions=True)
        log.info("节点已退出")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ProxyPool Node")
    parser.add_argument("--manager-url", help="manager 地址，如 http://manager:8000")
    parser.add_argument("--host", help="manager 用来探测本节点的地址（容器名/IP）")
    parser.add_argument("--port", type=int, help="本地代理监听端口（默认 8888）")
    parser.add_argument("--egress-host", help="下发给客户端的地址（默认 127.0.0.1）")
    parser.add_argument("--egress-port", type=int, help="下发给客户端的端口（默认同 --port）")
    parser.add_argument("--node-id", help="固定 node_id，便于容器重启后保持身份")
    parser.add_argument("--log-level", default=_env("LOG_LEVEL", "INFO"))
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s [node] %(message)s",
    )
    cfg = build_config(args)
    if args.node_id:
        cfg["node_id"] = args.node_id
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
