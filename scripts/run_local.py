#!/usr/bin/env python3
"""在一台机器上把整套 proxypool 跑起来，**不需要 Docker**。

做的事：
  manager（uvicorn，FastAPI）+ N 个 node（Python 参考实现，纯标准库）全部作为
  独立后台进程拉起，日志落到 .localrun/logs/，PID 记到 .localrun/pids.json，
  然后等 manager 健康、等节点被探活，最后打印池子状态和客户端用法。

用法：
  python scripts/run_local.py start                  # 起 manager + 3 个节点
  python scripts/run_local.py start -n 5             # 起 5 个
  python scripts/run_local.py start --manager-port 9000 --start-port 19001
  python scripts/run_local.py status                 # 看进程活着没 + 池子状态
  python scripts/run_local.py logs node1 -f          # 看某个进程的日志
  python scripts/run_local.py stop                   # 全部停掉

为什么不用 Docker：本机可能没装 Docker / Go，而 manager 和 Python 节点加起来
只需要 fastapi + uvicorn（+ 客户端要 requests）。想跑 Go 节点或容器编排请用
docker compose；这个脚本是为了「一条命令就能看见它能跑」。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / ".localrun"
LOG_DIR = RUN_DIR / "logs"
PID_FILE = RUN_DIR / "pids.json"

# 管理面请求一律绕开环境里的代理（否则 HTTP_PROXY 会把「取列表」本身也代理掉，
# 而且本机 keep-alive 复用连接时会被改写请求行，表现为第一次成功、第二次 404）。
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_NOISY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "all_proxy")


# --------------------------------------------------------------------- 小工具
def say(msg: str = "") -> None:
    print(msg, flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_busy(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def http_get(url: str, timeout: float = 3.0):
    with _DIRECT.open(url, timeout=timeout) as resp:
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else {})


def wait_for(fn, timeout: float, interval: float = 0.4):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = fn()
            if last:
                return last
        except Exception as exc:
            last = exc
        time.sleep(interval)
    return None


def child_env(extra: dict) -> dict:
    env = dict(os.environ)
    env.update({k: str(v) for k, v in extra.items()})
    env["PYTHONUNBUFFERED"] = "1"
    for name in _NOISY_ENV:
        env.pop(name, None)
    return env


def spawn(name: str, cwd: Path, argv: list, extra_env: dict):
    """把子进程拉起并把 stdout/stderr 落盘，返回 (pid, log_path)。

    刻意用 subprocess 而不是 PowerShell 的 Start-Process：后者在本机的
    -RedirectStandardOutput 拿不到任何输出（文件是空的），排查时等于瞎。
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{name}.log"
    handle = open(log_path, "ab", buffering=0)
    handle.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 {name} =====\n".encode())

    kwargs = {}
    if os.name == "nt":
        # 脱离当前控制台/进程组，父进程退出后继续活着
        kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                   | getattr(subprocess, "DETACHED_PROCESS", 0))
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(argv, cwd=str(cwd), env=child_env(extra_env),
                            stdout=handle, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, **kwargs)
    handle.close()  # 子进程自己持有这个句柄
    return proc.pid, log_path


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, errors="replace")
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_pid(pid: int) -> None:
    if not pid_alive(pid):
        return
    if os.name == "nt":
        # /T 连子孙一起收掉，避免留下孤儿 python
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True, errors="replace")
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass


