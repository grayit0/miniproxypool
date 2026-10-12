# miniproxypool

一个可以「注册—分发—轮换」的最小代理池：

- **manager**（Docker，FastAPI + SQLite）—— 节点注册中心 + 代理分发 API，同时维护一份共享上游池；
- **node**（Docker，Go，零三方依赖）—— HTTP/CONNECT 代理，自动向 manager 注册、心跳、并在心跳里领取共享上游；
- **client**（Python，单文件）—— 从 manager 取代理，开箱即用；失败自动换节点并回报坏节点。

```
                    ┌──────────────────────────────────────────┐
                    │              manager (Docker)            │
  注册 / 心跳 ─────▶ │  POST /api/nodes/register                │
  ◀───── 共享上游    │  POST /api/nodes/{id}/heartbeat          │
                    │  POST /api/nodes/{id}/unregister         │
                    │  GET  /api/proxies?count=N    ◀──────┐   │
                    │  GET  /api/proxy                      │   │
                    │  POST /api/nodes/{id}/report_bad ◀──┐ │   │
                    │  SQLite: nodes / upstreams          │ │   │
                    └──────────┬───────────────────────────┼─┼───┘
                               │ TCP 探活 (host:port)      │ │
        ┌──────────────────────┼──────────────────────┐    │ │
        ▼                      ▼                      ▼    │ │
  ┌───────────┐         ┌───────────┐          ┌───────────┐│ │
  │  node1    │         │  node2    │          │  node3    ││ │
  │ Go 代理   │◀─上游链─│ Go 代理   │          │ Go 代理   ││ │
  └─────┬─────┘         └─────┬─────┘          └─────┬─────┘│ │
        │ :18801              │ :18802               │ :18803 │
        └─────────────────────┴──────────────────────┘        │
                              │                               │
                        ┌─────▼──────┐      取代理 / 上报      │
                        │  client    │────────────────────────┘
                        │ (Python)   │
                        └────────────┘
```

## 快速开始

### 前提：让 GitHub Actions 能跑起来（一次性）

`.github/workflows/` 下的文件只能由带 `workflow` scope 的凭证推送。如果 push 时报：

```
refusing to allow a Personal Access Token to create or update workflow
`.github/workflows/build-images.yml` without `workflow` scope
```

或者 Contents / Git Data API 对 `.github/workflows/` 直接返回 404，那就是 scope 不够。
补上（二选一）：

- **classic PAT**：Settings → Developer settings → Personal access tokens → Tokens (classic)
  → 编辑现有 token → 勾上 `workflow` → Update。
- **fine-grained PAT**：新建 token → Repository permissions → **Workflows = Read and write**。

拿到新 token 后一条命令补推这三个文件：

```powershell
.\scripts\push_workflows.ps1 -Token <带 workflow scope 的 token>
```

不想折腾 scope 也行 —— 直接看**方式 D**，那条路完全不依赖 Actions。

### 方式 A：直接用 GitHub 上构建好的镜像（推荐）

镜像由 `.github/workflows/build-images.yml` 在每次 push 到 `main` 时自动构建并推送到 GHCR。
GitHub Packages 的镜像默认是私有的，想公开访问就去
`https://github.com/users/grayit0/packages/container/<包名>/settings` 把可见性改成 Public。

```bash
git clone https://github.com/grayit0/miniproxypool.git
cd miniproxypool
cp .env.example .env          # 改 IMAGE_TAG / EGRESS_HOST（公网机器要填公网地址）
docker compose -f docker-compose.ghcr.yml up -d
curl http://127.0.0.1:8000/api/stats
```

### 方式 B：本地从源码构建（需要有 Go 和 Docker）

```bash
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8000/api/stats
python client/proxypool_client.py list
```

### 方式 C：让 GitHub 的 runner 帮你托管 manager

`.github/workflows/run-pool.yml` 会在 runner 上把整套栈跑起来，并用 Cloudflare
快速隧道把 manager 暴露成一个临时公网地址（地址会写在 job 的 Summary 里）。
适合「我只想把自己的机器/服务器上的节点注册进去，不想自己维护 manager」。

**这条路的边界要说清楚**：GitHub runner 不是常驻主机，单个 job 最长 6 小时，
job 结束即清零；隧道地址每次运行都会变；runner 上的 node 没有公网入口，
真正对外服务的是你自己注册进去的节点。想做到「几乎常驻」需要把工作流里
`schedule` 那两行取消注释（默认注释掉了），请自行评估是否符合你所在环境的使用规则。

