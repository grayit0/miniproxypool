"""与 manager 通信：注册 / 心跳 / 注销。只用标准库 urllib，放在线程里跑。"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("node.registrar")


class Registrar:
    def __init__(
        self,
        manager_url: str,
        payload: Dict[str, Any],
        node_id: Optional[str] = None,
        timeout: float = 5.0,
        heartbeat_interval: int = 10,
        local_upstreams: Optional[List[str]] = None,
    ) -> None:
        self.manager_url = manager_url.rstrip("/")
        self.payload = dict(payload)
        self.node_id = node_id
        self.timeout = timeout
        self.heartbeat_interval = heartbeat_interval
        self.local_upstreams = list(local_upstreams or [])
        self.upstreams: List[str] = []
        # 明确绕过系统代理，否则容器里的 HTTP_PROXY 会把注册请求也代理掉
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ------------------------------------------------------------------ HTTP
    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
        url = self.manager_url + path
        data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json; charset=utf-8",
                     "User-Agent": "proxypool-node/1.0"},
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw.decode("utf-8")) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                body = {"detail": raw.decode("utf-8", "ignore")[:200]}
            return exc.code, body
        except Exception as exc:  # 网络层错误
            return 0, {"detail": f"{type(exc).__name__}: {exc}"}

    async def _request_async(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None):
        return await asyncio.to_thread(self._request, method, path, payload)

    # -------------------------------------------------------------- 注册流程
    async def register_once(self) -> bool:
        payload = dict(self.payload)
        if self.node_id:
            payload["node_id"] = self.node_id
        status, body = await self._request_async("POST", "/api/nodes/register", payload)
        if status == 200 and body.get("node_id"):
            self.node_id = body["node_id"]
            self.heartbeat_interval = int(body.get("heartbeat_interval") or self.heartbeat_interval)
            self.upstreams = list(body.get("upstreams") or [])
            log.info(
                "注册成功 node_id=%s 心跳周期=%ss 共享上游=%d 条",
                self.node_id, self.heartbeat_interval, len(self.upstreams),
            )
            return True
        log.warning("注册失败 status=%s %s", status, body.get("detail", body))
        return False

    async def register_with_retry(self, max_backoff: float = 30.0) -> bool:
        delay = 1.0
        while True:
            try:
                if await self.register_once():
                    return True
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("注册时出现未预期异常")
            log.info("%.0fs 后重试注册 manager=%s", delay, self.manager_url)
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_backoff)

    # -------------------------------------------------------------- 心跳循环
    async def heartbeat_loop(self, proxy) -> None:
        consecutive_fail = 0
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            payload = {
                "active_conns": proxy.stats.get("active", 0),
                "served": proxy.stats.get("total", 0),
            }
            status, body = await self._request_async(
                "POST", f"/api/nodes/{self.node_id}/heartbeat", payload
            )
            if status == 200:
                consecutive_fail = 0
                if "upstreams" in body:
                    self.upstreams = list(body.get("upstreams") or [])
                    proxy.set_upstreams(self.upstreams + self.local_upstreams)
                continue

            consecutive_fail += 1
            log.warning("心跳失败 status=%s %s（连续 %d 次）", status, body.get("detail", ""), consecutive_fail)
            if status in (404, 410) or consecutive_fail >= 3:
                log.info("重新注册节点……")
                if await self.register_with_retry():
                    proxy.set_upstreams(self.upstreams + self.local_upstreams)
                    consecutive_fail = 0

    async def unregister(self) -> None:
        if not self.node_id:
            return
        status, _ = await self._request_async("POST", f"/api/nodes/{self.node_id}/unregister", {})
        log.info("已向 manager 注销 node_id=%s status=%s", self.node_id, status)
