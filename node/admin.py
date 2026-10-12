"""节点自省 HTTP 服务：/ping /stats /upstreams。

只是给运维排查用，不做认证，因此不要把它暴露到公网。
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("node.admin")

READ_TIMEOUT = 5.0


class AdminServer:
    def __init__(self, host: str, port: int, routes: Dict[str, Callable[[], Any]]) -> None:
        self.host = host
        self.port = port
        self.routes = routes
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        log.info("管理端口已监听 %s:%d（%s）", self.host, self.port, " ".join(sorted(self.routes)))

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), READ_TIMEOUT)
            line = head.split(b"\r\n", 1)[0].decode("latin-1", "ignore")
            parts = line.split(" ")
            raw_path = parts[1] if len(parts) > 1 else "/"
            path = raw_path.split("?", 1)[0].rstrip("/") or "/"
            handler = self.routes.get(path)
            if handler is None:
                await self._send(writer, 404, {"ok": False, "error": "not found",
                                               "routes": sorted(self.routes)})
                return
            payload = handler()
            if inspect.isawaitable(payload):
                payload = await payload
            await self._send(writer, 200, payload)
        except Exception as exc:
            log.debug("管理请求处理失败: %s", exc)
            with contextlib.suppress(Exception):
                await self._send(writer, 500, {"ok": False, "error": str(exc)})
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        writer.write(
            b"HTTP/1.1 %d %s\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            b"Connection: close\r\n"
            b"Content-Length: %d\r\n\r\n" % (status, b"OK" if status == 200 else b"Error", len(body))
        )
        writer.write(body)
        await writer.drain()
