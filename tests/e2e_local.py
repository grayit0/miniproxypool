"""不开 Docker 的本地端到端测试。

直接拉起 manager（uvicorn）+ 两个 node 进程 + 一个回显用的目标 HTTP 服务，
然后验证：注册 / 心跳 / 分发 / 明文 HTTP 转发 / CONNECT 隧道 / 上游链式转发 /
client 库 / report_bad 摘除与恢复。

用法（manager 的依赖需要先装好：fastapi uvicorn httpx）：
    python tests/e2e_local.py

只用标准库断言，client 库那部分需要 requests，缺失时自动跳过。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / ".e2e-logs"

_results: list = []


# --------------------------------------------------------------------- 工具
def check(name: str, ok, detail: str = "") -> bool:
    _results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  <- {detail}" if detail and not ok else ""))
    return bool(ok)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------- 节点实现选择
# 有 Go 二进制就用 Go 节点，否则退回 Python 节点。可用 E2E_NODE_ENGINE=go|python 强制。
_FORCED_ENGINE = (os.getenv("E2E_NODE_ENGINE") or "").strip().lower()


def node_engine() -> str:
    if _FORCED_ENGINE in ("go", "python"):
        return _FORCED_ENGINE
    for name in ("proxypool-node.exe", "proxypool-node"):
        if (ROOT / "node-go" / name).exists():
            return "go"
    return "python"


def node_argv() -> list:
    if node_engine() == "go":
        for name in ("proxypool-node.exe", "proxypool-node"):
            path = ROOT / "node-go" / name
            if path.exists():
                return [str(path)]
        raise RuntimeError(
            "找不到 Go 节点二进制。先执行：cd node-go && go build -o proxypool-node.exe ."
        )
    return [sys.executable, "-u", "entrypoint.py"]


def node_cwd() -> Path:
    return ROOT / ("node-go" if node_engine() == "go" else "node")


_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(method: str, url: str, payload=None, timeout: float = 10.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with _direct.open(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else {}
        except ValueError:
            return exc.code, {"raw": raw.decode("utf-8", "ignore")}


def wait_until(fn, timeout: float = 20.0, interval: float = 0.3):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = fn()
            if last:
                return last
        except Exception as exc:  # 服务还在起来
            last = exc
        time.sleep(interval)
    return None


def get_via_proxy(url: str, proxy: str, timeout: float = 10.0):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    with opener.open(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def node_stat(port: int, key: str):
    """读节点管理端口的某个统计值；读不到就返回 None。"""
    try:
        status, body = api("GET", f"http://127.0.0.1:{port}/stats")
        return body.get(key) if status == 200 else None
    except Exception:
        return None


# ------------------------------------------------------------- 目标回显服务
class EchoHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        body = json.dumps({"ok": True, "path": self.path, "host": self.headers.get("Host")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静音
        pass


def start_target() -> tuple:
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), EchoHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


# ------------------------------------------------------------------ 进程管理
def start_proc(name: str, cwd: Path, args: list, env_extra: dict):
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"{name}.log"
    env = dict(os.environ)
    env.update({k: str(v) for k, v in env_extra.items()})
    for noisy in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(noisy, None)
    handle = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(args, cwd=str(cwd), env=env, stdout=handle, stderr=subprocess.STDOUT)
    return proc, handle, log_path


def dump_logs(paths):
    for path in paths:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            print(f"\n--- {path.name} ---")
            print("\n".join(text.splitlines()[-40:]))


# ------------------------------------------------------------------ 主流程
def main() -> int:
    manager_port = free_port()
    node1_proxy, node1_admin = free_port(), free_port()
    node2_proxy, node2_admin = free_port(), free_port()
    manager_url = f"http://127.0.0.1:{manager_port}"

    target, target_port = start_target()
    target_url = f"http://127.0.0.1:{target_port}/plain"
    print(f"目标回显服务: {target_url}")

    procs, handles, logs = [], [], []
    db_file = ROOT / "manager" / "data" / "e2e.db"
    if db_file.exists():
        db_file.unlink()

    try:
        # ---------------------------------------------------------- manager
        print("\n[1] 启动 manager")
        proc, h, log = start_proc(
            "manager", ROOT / "manager",
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(manager_port), "--no-access-log"],
            {
                "DB_PATH": "./data/e2e.db",
                "HEARTBEAT_INTERVAL": 2,
                "NODE_TTL": 12,
                "CHECK_INTERVAL": 3,
                "CHECK_TIMEOUT": 1,
                "CHECK_UPSTREAMS": 0,
                "FAIL_THRESHOLD": 2,
                "LOG_LEVEL": "INFO",
            },
        )
        procs.append(proc); handles.append(h); logs.append(log)
        ok = wait_until(lambda: api("GET", f"{manager_url}/health")[0] == 200, timeout=30)
        check("manager /health 就绪", ok)
        if not ok:
            dump_logs(logs)
            return 1

        # ------------------------------------------------------------ node1
        print(f"\n[2] 启动 node1（{node_engine()} 实现）")
        proc, h, log = start_proc(
            "node1", node_cwd(), node_argv(),
            {
                "MANAGER_URL": manager_url,
                "NODE_HOST": "127.0.0.1",
                "NODE_NAME": "node1",
                "PROXY_PORT": node1_proxy,
                "ADMIN_PORT": node1_admin,
                "EGRESS_HOST": "127.0.0.1",
                "EGRESS_PORT": node1_proxy,
                "REGION": "local",
                "LOG_LEVEL": "INFO",
                # 故意把节点自己的地址当上游灌进去：节点必须把它过滤掉，
                # 否则会 CONNECT 给自己、再给自己，无限递归到超时。
                "UPSTREAMS": f"http://127.0.0.1:{node1_proxy}",
            },
        )
        procs.append(proc); handles.append(h); logs.append(log)

        node1_ping = wait_until(
            lambda: (lambda r: r[1] if r[0] == 200 and r[1].get("node_id") else None)(
                api("GET", f"http://127.0.0.1:{node1_admin}/ping")),
            timeout=20,
        )
        check("node1 管理端口 /ping 可用", bool(node1_ping) and node1_ping.get("node_id"))
        node1_id = (node1_ping or {}).get("node_id", "")
        print(f"      node1 node_id = {node1_id}")

        alive = wait_until(lambda: api("GET", f"{manager_url}/api/stats")[1]["nodes"]["alive"], timeout=20)
        check("manager 收到 node1 注册（alive>=1）", bool(alive), f"alive={alive}")

        status, proxies = api("GET", f"{manager_url}/api/proxies?count=10")
        urls = [p["url"] for p in proxies.get("proxies", [])]
        check("GET /api/proxies 返回对外地址", f"http://127.0.0.1:{node1_proxy}" in urls, str(urls))

        node1_ups = api("GET", f"http://127.0.0.1:{node1_admin}/upstreams")[1].get("upstreams") or []
        check("节点把指向自己的上游过滤掉了（避免自己连自己无限递归）",
              f"http://127.0.0.1:{node1_proxy}" not in node1_ups, str(node1_ups))

        # --------------------------------------------------- 明文 HTTP 转发
        print("\n[3] 经 node1 转发明文 HTTP 请求")
        try:
            body = get_via_proxy(target_url, f"http://127.0.0.1:{node1_proxy}")
            check("明文 HTTP 转发成功（且请求行已改写成源服务器形式）",
                  body.get("ok") and body.get("path") == "/plain", str(body))
        except Exception as exc:
            check("明文 HTTP 转发成功（且请求行已改写成源服务器形式）", False, repr(exc))

        # ------------------------------------------------------ CONNECT 隧道
        print("\n[4] 经 node1 走 CONNECT 隧道")
        try:
            with socket.create_connection(("127.0.0.1", node1_proxy), timeout=10) as sock:
                sock.sendall(f"CONNECT 127.0.0.1:{target_port} HTTP/1.1\r\n"
                             f"Host: 127.0.0.1:{target_port}\r\n\r\n".encode())
                head = sock.recv(4096)
                check("CONNECT 返回 200", b" 200 " in head.split(b"\r\n")[0], head[:80].decode("latin-1"))
                sock.sendall(b"GET /tunnel HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
                buf = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                check("隧道内请求拿到响应", b"/tunnel" in buf and b"200 OK" in buf, buf[:120].decode("latin-1"))
        except Exception as exc:
            check("CONNECT 隧道可用", False, repr(exc))

        # -------------------------------------------- manager 下发共享上游
        print("\n[5] 启动 node2，让 manager 下发 node1 作为上游（链式转发）")
        status, resp = api("POST", f"{manager_url}/api/upstreams",
                           {"urls": [f"http://127.0.0.1:{node1_proxy}"], "source": "e2e"})
        check("POST /api/upstreams 写入共享上游", status == 200 and resp.get("inserted") == 1, str(resp))

        proc, h, log = start_proc(
            "node2", node_cwd(), node_argv(),
            {
                "MANAGER_URL": manager_url,
                "NODE_HOST": "127.0.0.1",
                "NODE_NAME": "node2",
                "PROXY_PORT": node2_proxy,
                "ADMIN_PORT": node2_admin,
                "EGRESS_HOST": "127.0.0.1",
                "EGRESS_PORT": node2_proxy,
                "REGION": "chain",
                "LOG_LEVEL": "INFO",
            },
        )
        procs.append(proc); handles.append(h); logs.append(log)

        ups = wait_until(
            lambda: (lambda r: r[1].get("upstreams") if r[0] == 200 and r[1].get("upstreams") else None)(
                api("GET", f"http://127.0.0.1:{node2_admin}/upstreams")),
            timeout=25,
        )
        check("node2 通过心跳拿到共享上游", bool(ups), str(ups))

        before = node_stat(node1_admin, "total")
        try:
            body = get_via_proxy(target_url, f"http://127.0.0.1:{node2_proxy}")
            check("经 node2 → node1 链式转发成功", body.get("ok") and body.get("path") == "/plain", str(body))
        except Exception as exc:
            check("经 node2 → node1 链式转发成功", False, repr(exc))
        after = node_stat(node1_admin, "total")
        check("node1 的连接计数确实增长了（说明流量真的过了它）",
              before is not None and after is not None and after > before, f"{before} -> {after}")

        # ------------------------------------------------------- client 库
        print("\n[6] 验证 Python client 库")
        sys.path.insert(0, str(ROOT / "client"))
        try:
            import requests  # noqa: F401
            from proxypool_client import ProxyPoolClient, RotatingSession
            pool = ProxyPoolClient(manager_url)
            check("client 拿到 manager 健康信息", pool.health().get("status") == "ok")
            listed = pool.list_proxies(count=10)
            check("client 列出代理", len(listed) >= 2, f"{len(listed)} 个")
            one = pool.get_proxy()
            check("client 取到单个代理", bool(one.url), one.url)
            with RotatingSession(pool, retries=3) as sess:
                resp = sess.get(target_url)
            check("RotatingSession 请求成功", resp.status_code == 200 and resp.json().get("ok"), resp.text[:100])

            # 回归用例：客户端在同一连接上复用（keep-alive）时，第二个请求的请求行
            # 仍然是绝对 URI 形式，代理必须重写成源服务器形式，否则源站会直接 400/404。
            ka = requests.Session()
            ka.trust_env = False
            try:
                r1 = ka.get(target_url, proxies=one.proxies, timeout=10)
                r2 = ka.get(target_url, proxies=one.proxies, timeout=10)
                check("同一连接上连续两次请求都拿到正确路径（keep-alive 回归）",
                      r1.status_code == 200 and r2.status_code == 200
                      and r1.json().get("path") == "/plain" and r2.json().get("path") == "/plain",
                      f"{r1.status_code}/{r2.status_code} {r1.text[:60]} {r2.text[:60]}")
            finally:
                ka.close()
            pool.close()

            # ------------------------------- CLI 开箱即用（不写代码，直接当命令用）
            cli = ROOT / "client" / "proxypool_client.py"
            clean_env = {k: v for k, v in os.environ.items()
                         if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}

            def run_cli(*argv):
                return subprocess.run(
                    [sys.executable, "-u", str(cli), "--manager", manager_url, *argv],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=90, env=clean_env,
                )

            r = run_cli("pick")
            check("CLI pick 打印一个可用代理",
                  r.returncode == 0 and r.stdout.strip().startswith("http://"),
                  (r.stdout + r.stderr).strip()[:160])

            r = run_cli("env", "--shell", "bash")
            check("CLI env 打印可直接生效的代理环境变量",
                  r.returncode == 0 and "export HTTP_PROXY=http://" in r.stdout,
                  (r.stdout + r.stderr).strip()[:160])

            r = run_cli("list", "--count", "5")
            check("CLI list 列出池中的代理",
                  r.returncode == 0 and "node_id" in r.stdout,
                  (r.stdout + r.stderr).strip()[:160])

            r = run_cli("check", "--url", target_url)
            check("CLI check 经代理真的请求成功",
                  r.returncode == 0 and "200" in r.stdout,
                  (r.stdout + r.stderr).strip()[:200])
        except ImportError as exc:
            check("client 库验证（缺少 requests，跳过）", True, f"skipped: {exc}")
        except Exception as exc:
            check("client 库验证", False, repr(exc))

        # ------------------------------------------- report_bad 摘除与恢复
        print("\n[7] report_bad 摘除节点 / 心跳自动恢复")
        api("POST", f"{manager_url}/api/nodes/{node1_id}/report_bad", {"reason": "e2e-1"})
        status, resp = api("POST", f"{manager_url}/api/nodes/{node1_id}/report_bad", {"reason": "e2e-2"})
        check("连续两次上报后节点被判死", status == 200 and resp.get("alive") is False, str(resp))

        restored = wait_until(
            lambda: next((n for n in api("GET", f"{manager_url}/api/nodes")[1]["nodes"]
                          if n["node_id"] == node1_id and n["alive"]), None),
            timeout=20,
        )
        check("节点靠心跳/巡检自动恢复", bool(restored))

        print("\n[8] 注销流程")
        api("POST", f"{manager_url}/api/nodes/{node1_id}/unregister")
        status, _ = api("POST", f"{manager_url}/api/nodes/{node1_id}/heartbeat", {"active_conns": 0})
        check("注销后心跳返回 404", status == 404, f"status={status}")

    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        for handle in handles:
            handle.close()
        target.shutdown()

    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print(f"\n===== {passed}/{total} 通过 =====")
    if passed != total:
        dump_logs(logs)
        print("\n失败项：" + ", ".join(name for name, ok, _ in _results if not ok))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
