"""极简异步 HTTP/CONNECT 代理，支持上游链式转发（round-robin + 失败熔断）。

- 支持 `CONNECT host:port`（HTTPS 隧道）
- 支持绝对 URI 形式的明文 HTTP 请求（`GET http://a.b/c`）
- 支持相对路径 + Host 头（当浏览器/工具把本服务当普通代理用时）
- 配置了上游代理时，全部流量先 `CONNECT` 到上游，实现「链式跳板」
- 上游仅支持 http 协议代理（socks5 需要额外握手，不在本示例范围内）

只用标准库 asyncio，节点镜像不需要装任何第三方包。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import socket as _socket
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

log = logging.getLogger("node.proxy")

BUFFER_SIZE = 1 << 16          # 64 KiB
MAX_HEAD = 1 << 16             # 请求头上限
READ_HEAD_TIMEOUT = 15.0       # 读客户端请求头超时
HALF_CLOSE_GRACE = 10.0        # 客户端半关闭后，等响应的最长时间
UPSTREAM_FAIL_LIMIT = 3        # 上游连续失败多少次后临时剔除


def parse_authority(authority: str, default_port: int = 80) -> Tuple[str, int]:
    """解析 `host:port` / `[::1]:8080` / `host`。"""
    authority = authority.strip()
    if authority.startswith("["):  # IPv6
        host, _, rest = authority[1:].partition("]")
        port = rest.lstrip(":")
        return host, int(port) if port.isdigit() else default_port
    host, sep, port = authority.rpartition(":")
    if not sep or not port.isdigit():
        return authority, default_port
    return host, int(port)


def get_header(head: bytes, name: bytes) -> bytes:
    """从原始请求头里取某个头（大小写不敏感）。"""
    want = name.lower()
    for line in head.split(b"\r\n")[1:]:
        key, _, value = line.partition(b":")
        if key.strip().lower() == want:
            return value.strip()
    return b""


HOP_BY_HOP = (
    b"proxy-connection",
    b"proxy-authorization",
    b"proxy-authenticate",
    b"connection",
    b"keep-alive",
)


def host_port_key(host: str, port: int) -> str:
    """拼出 host:port 形式的比较键；host 为空或通配地址时返回空串。"""
    host = (host or "").strip().lower()
    if not host or host in ("0.0.0.0", "::", "[::]"):
        return ""
    return f"{host}:{port}"


def url_host_port(url: str) -> str:
    """从 url 里取 host:port 形式的比较键。"""
    parts = urlsplit(url)
    if not parts.hostname:
        return ""
    return f"{parts.hostname.lower()}:{parts.port or 80}"


def to_origin_form(head: bytes, target: str) -> bytes:
    """把请求行 `GET http://a.b/c?d=1 HTTP/1.1` 改写为 `GET /c?d=1 HTTP/1.1`。

    绝对 URI 形式只对代理合法，源服务器（尤其是 nginx 之类）通常不认，
    所以转发前必须改写；顺手丢掉 Proxy-* 这类逐跳头。
    """
    parts = urlsplit(target)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    lines = head.split(b"\r\n")
    request_line = lines[0]
    raw_target = target.encode("latin-1", "ignore")
    if raw_target in request_line:
        request_line = request_line.replace(raw_target, path.encode("latin-1", "ignore"), 1)

    kept = []
    for line in lines[1:]:
        if not line:
            continue
        key = line.partition(b":")[0].strip().lower()
        if key in HOP_BY_HOP:
            continue
        kept.append(line)
    # 补上 Connection: close —— 明文转发强制「一个连接一个请求」。
    # 否则客户端复用 keep-alive 连接时，第二个请求的请求行仍然是绝对 URI 形式，
    # 而这里只改写了第一个请求头，源站会直接把它当成路径，返回 400/404。
    return b"\r\n".join([request_line] + kept) + b"\r\nConnection: close\r\n\r\n"


def ensure_close(head: bytes) -> bytes:
    """相对路径形式（少见）时也补上 Connection: close。"""
    if get_header(head, b"connection"):
        return head
    return head.rstrip(b"\r\n") + b"\r\nConnection: close\r\n\r\n"


class ProxyServer:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8888,
        upstreams: Optional[List[str]] = None,
        max_conns: int = 512,
        connect_timeout: float = 10.0,
    ) -> None:
        self.host = host
        self.port = port
        self.max_conns = max_conns
        self.connect_timeout = connect_timeout

        self._server: Optional[asyncio.AbstractServer] = None
        self._sem = asyncio.Semaphore(max(8, max_conns))

        self._upstreams: List[str] = []
        self._upstream_fails: Dict[str, int] = {}
        self._rr = 0
        # 本节点自己的地址：共享上游池是全局的，里面很可能有自己，
        # 不过滤掉会变成「自己连自己」的无限递归。
        self.self_addrs: set = set()

        self.stats: Dict[str, int] = {
            "active": 0,        # 当前活跃连接
            "total": 0,         # 累计连接
            "errors": 0,        # 累计失败
            "bytes_in": 0,      # 客户端 -> 目标
            "bytes_out": 0,     # 目标 -> 客户端
        }
        self.started_at = time.time()
        if upstreams:
            self.set_upstreams(upstreams)

    # ------------------------------------------------------------ 上游管理
    @property
    def upstreams(self) -> List[str]:
        return list(self._upstreams)

    def add_self_addrs(self, addrs: List[str]) -> None:
        """登记本节点自己的地址（host:port），用于从共享上游里把自己过滤掉。"""
        for addr in addrs:
            key = (addr or "").strip().lower()
            if key:
                self.self_addrs.add(key)

    def set_upstreams(self, urls: Optional[List[str]]) -> None:
        normalized: List[str] = []
        for raw in urls or []:
            url = (raw or "").strip()
            if not url:
                continue
            if "://" not in url:
                url = "http://" + url
            if not url.startswith("http://"):
                log.warning("暂不支持的上游协议，已忽略：%s", url)
                continue
            if url_host_port(url) in self.self_addrs:
                log.info("共享上游里指向本节点自己，已忽略：%s", url)
                continue
            if url not in normalized:
                normalized.append(url)
        if normalized == self._upstreams:
            return
        self._upstreams = normalized
        self._upstream_fails = {u: 0 for u in normalized}
        log.info("上游列表已更新：%d 条", len(normalized))

    def _pick_upstream(self) -> Optional[str]:
        if not self._upstreams:
            return None
        n = len(self._upstreams)
        for _ in range(n):
            url = self._upstreams[self._rr % n]
            self._rr += 1
            if self._upstream_fails.get(url, 0) < UPSTREAM_FAIL_LIMIT:
                return url
        # 全部处于熔断状态：清零后重试一轮
        self._upstream_fails = {u: 0 for u in self._upstreams}
        url = self._upstreams[self._rr % n]
        self._rr += 1
        return url

    def _mark_upstream(self, url: Optional[str], ok: bool) -> None:
        if not url:
            return
        if ok:
            self._upstream_fails[url] = 0
        else:
            self._upstream_fails[url] = self._upstream_fails.get(url, 0) + 1

    # ---------------------------------------------------------------- 生命周期
    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_client, self.host, self.port)
        addrs = ", ".join(str(s.getsockname()) for s in (self._server.sockets or []))
        log.info("代理服务已监听 %s", addrs or f"{self.host}:{self.port}")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    def snapshot(self) -> Dict[str, object]:
        return {
            "ok": True,
            "listen": f"{self.host}:{self.port}",
            "uptime_sec": round(time.time() - self.started_at, 1),
            "upstreams": self.upstreams,
            "upstream_fails": {k: v for k, v in self._upstream_fails.items() if v},
            "max_conns": self.max_conns,
            **self.stats,
        }

    # ------------------------------------------------------------------ 主流程
    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await self._sem.acquire()
        self.stats["active"] += 1
        self.stats["total"] += 1
        remote_writer: Optional[asyncio.StreamWriter] = None
        upstream_used: Optional[str] = None
        try:
            with contextlib.suppress(OSError):
                sock = writer.get_extra_info("socket")
                if sock is not None:
                    sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, True)

            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), READ_HEAD_TIMEOUT)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
                return  # 客户端没发完整请求头，直接断开

            if len(head) > MAX_HEAD:
                await self._reply(writer, 431, "Request Header Fields Too Large")
                return

            try:
                request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
                method, target, _version = request_line.split(" ", 2)
            except ValueError:
                await self._reply(writer, 400, "Bad Request")
                return
            method = method.upper()

            absolute_form = False
            if method == "CONNECT":
                host, port = parse_authority(target, 443)
            else:
                parts = urlsplit(target)
                if parts.hostname:
                    absolute_form = True
                    host = parts.hostname
                    port = parts.port or (443 if parts.scheme == "https" else 80)
                else:
                    # 相对路径：退化成用 Host 头
                    host_header = get_header(head, b"host").decode("latin-1")
                    if not host_header:
                        await self._reply(writer, 400, "Bad Request: 缺少 Host")
                        return
                    host, port = parse_authority(host_header, 80)

            try:
                remote_reader, remote_writer, upstream_used = await self._open_remote(host, port)
            except Exception as exc:
                self._mark_upstream(upstream_used, ok=False)
                self.stats["errors"] += 1
                log.debug("连接 %s:%s 失败：%s", host, port, exc)
                await self._reply(writer, 502, "Bad Gateway")
                return

            self._mark_upstream(upstream_used, ok=True)

            if method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\nProxy-Agent: proxypool-node\r\n\r\n")
                await writer.drain()
            else:
                # 明文 HTTP：代理收到的是绝对 URI，转发给源服务器前改写成源服务器形式
                forward_head = to_origin_form(head, target) if absolute_form else ensure_close(head)
                remote_writer.write(forward_head)
                await remote_writer.drain()

            await self._tunnel(reader, writer, remote_reader, remote_writer)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        except Exception:
            self.stats["errors"] += 1
            log.exception("处理连接时出现异常")
        finally:
            self.stats["active"] -= 1
            self._sem.release()
            for w in (writer, remote_writer):
                if w is None:
                    continue
                with contextlib.suppress(Exception):
                    w.close()
                    await w.wait_closed()

    async def _open_remote(
        self, host: str, port: int
    ) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter, Optional[str]]:
        """直连或经上游代理连接目标，返回 (reader, writer, 使用过的上游)。"""
        upstream = self._pick_upstream()
        if upstream is None:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), self.connect_timeout
            )
            return reader, writer, None

        parts = urlsplit(upstream)
        up_host = parts.hostname or ""
        up_port = parts.port or 80
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(up_host, up_port), self.connect_timeout
        )
        try:
            head = (
                f"CONNECT {host}:{port} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                f"Proxy-Connection: keep-alive\r\n\r\n"
            ).encode("latin-1")
            writer.write(head)
            await writer.drain()
            resp = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self.connect_timeout)
        except Exception:
            with contextlib.suppress(Exception):
                writer.close()
            raise
        status_line = resp.split(b"\r\n", 1)[0].decode("latin-1", "ignore")
        if " 200" not in status_line:
            with contextlib.suppress(Exception):
                writer.close()
            raise RuntimeError(f"上游 {upstream} 拒绝了 CONNECT：{status_line}")
        return reader, writer, upstream

    async def _tunnel(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        remote_reader: asyncio.StreamReader,
        remote_writer: asyncio.StreamWriter,
    ) -> None:
        up = asyncio.create_task(self._pipe(client_reader, remote_writer, "bytes_in"))
        down = asyncio.create_task(self._pipe(remote_reader, client_writer, "bytes_out"))
        try:
            done, _pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
            if up in done and not down.done():
                # 客户端请求发送完毕：半关闭远端写端，继续等响应回来
                with contextlib.suppress(Exception):
                    if remote_writer.can_write_eof():
                        remote_writer.write_eof()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait({down}, timeout=HALF_CLOSE_GRACE)
        finally:
            for task in (up, down):
                if not task.done():
                    task.cancel()
            await asyncio.gather(up, down, return_exceptions=True)

    async def _pipe(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, counter: str
    ) -> None:
        try:
            while True:
                data = await reader.read(BUFFER_SIZE)
                if not data:
                    break
                self.stats[counter] += len(data)
                writer.write(data)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        except Exception:
            log.debug("转发中断", exc_info=True)
        finally:
            with contextlib.suppress(Exception):
                if writer.can_write_eof():
                    writer.write_eof()

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: int, message: str) -> None:
        body = f"{status} {message}".encode("utf-8")
        with contextlib.suppress(Exception):
            writer.write(
                b"HTTP/1.1 %d %s\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                b"Connection: close\r\n"
                b"Content-Length: %d\r\n\r\n" % (status, message.encode("latin-1", "ignore"), len(body))
            )
            writer.write(body)
            await writer.drain()