### 方式 D：用 GitHub Codespaces（不需要 workflow scope）

仓库里带了 `.devcontainer/devcontainer.json`：在 GitHub 页面上点
**Code → Codespaces → Create codespace on main**，容器起来后会自动执行
`scripts/codespace_up.sh`，把 manager + 3 个 Go node 直接跑起来（Codespace 自带 Docker）。

然后在 **PORTS** 面板把 `8000` 的 Visibility 改成 **Public**，
就得到一个形如 `https://<codespace-name>-8000.app.github.dev` 的 manager 地址：

```bash
export PROXYPOOL_MANAGER=https://<codespace-name>-8000.app.github.dev
python client/proxypool_client.py check
python client/proxypool_client.py env
```

想顺便把镜像发到 GHCR（用 Codespaces 的容器算力当构建机），执行：

```bash
export GHCR_TOKEN=<带 write:packages 的 PAT>
bash scripts/push_ghcr.sh
```

> 别忘了：Codespace 里的 node 只能在 Codespace 内部访问。要让外部客户端真正走代理，
> 需要把你自己那台有公网入口的机器上的 node 注册到这个 manager
> （`MANAGER_URL` 填上面那个公开地址，`EGRESS_HOST` 填那台机器的公网 IP/域名）。
> Codespace 停掉之后一切清零。

## 客户端：开箱即用

```bash
pip install requests        # 唯一依赖
export PROXYPOOL_MANAGER=http://127.0.0.1:8000    # 或者用 --manager 参数
```

三种用法，从最简到最灵活：

```python
# 1) 一行式：拿到就能用
from proxypool_client import session
with session() as s:
    print(s.get("https://example.com").status_code)   # 自动选节点、失败自动换

# 2) 只要地址，自己拿去用（requests / curl / 环境变量都行）
from proxypool_client import ProxyPool
pool = ProxyPool()
print(pool.proxy_url())                     # http://1.2.3.4:18801
requests.get("https://example.com", proxies=pool.get_proxy().proxies)

# 3) 批量拿一批，做自己的调度
for p in pool.list_proxies(count=10, region="cn"):
    print(p.node_id, p.url, p.latency_ms)
```

也可以完全不写代码，当命令行工具用（`client/proxypool_client.py` 本身就能跑）：

```bash
python client/proxypool_client.py list --count 5     # 列出可用代理
python client/proxypool_client.py pick               # 打印一个可用代理地址
eval $(python client/proxypool_client.py env)        # 直接给当前 shell 设好代理
python client/proxypool_client.py env --shell powershell
python client/proxypool_client.py check              # 走池子发一次请求，验证真能出网
python client/proxypool_client.py bench --n 3        # 逐个节点测延迟
python client/proxypool_client.py run -- curl -s https://httpbin.org/ip   # 用池里的代理跑命令
python client/proxypool_client.py watch              # 池子状态看板
```

装成命令：`pip install ./client` 之后可以直接用 `proxypool list` / `proxypool check`。

`RotatingSession` 的行为：每次失败换一个节点，并把坏节点通过 `report_bad`
上报给 manager（连续失败到阈值就摘掉），重试次数用尽才抛 `ProxyPoolError`。
可以传 `retry_status=(502, 503, 504)` 让 5xx 也触发换节点。

## 组件细节

### manager

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `./data/proxypool.db` | SQLite 路径（容器里是 `/data/proxypool.db`） |
| `PORT` / `HOST` | `8000` / `0.0.0.0` | 监听地址 |
| `HEARTBEAT_INTERVAL` | `10` | 下发给节点的心跳周期（秒） |
| `NODE_TTL` | `45` | 超过这么久没心跳视为掉线（秒） |
| `FAIL_THRESHOLD` | `2` | 连续探测/上报失败几次判定下线 |
| `CHECK_INTERVAL` | `15` | 巡检周期（秒） |
| `CHECK_TIMEOUT` | `4` | 单次探测超时（秒） |
| `CHECK_URL` | `http://httpbin.org/ip` | 校验上游代理出口的目标地址 |
| `CHECK_UPSTREAMS` | `1` | 是否主动校验上游代理 |
| `ADMIN_TOKEN` | 空 | 非空时 `/api/nodes`、`/api/upstreams` 需要 `X-Admin-Token` 头 |

### node（Go 版）