def load_pids() -> dict:
    if not PID_FILE.exists():
        return {}
    try:
        return json.loads(PID_FILE.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def save_pids(data: dict) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def resolve_python(explicit: str | None) -> str:
    """找出一个装了 fastapi/uvicorn 的解释器。"""
    cands: list[str] = []
    if explicit:
        cands.append(explicit)
    if os.getenv("PROXYPOOL_PYTHON"):
        cands.append(os.environ["PROXYPOOL_PYTHON"])
    cands.append(str(ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")))
    cands.append(sys.executable)
    from shutil import which
    for name in ("python3", "python"):
        found = which(name)
        if found:
            cands.append(found)

    for cand in cands:
        if not cand or not Path(cand).exists():
            continue
        probe = subprocess.run([cand, "-c", "import fastapi, uvicorn, requests"],
                               capture_output=True, text=True)
        if probe.returncode == 0:
            return cand
    return ""


def make_venv() -> str:
    """造一个 .venv 并装依赖（只在这个脚本判断出「没有可用解释器」时被调用）。"""
    venv_py = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not venv_py.exists():
        say("没找到带 fastapi/uvicorn/requests 的解释器，正在创建 .venv 并安装依赖……")
        base = sys.executable or "python"
        r = subprocess.run([base, "-m", "venv", str(ROOT / ".venv")],
                           capture_output=True, text=True)
        if r.returncode != 0:
            say("创建 venv 失败：" + (r.stderr or r.stdout).strip()[:400])
            return ""
    req = [str(ROOT / "manager" / "requirements.txt"), str(ROOT / "client" / "requirements.txt")]
    r = subprocess.run([str(venv_py), "-m", "pip", "install", "-q", *req],
                       capture_output=True, text=True)
    if r.returncode != 0:
        say("安装依赖失败：" + (r.stderr or r.stdout).strip()[-600:])
        return ""
    return str(venv_py)


def manager_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def show_pool(port: int) -> None:
    try:
        _, stats = http_get(f"{manager_url(port)}/api/stats")
    except Exception as exc:
        say(f"读 /api/stats 失败：{exc!r}")
        return
    nodes = stats.get("nodes", {})
    say(f"池子状态：总节点 {nodes.get('total', '?')} / 在线 {nodes.get('alive', '?')} / "
        f"死了 {nodes.get('dead', '?')} | 上游 {stats.get('upstreams', '?')}")
    try:
        _, body = http_get(f"{manager_url(port)}/api/proxies?count=20")
        for p in body.get("proxies", []):
            say(f"   {p.get('url'):<32} node={p.get('node_id', '')[:12]} "
                f"region={p.get('region')} 延迟={p.get('latency_ms', '-')}ms")
    except Exception as exc:
        say(f"读 /api/proxies 失败：{exc!r}")


def print_usage(port: int) -> None:
    mgr = manager_url(port)
    say("")
    say("接下来这样用（另开一个终端）：")
    say("")
    say(f"    python client/proxypool_client.py --manager {mgr} check")
    say(f"    python client/proxypool_client.py --manager {mgr} pick")
    say(f"    python client/proxypool_client.py --manager {mgr} watch")
    say("")
    say("    # 让当前 shell 的所有命令都走池子")
    say(f"    export PROXYPOOL_MANAGER={mgr}     # PowerShell: $env:PROXYPOOL_MANAGER=\"...\"")
    say("    eval $(python client/proxypool_client.py env)      # PowerShell 见 env --shell powershell")
    say("")
    say("    # 写代码")
    say("    from proxypool_client import session")
    say("    with session() as s: print(s.get('https://example.com').status_code)")
    say("")
    say(f"    manager 交互式文档：{mgr}/docs")


# ------------------------------------------------------------------ 子命令实现
def cmd_start(args) -> int:
    existing = load_pids()
    if existing:
        live = {name: pid for name, pid in existing.items() if pid_alive(pid)}
        if live:
            say("已经有一批进程在跑了：")
            for name, pid in live.items():
                say(f"   {name}  pid={pid}")
            say("先 `stop`，或者用 `status` 看看。")
            return 1
        # 上次留下的 pid 记录，进程其实早就没了（崩溃 / 被外部收掉）——清掉继续
        say("发现上次遗留的 pid 记录（进程都已退出），清理后继续。")
        PID_FILE.unlink(missing_ok=True)

    py = resolve_python(args.python)
    if not py:
        py = make_venv()
    if not py:
        say("")
        say("找不到可用的 Python 解释器。手动装一下依赖再重试：")
        say("    python -m pip install -r manager/requirements.txt -r client/requirements.txt")
        say("然后用 -Python 指定解释器，或设环境变量 PROXYPOOL_PYTHON。")
        return 1
    say(f"解释器：{py}")

    manager_port = args.manager_port
    if port_busy(manager_port):
        say(f"端口 {manager_port} 已被占用，换一个：--manager-port <port>")
        return 1

    proxy_ports = [args.start_port + i for i in range(args.nodes)]
    admin_ports = [args.start_port + 100 + i for i in range(args.nodes)]
    for p in proxy_ports + admin_ports:
        if port_busy(p):
            say(f"端口 {p} 已被占用，换一个起始端口：--start-port <port>")
            return 1

    (ROOT / "manager" / "data").mkdir(parents=True, exist_ok=True)

    pids: dict = {}
    try:
        # ------------------------------------------------------------ manager
        say(f"启动 manager（端口 {manager_port}）……")
        pid, _ = spawn(
            "manager", ROOT / "manager",
            [py, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(manager_port), "--no-access-log"],
            {
                "DB_PATH": "./data/localrun.db",
                "HEARTBEAT_INTERVAL": 10,
                "NODE_TTL": 45,
                "FAIL_THRESHOLD": 2,
                "CHECK_INTERVAL": 15,
                "CHECK_TIMEOUT": 4,
                # 本地不挂上游，关掉校验，省得因为访问不了 httpbin 一直报错
                "CHECK_UPSTREAMS": 0,
                "LOG_LEVEL": "INFO",
            },
        )
        pids["manager"] = pid

        ok = wait_for(lambda: http_get(f"{manager_url(manager_port)}/health")[0] == 200,
                      timeout=args.timeout)
        if not ok:
            say("manager 没起来。日志：")
            say((LOG_DIR / "manager.log").read_text(encoding="utf-8", errors="replace")[-2000:])
            return 1
        say(f"manager 就绪：{manager_url(manager_port)}")

        # -------------------------------------------------------------- nodes
        for i, (pport, aport) in enumerate(zip(proxy_ports, admin_ports), start=1):
            name = f"node{i}"
            say(f"启动 {name}（代理端口 {pport}，自省端口 {aport}）……")
            pid, _ = spawn(
                name, ROOT / "node",
                [py, "-u", "entrypoint.py"],
                {
                    "MANAGER_URL": manager_url(manager_port),
                    "NODE_HOST": "127.0.0.1",
                    "NODE_NAME": name,
                    "NODE_ID": name,          # 固定身份，重启不换号
                    "PROXY_PORT": pport,
                    "ADMIN_PORT": aport,
                    "EGRESS_HOST": "127.0.0.1",   # 客户端该用哪个地址连
                    "EGRESS_PORT": pport,
                    "REGION": args.region,
                    "LOG_LEVEL": "INFO",
                },
            )
            pids[name] = pid

        save_pids(pids)

        alive = wait_for(
            lambda: (lambda s: s[1].get("nodes", {}).get("alive") or None)(
                http_get(f"{manager_url(manager_port)}/api/stats")),
            timeout=args.timeout,
        )
        if not alive:
            say("节点起来了但 manager 一直没探到在线节点，检查日志：")
            say(str(LOG_DIR))
            return 1

        say("")
        show_pool(manager_port)
        print_usage(manager_port)
        say("")
        say(f"日志目录：{LOG_DIR}")
        say("停掉：python scripts/run_local.py stop")
        return 0
    except Exception as exc:
        say(f"启动失败：{exc!r}")
        return 1


def cmd_stop(args) -> int:
    pids = load_pids()
    if not pids:
        say("没有记录的进程。")
        return 0
    for name, pid in pids.items():
        if pid_alive(pid):
            kill_pid(pid)
            say(f"已停止 {name}（pid {pid}）")
        else:
            say(f"{name}（pid {pid}）已经不在")
    PID_FILE.unlink(missing_ok=True)
    return 0


def cmd_status(args) -> int:
    pids = load_pids()
    if not pids:
        say("没有记录的进程（还没跑过，或者已经 stop 了）。")
        return 1
    for name, pid in pids.items():
        say(f"{'运行中' if pid_alive(pid) else '已退出'}  {name:<8} pid={pid}")
    say("")
    show_pool(args.manager_port)
    return 0


def cmd_logs(args) -> int:
    path = LOG_DIR / f"{args.name}.log"
    if not path.exists():
        say(f"没有这个日志：{path}")
        say("可用：" + ", ".join(sorted(p.stem for p in LOG_DIR.glob("*.log"))) or "（空）")
        return 1
    if args.follow:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(0, os.SEEK_END)
            try:
                while True:
                    line = fh.readline()
                    if line:
                        sys.stdout.write(line)
                        sys.stdout.flush()
                    else:
                        time.sleep(0.4)
            except KeyboardInterrupt:
                pass
        return 0
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(text[-args.lines:]))
    return 0


# ---------------------------------------------------------------------- 入口
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_local.py",
        description="本机免 Docker 跑 proxypool（manager + N 个 Python 节点）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("start", help="拉起 manager + 节点（默认）")
    s.add_argument("-n", "--nodes", type=int, default=3, help="节点数量（默认 3）")
    s.add_argument("--manager-port", type=int, default=8000, help="manager 端口（默认 8000）")
    s.add_argument("--start-port", type=int, default=18801,
                   help="第一个节点的代理端口（默认 18801，依次 +1；自省端口 = +100）")
    s.add_argument("--region", default="local", help="节点 region 标签")
    s.add_argument("--python", default=os.getenv("PROXYPOOL_PYTHON", ""),
                   help="指定解释器路径（需要已装 fastapi/uvicorn/requests）")
    s.add_argument("--timeout", type=float, default=40.0, help="等待就绪的秒数")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("stop", help="停掉全部进程")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("status", help="看进程与池子状态")
    s.add_argument("--manager-port", type=int, default=8000)
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("logs", help="看某个进程的日志")
    s.add_argument("name", help="manager / node1 / node2 ...")
    s.add_argument("-f", "--follow", action="store_true", help="持续跟随")
    s.add_argument("--lines", type=int, default=60, help="打印最后 N 行（默认 60）")
    s.set_defaults(func=cmd_logs)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["start"]
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
