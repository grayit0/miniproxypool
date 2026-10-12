"""ProxyPool Manager —— 节点的注册中心 + 代理分发 API。

职责：
1. 接收 node 的注册 / 心跳 / 注销；
2. 周期性主动探测节点存活（TCP 连接 manager 侧可达的 host:port）；
3. 维护一份共享的「上游代理池」，心跳响应里下发给各节点；
4. 给 client 提供取代理的只读接口。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query

from .config import settings
from .schemas import HeartbeatRequest, RegisterRequest, ReportBadRequest, UpstreamBulkRequest
from .store import Store

try:  # 校验上游代理需要 httpx；缺失时自动降级为「不校验上游」
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s %(levelname)-7s [manager] %(message)s",
)
log = logging.getLogger("manager")

store = Store(settings.db_path)
START_TIME = time.time()
VERSION = "1.0.0"


# ---------------------------------------------------------------- 生命周期
async def validator_loop() -> None:
    """后台巡检：节点 TCP 可达性 + 上游代理可用性 + 掉线超时。"""
    sem = asyncio.Semaphore(max(1, settings.check_concurrency))
    while True:
        try:
            nodes = store.list_nodes(alive_only=False)
            if nodes:
                await asyncio.gather(
                    *(_check_node(n, sem) for n in nodes), return_exceptions=True
                )
            kicked = store.expire_stale(settings.node_ttl)
            if kicked:
                log.info("心跳超时，%d 个节点被标记为掉线", kicked)

            if settings.check_upstreams and httpx is not None:
                # 校验时要把「之前失败过的」也捞出来，给它们恢复的机会
                ups = store.list_upstreams()[: max(1, settings.max_upstream_check)]
                if ups:
                    await asyncio.gather(
                        *(_check_upstream(u["url"], sem) for u in ups), return_exceptions=True
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # 巡检本身不能把服务搞崩
            log.exception("巡检轮询异常")
        await asyncio.sleep(max(2.0, settings.check_interval))


async def _check_node(node: Dict[str, Any], sem: asyncio.Semaphore) -> None:
    async with sem:
        ok, latency = await _probe_tcp(node["host"], int(node["port"]), settings.check_timeout)
    before = node["alive"]
    store.mark_check(node["node_id"], ok, latency, settings.fail_threshold)
    if before and not ok and store.get_node(node["node_id"] or ""):
        after = store.get_node(node["node_id"])
        if after and not after["alive"]:
            log.warning("节点 %s (%s:%s) 连续探测失败，已下线", node["node_id"], node["host"], node["port"])


async def _check_upstream(url: str, sem: asyncio.Semaphore) -> None:
    async with sem:
        ok, latency = await _probe_http_proxy(url, settings.check_timeout, settings.check_url)
    store.mark_upstream(url, ok, latency)


async def _probe_tcp(host: str, port: int, timeout: float) -> tuple:
    t0 = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True, round((time.perf_counter() - t0) * 1000, 2)
    except Exception:
        return False, None


async def _probe_http_proxy(proxy_url: str, timeout: float, target: str) -> tuple:
    """用该代理访问 target，判断它是否真的能出网。"""
    try:
        kwargs = {"timeout": timeout, "follow_redirects": True}
        try:
            client = httpx.AsyncClient(proxy=proxy_url, **kwargs)          # httpx >= 0.26
        except TypeError:
            client = httpx.AsyncClient(proxies=proxy_url, **kwargs)        # httpx < 0.26
        async with client:
            t0 = time.perf_counter()
            resp = await client.get(target)
            latency = round((time.perf_counter() - t0) * 1000, 2)
            return resp.status_code < 400, latency
    except Exception:
        return False, None


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info(
        "manager 启动 v%s | db=%s | 心跳周期=%ss TTL=%ss 巡检=%ss",
        VERSION, settings.db_path, settings.heartbeat_interval, settings.node_ttl, settings.check_interval,
    )
    if settings.check_upstreams and httpx is None:
        log.warning("未安装 httpx，上游代理自动校验已禁用")
    task = asyncio.create_task(validator_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        store.close()
        log.info("manager 已停止")


app = FastAPI(
    title="ProxyPool Manager",
    version=VERSION,
    description="Docker 代理节点池的注册中心与分发接口",
    lifespan=lifespan,
)


def require_admin(x_admin_token: Optional[str] = Header(default=None)) -> None:
    """设置了 ADMIN_TOKEN 时，管理接口需要带 X-Admin-Token 头。"""
    if settings.admin_token and x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="X-Admin-Token 无效")


def _to_proxy(node: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "node_id": node["node_id"],
        "url": f"{node['scheme']}://{node['egress_host']}:{node['egress_port']}",
        "host": node["egress_host"],
        "port": node["egress_port"],
        "scheme": node["scheme"],
        "region": node["region"],
        "tags": node["tags"],
        "latency_ms": node["latency_ms"],
        "last_seen": node["last_seen"],
    }


def _pick(count: int, region: Optional[str] = None, scheme: Optional[str] = None) -> List[Dict[str, Any]]:
    store.expire_stale(settings.node_ttl)
    nodes = store.list_nodes(alive_only=True, region=region)
    if scheme:
        nodes = [n for n in nodes if n["scheme"] == scheme]
    # 探测延迟小的排前面；没探测过的排最后
    nodes.sort(key=lambda n: (n["latency_ms"] is None, n["latency_ms"] or 0.0))
    return [_to_proxy(n) for n in nodes[:count]]


# ------------------------------------------------------------ 节点侧接口
@app.post("/api/nodes/register", tags=["node"])
def register_node(req: RegisterRequest) -> Dict[str, Any]:
    node_id = (req.node_id or "").strip() or uuid.uuid4().hex[:12]
    node = store.upsert_node(
        node_id=node_id,
        host=req.host,
        port=req.port,
        egress_host=req.egress_host or req.host,
        egress_port=req.egress_port or req.port,
        scheme=req.scheme or "http",
        region=req.region or "",
        tags=req.tags,
        version=req.version,
        meta=req.meta,
        name=str(req.meta.get("name", "")) if isinstance(req.meta, dict) else "",
    )
    log.info(
        "节点注册 %s | 探测地址=%s:%s | 对外地址=%s:%s | region=%s",
        node_id, node["host"], node["port"],
        node["egress_host"], node["egress_port"], node["region"] or "-",
    )
    return {
        "ok": True,
        "node_id": node_id,
        "heartbeat_interval": settings.heartbeat_interval,
        "node_ttl": settings.node_ttl,
        "upstreams": [u["url"] for u in store.list_upstreams(usable_only=True)],
    }


@app.post("/api/nodes/{node_id}/heartbeat", tags=["node"])
def heartbeat(node_id: str, req: HeartbeatRequest) -> Dict[str, Any]:
    if not store.heartbeat(
        node_id, active_conns=req.active_conns, served=req.served, latency_ms=req.latency_ms
    ):
        raise HTTPException(status_code=404, detail="节点不存在，请重新注册")
    return {
        "ok": True,
        "heartbeat_interval": settings.heartbeat_interval,
        "upstreams": [u["url"] for u in store.list_upstreams(usable_only=True)],
        "pool_size": len(store.list_nodes(alive_only=True)),
    }


@app.post("/api/nodes/{node_id}/unregister", tags=["node"])
def unregister_node(node_id: str) -> Dict[str, Any]:
    removed = store.delete_node(node_id)
    log.info("节点注销 %s (removed=%s)", node_id, removed)
    return {"ok": True, "removed": removed}


@app.post("/api/nodes/{node_id}/report_bad", tags=["node"])
def report_bad(node_id: str, req: Optional[ReportBadRequest] = None) -> Dict[str, Any]:
    """client 使用代理失败时上报，累计到阈值就摘掉该节点。"""
    result = store.report_bad(node_id, settings.fail_threshold)
    if not result["found"]:
        raise HTTPException(status_code=404, detail="节点不存在")
    if not result["alive"]:
        log.warning("节点 %s 被客户端连续上报失败，已下线（原因：%s）",
                    node_id, (req.reason if req else "")[:120])
    return {"ok": True, **result}


# ------------------------------------------------------------ client 侧接口
@app.get("/api/proxies", tags=["client"])
def list_proxies(
    count: int = Query(10, ge=1, le=500, description="最多返回多少个"),
    region: Optional[str] = Query(None, description="按 region 过滤"),
    scheme: Optional[str] = Query(None, description="按协议过滤，如 http"),
) -> Dict[str, Any]:
    proxies = _pick(count, region, scheme)
    return {"count": len(proxies), "proxies": proxies}


@app.get("/api/proxy", tags=["client"])
def random_proxy(region: Optional[str] = None) -> Dict[str, Any]:
    """随机返回一个可用代理。"""
    proxies = _pick(200, region)
    if not proxies:
        raise HTTPException(status_code=503, detail="池中没有可用节点")
    return random.choice(proxies)


# ------------------------------------------------------------ 运维接口
@app.get("/api/nodes", tags=["admin"])
def list_nodes(_: None = Depends(require_admin)) -> Dict[str, Any]:
    store.expire_stale(settings.node_ttl)
    nodes = store.list_nodes(alive_only=False)
    return {"count": len(nodes), "nodes": nodes}


@app.get("/api/stats", tags=["admin"])
def stats() -> Dict[str, Any]:
    store.expire_stale(settings.node_ttl)
    nodes = store.list_nodes(alive_only=False)
    upstreams = store.list_upstreams()
    by_region: Dict[str, int] = {}
    for n in nodes:
        if n["alive"]:
            key = n["region"] or "-"
            by_region[key] = by_region.get(key, 0) + 1
    return {
        "uptime_sec": round(time.time() - START_TIME, 1),
        "nodes": {
            "total": len(nodes),
            "alive": sum(1 for n in nodes if n["alive"]),
            "dead": sum(1 for n in nodes if not n["alive"]),
            "by_region": by_region,
            "active_conns": sum(int(n.get("active_conns") or 0) for n in nodes),
            "served": sum(int(n.get("served") or 0) for n in nodes),
        },
        "upstreams": {
            "total": len(upstreams),
            "ok": sum(1 for u in upstreams if u["ok"]),
        },
        "config": {
            "heartbeat_interval": settings.heartbeat_interval,
            "node_ttl": settings.node_ttl,
            "check_interval": settings.check_interval,
            "check_url": settings.check_url,
        },
    }


@app.get("/api/upstreams", tags=["admin"])
def list_upstreams(_: None = Depends(require_admin)) -> Dict[str, Any]:
    items = store.list_upstreams(usable_only=False)
    return {"count": len(items), "upstreams": items}


@app.post("/api/upstreams", tags=["admin"])
def add_upstreams(req: UpstreamBulkRequest, _: None = Depends(require_admin)) -> Dict[str, Any]:
    """批量灌入上游代理，节点会在下次心跳时拿到。"""
    inserted = store.add_upstreams(req.urls, req.source)
    return {"ok": True, "received": len(req.urls), "inserted": inserted}


@app.delete("/api/upstreams", tags=["admin"])
def delete_upstream(url: str = Query(..., description="要删除的上游 url，可带 http:// 前缀"),
                    _: None = Depends(require_admin)) -> Dict[str, Any]:
    norm = url if "://" in url else "http://" + url
    return {"ok": store.delete_upstream(norm) or store.delete_upstream(url)}


@app.post("/api/nodes/cleanup", tags=["admin"])
def cleanup(older_than: float = Query(3600.0, description="掉线超过多少秒的节点直接删除"),
            _: None = Depends(require_admin)) -> Dict[str, Any]:
    return {"ok": True, "deleted_nodes": store.delete_dead_nodes(older_than)}


@app.get("/health", tags=["ops"])
def health() -> Dict[str, Any]:
    return {"status": "ok", "version": VERSION, "uptime_sec": round(time.time() - START_TIME, 1)}


@app.get("/", tags=["ops"])
def index() -> Dict[str, Any]:
    return {
        "service": "proxypool-manager",
        "version": VERSION,
        "docs": "/docs",
        "endpoints": {
            "node": [
                "POST /api/nodes/register",
                "POST /api/nodes/{node_id}/heartbeat",
                "POST /api/nodes/{node_id}/unregister",
            ],
            "client": [
                "GET /api/proxies?count=10&region=",
                "GET /api/proxy",
                "POST /api/nodes/{node_id}/report_bad",
            ],
            "admin": [
                "GET /api/nodes",
                "GET /api/stats",
                "GET /api/upstreams",
                "POST /api/upstreams",
                "POST /api/nodes/cleanup",
            ],
        },
    }