环境变量和 Python 版完全一致，所以换镜像是无痛的：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MANAGER_URL` | `http://manager:8000` | manager 地址 |
| `NODE_HOST` | 容器主机名 | **manager 用来探活的地址**（同 docker 网络里可达） |
| `PROXY_PORT` | `8888` | 容器内代理监听端口 |
| `EGRESS_HOST` | `127.0.0.1` | **客户端该用什么地址访问**（通常是公网 IP / 域名） |
| `EGRESS_PORT` | 同 `PROXY_PORT` | 客户端该用什么端口访问（通常是宿主机映射端口） |
| `ADMIN_PORT` | `8000` | `/ping /stats /upstreams /config` 自省端口 |
| `REGION` | `local` | 打标，可按 region 取代理 |
| `TAGS` | 空 | 逗号分隔的标签 |
| `UPSTREAMS` | 空 | 本节点自己的上游链（逗号分隔） |
| `MAX_CONNS` | `512` | 最大并发连接 |
| `CONNECT_TIMEOUT` | `10` | 建连超时（秒） |
| `HEARTBEAT_INTERVAL` | `10` | 初始心跳周期（注册后以 manager 下发的为准） |
| `NODE_ID` | 空 | 固定 id，容器重启后保持同一身份 |

> `NODE_HOST` 和 `EGRESS_HOST` 是两件事，填混了会得到「节点显示在线但客户端连不上」。
> 前者是 manager 在 docker 网络内部探活用的（容器名），后者是给客户端用的（宿主/公网地址）。

命令行参数和 `--healthcheck` 自检模式见 `proxypool-node --help`。

实现了什么：

- `CONNECT host:port` 隧道（HTTPS）；
- 绝对 URI 形式的明文 HTTP（`GET http://a.b/c`），转发前改写成源服务器形式并丢掉逐跳头；
- 相对路径 + `Host` 头；
- 上游链（轮询 + 连续失败熔断，从 manager 心跳里领取，也支持本地 `UPSTREAMS`）；
- 连接数上限、字节数/连接数统计、优雅退出时自动注销。

代理转发有两个特别容易踩的点，两个实现都处理了：

1. **明文 HTTP 强制「一个连接一个请求」**：转发时注入 `Connection: close`，并丢掉 `Connection`
   这类逐跳头。明文代理只改写了第一个请求的请求行；一旦客户端复用 keep-alive 连接，
   第二个请求的请求行仍然是绝对 URI 形式，透传给源站会被当成路径，直接 400/404。
   HTTPS 走 CONNECT 隧道，是端到端加密的裸管道，keep-alive 不受影响。
2. **上游列表里过滤掉自己**：共享上游池是全局的，很可能包含节点自己。不过滤就会变成
   「CONNECT 给自己 → 又 CONNECT 给自己」的无限递归，最后整个节点超时不可用。
   节点启动时会把 `NODE_HOST:PROXY_PORT`、`EGRESS_HOST:EGRESS_PORT`、`127.0.0.1:PROXY_PORT`、
   `localhost:PROXY_PORT` 登记为自己的地址，命中就忽略（日志里会写明是哪条）。

唯一的实现差异：上游只支持 `http://`（https 上游要 TLS 隧道、socks5 要额外握手，故意没做，
保持代码好读）。

`node/` 目录里保留了原来的 Python 实现（纯标准库 asyncio），作为参考实现和对照，
compose 里默认不再使用它。

### client

- `ProxyPool` —— manager 的瘦客户端，管理面流量默认 `trust_env=False`，
  不会因为你机器上设了 `HTTP_PROXY` 就把「取代理列表」这个动作本身代理掉；
- `RotatingSession` —— 自动轮换 + 上报坏节点；
- `session()` / CLI —— 开箱即用入口。

## API 一览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/nodes/register` | 节点注册（返回 `node_id`、心跳周期、共享上游列表） |
| POST | `/api/nodes/{id}/heartbeat` | 心跳（顺带领最新上游列表） |
| POST | `/api/nodes/{id}/unregister` | 节点注销 |
| POST | `/api/nodes/{id}/report_bad` | 客户端上报坏节点 |
| GET | `/api/proxies?count=10&region=&scheme=` | 批量取可用代理（按探测延迟排序） |
| GET | `/api/proxy?region=` | 随机取一个可用代理 |
| GET | `/api/nodes` | 全部节点（需 `ADMIN_TOKEN`） |
| GET | `/api/stats` | 池子统计 |
| GET / POST / DELETE | `/api/upstreams` | 共享上游池读写 |
| POST | `/api/nodes/cleanup?older_than=` | 清理长期掉线的节点 |
| GET | `/health` | 健康检查 |

交互式文档：`http://127.0.0.1:8000/docs`

## 验证

本机没 Docker 也能跑端到端测试（manager + 两个 node + 回显服务全都拉起来）：

```bash
pip install -r manager/requirements.txt -r client/requirements.txt
cd node-go && go build -o proxypool-node . && cd ..
python tests/e2e_local.py                      # 有 Go 二进制就自动用 Go 节点
E2E_NODE_ENGINE=python python tests/e2e_local.py   # 强制用 Python 节点
```

覆盖：manager 就绪、节点注册、心跳、`/api/proxies` 下发对外地址、节点把自己从上游里过滤掉、
明文 HTTP 转发（含请求行改写）、CONNECT 隧道、经共享上游的链式转发（并核对中转节点的连接计数
真的增长了）、client 库（含同一连接连续两次请求的 keep-alive 回归用例）、CLI 四个子命令、
`report_bad` 摘除与心跳自动恢复、注销。当前 **24/24 通过**，Go 与 Python 两种节点实现都跑一遍。

CI 里跑的东西：

| 工作流 | 做什么 |
| --- | --- |
| `e2e-go-node.yml` | 编译 Go 节点 + `go vet` + 跑上面的端到端测试 |
| `build-images.yml` | 构建并推送 manager/node 镜像到 GHCR；非 PR 事件还会把镜像真跑起来，用客户端经代理访问外网做冒烟 |
| `run-pool.yml` | 在 runner 上托管 manager + 临时公网隧道（手动触发为主） |

> ⚠️ 这三个工作流目前**还没推到 GitHub**（推送所用的 token 缺 `workflow` scope），
> 所以 Actions 页面上还看不到它们、GHCR 里也还没有镜像。
> 补推方式见上面「前提：让 GitHub Actions 能跑起来」。
> 在补推之前，请用**方式 B**（本地构建）或**方式 D**（Codespaces）。

## 注意

- **node 是开放代理**：谁能连到 `18801` 就能拿它出网。生产环境请加出口白名单、
  在节点前面套 `Proxy-Authorization` 认证，并给管理接口设 `ADMIN_TOKEN`。
- `/api/nodes`、`/api/upstreams` 之类的接口不设 `ADMIN_TOKEN` 时是完全公开的。
- manager 的 SQLite 挂在 `manager-data` 卷里，`docker compose down -v` 会连数据一起删。
- **不要把池子里的节点塞进共享上游池**。共享上游是给「外部代理」用的；把池内节点塞进去，
  两个节点可能互相把对方当上游而形成环路（指向自己的那一份会被自动过滤，但互相指向不会）。
  端到端测试里那么做只是为了演示链式转发。
- `CHECK_URL` 默认是 `httpbin.org`，国内网络建议换成你能稳定访问的目标，
  否则上游校验会一直失败。

## 目录结构

```
.
├── manager/                # FastAPI 注册中心（Docker）
│   ├── app/{config,schemas,store,main}.py
│   ├── Dockerfile
│   └── requirements.txt
├── node-go/                # Go 代理节点（Docker，零三方依赖）
│   ├── {main,proxy,registrar,admin}.go
│   ├── go.mod
│   └── Dockerfile          # 多阶段构建，最终镜像 ~15MB
├── node/                   # 原来的 Python 节点（纯标准库，留作参考）
├── client/
│   ├── proxypool_client.py # 单文件客户端 + CLI
│   ├── pyproject.toml      # pip install ./client 后可用 proxypool 命令
│   └── demo.py
├── tests/
│   ├── e2e_local.py        # 本机端到端（不开 Docker）
│   └── ci_smoke.py         # CI 冒烟：经代理访问外网
├── scripts/
│   ├── add_nodes.ps1       # 批量加节点容器（Windows）
│   ├── codespace_up.sh     # Codespaces / 任意带 Docker 的机器上一键起全家桶
│   ├── push_ghcr.sh        # 在容器里构建并推送镜像到 GHCR
│   └── push_workflows.ps1  # 用带 workflow scope 的 token 补推 .github/workflows/
├── .devcontainer/          # Codespaces：自带 Docker，起来就能跑
├── docker-compose.yml      # 本地从源码构建
├── docker-compose.ghcr.yml # 直接使用 GHCR 镜像
└── .github/workflows/      # build-images / e2e-go-node / run-pool
```

MIT License.
