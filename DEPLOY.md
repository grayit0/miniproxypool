# 远端部署指南

面向「我有服务器，我要把它跑起来」的场景。全部命令可直接复制。
镜像已经在 GHCR 上构建好了（**public，不需要 docker login**）。

---

## 0. 先选拓扑 —— 这一步决定你能不能真的换 IP

| | 单机 | 多机 |
| --- | --- | --- |
| 机器数 | 1 | 1 台 manager + N 台 node |
| 出口 IP | **只有 1 个**（所有 node 共用这台机器的 IP） | 每台各自一个 |
| 能给你什么 | 连接级分散、并发上量、某个 node 挂了自动摘除 | 上面这些 **+ IP 轮换** |
| 什么时候用 | 只是想把请求从「直连」换成「经一台服务器出去」 | 需要分散风控 / 换出口 IP |

> **关键事实**：一个 node 做的事就是「在本机监听一个 HTTP 代理端口」，出口 IP 天然就是那台机器的 IP。
> 所以**在同一台机器上开 3 个 node，并不会得到 3 个出口 IP**。要换 IP，只能开多台机器。
> 如果你的目标是「同一台服务器上的请求分几个连接走」，单机就够；如果想被目标站看成不同来源，必须多机。

---

## 1. 前置：服务器上得有 Docker

```bash
docker --version || curl -fsSL https://get.docker.com | sh
docker compose version          # 可选，用 docker run 不需要它
docker pull ghcr.io/grayit0/miniproxypool-manager:latest
docker pull ghcr.io/grayit0/miniproxypool-node:latest
```

端口规划（先想清楚再填安全组）：

| 端口 | 谁开 | 给谁访问 |
| --- | --- | --- |
| `8000` | manager | node 机器（注册/心跳）+ 你的客户端（取代理列表） |
| `18801+` | node 代理端口 | **只给你的客户端** |
| node 的 `ADMIN_PORT`（默认 8000） | 不发布到宿主 | 只给容器内 HEALTHCHECK 用 |

---

## 2. 贯穿全文的一条规则（不搞清它必然踩坑）

node 注册时上报两个地址，含义完全不同，**别填混**：

| 上报字段 | 环境变量 | 谁用它 | 必须是什么 |
| --- | --- | --- | --- |
| 探测地址 | `NODE_HOST` + `PROXY_PORT` | **manager** 主动 TCP 探测存活 | manager **连得上**的地址 |
| 下发地址 | `EGRESS_HOST` + `EGRESS_PORT` | **你的客户端**拿到后去连 | 客户端**连得上**的地址 |

`EGRESS_PORT` 不设时自动跟随 `PROXY_PORT`，所以通常只要关心 `PROXY_PORT`。

- 同一个 docker 网络里 → `NODE_HOST` 填**容器名**，`PROXY_PORT` 填**容器内端口**（8888）。
- 跨机器 → `NODE_HOST` 填**公网 IP**，`PROXY_PORT` 必须等于**已发布到宿主的端口**。

**填错的症状**：容器 `docker ps` 里是 Up、日志也打了「节点就绪」，但 `/api/proxies` 里没有它 ——
因为 manager 每 15 秒探一次 `NODE_HOST:PROXY_PORT`，连续失败 2 次就摘掉了。

---

## 3. 单机部署（最快，3 条命令）

```bash
# 0) 本机公网 IP（EGRESS_HOST 要用它）
PUBIP=$(curl -fsS https://api.ipify.org)

# 1) 一个私有网络，manager 和 node 在里面互相看得见
docker network create proxypool

# 2) manager
docker run -d --name manager --network proxypool --restart unless-stopped \
  -p 8000:8000 -v proxypool-data:/data \
  ghcr.io/grayit0/miniproxypool-manager:latest

# 3) node1
docker run -d --name node1 --network proxypool --restart unless-stopped \
  -e MANAGER_URL=http://manager:8000 \
  -e NODE_NAME=node1 -e NODE_HOST=node1 -e PROXY_PORT=8888 \
  -e EGRESS_HOST="$PUBIP" -e EGRESS_PORT=18801 \
  -e REGION=cn \
  -p 18801:8888 \
  ghcr.io/grayit0/miniproxypool-node:latest
```

要点：`NODE_HOST=node1` + `PROXY_PORT=8888` 走的是**容器内网**，manager 探测不经过公网（避开云主机的
NAT 回环/hairpin 问题）；客户端拿到的则是 `EGRESS_HOST:EGRESS_PORT` = `公网IP:18801`。

想要 3 个 node 就在同一台机器上再开两个，只改三处：

```bash
docker run -d --name node2 --network proxypool --restart unless-stopped \
  -e MANAGER_URL=http://manager:8000 \
  -e NODE_NAME=node2 -e NODE_HOST=node2 -e PROXY_PORT=8888 \
  -e EGRESS_HOST="$PUBIP" -e EGRESS_PORT=18802 \
  -e REGION=cn \
  -p 18802:8888 \
  ghcr.io/grayit0/miniproxypool-node:latest

docker run -d --name node3 --network proxypool --restart unless-stopped \
  -e MANAGER_URL=http://manager:8000 \
  -e NODE_NAME=node3 -e NODE_HOST=node3 -e PROXY_PORT=8888 \
  -e EGRESS_HOST="$PUBIP" -e EGRESS_PORT=18803 \
  -e REGION=cn \
  -p 18803:8888 \
  ghcr.io/grayit0/miniproxypool-node:latest
```

验证（全部在服务器上跑）：

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/api/stats
curl -s "http://127.0.0.1:8000/api/proxies?count=5"
curl -x "http://$PUBIP:18801" -s https://api.ipify.org ; echo   # 应该回显 $PUBIP 同一个 IP
docker logs --tail 30 node1
```

---

## 4. 多机部署（真·多出口 IP）

**manager 机器**：

```bash
docker network create proxypool
docker run -d --name manager --network proxypool --restart unless-stopped \
  -p 8000:8000 -v proxypool-data:/data \
  ghcr.io/grayit0/miniproxypool-manager:latest
```

安全组：`8000` 只放给各台 node 机器的 IP。

**每台 node 机器**（把 `<MANAGER_IP>` 换成 manager 的公网 IP；`REGION` 随便起个能认出来的名字）：

```bash
MGIP=<MANAGER_IP>
PUBIP=$(curl -fsS https://api.ipify.org)

docker run -d --name node1 --restart unless-stopped \
  -e MANAGER_URL=http://$MGIP:8000 \
  -e NODE_NAME=$(hostname)-1 -e NODE_HOST="$PUBIP" -e PROXY_PORT=18801 \
  -e EGRESS_HOST="$PUBIP" \
  -e REGION=sg \
  -p 18801:18801 \
  ghcr.io/grayit0/miniproxypool-node:latest
```

这里 `PROXY_PORT=18801` 同时是**容器内监听端口**和**宿主映射端口**（`-p 18801:18801`），
`EGRESS_PORT` 没设所以自动跟着取 18801。这样 `NODE_HOST:PROXY_PORT` = `公网IP:18801`，
既在安全组放行范围内、又和客户端用的地址一致 —— 最不容易配错。

同一台机器想再开一个 node，改 `PROXY_PORT=18802` 和 `-p 18802:18802`：

```bash
docker run -d --name node2 --restart unless-stopped \
  -e MANAGER_URL=http://$MGIP:8000 \
  -e NODE_NAME=$(hostname)-2 -e NODE_HOST="$PUBIP" -e PROXY_PORT=18802 \
  -e EGRESS_HOST="$PUBIP" \
  -e REGION=sg \
  -p 18802:18802 \
  ghcr.io/grayit0/miniproxypool-node:latest
```

⚠️ **别照抄 `-p 18801:8888`**。那样宿主端口是 18801、容器内是 8888，
而 manager 探的是 `NODE_HOST:PROXY_PORT` = `公网IP:8888` —— 这个端口没发布，节点会被判死。

node 机器安全组：`18801`（以此类推）只放给你的客户端 IP。

在 manager 机器上确认池子：

```bash
curl -s http://127.0.0.1:8000/api/stats
curl -s "http://127.0.0.1:8000/api/proxies?count=20" | python3 -m json.tool
```

---

## 5. 安全：node 是**无认证的开放代理**，必须先关起来

node 不校验 `Proxy-Authorization`。**谁能连到 18801，谁就能用你这台服务器的 IP 出网**——
流量算在你头上。所以按重要性依次做：

```bash
# ① 只在安全组/防火墙上放行你的客户端 IP
#    ufw 例子（把 <客户端IP> 换掉）
ufw allow from <客户端IP> to any port 18801 proto tcp
ufw deny 18801

# ② manager 的 8000 只给 node 机器和你的客户端
ufw allow from <node机器IP> to any port 8000 proto tcp
```

如果**客户端 IP 不固定**（家用宽带、云函数、多出口），最省事的做法是根本别对公网暴露端口：
把发布端口**绑到宿主的回环地址**上，然后走 SSH 隧道。

网络层完全不用改，只把 `-p 18801:8888` 改成 `-p 127.0.0.1:18801:8888`：

```bash
docker run -d --name node1 --network proxypool --restart unless-stopped \
  -e MANAGER_URL=http://manager:8000 \
  -e NODE_NAME=node1 -e NODE_HOST=node1 -e PROXY_PORT=8888 \
  -e EGRESS_HOST=127.0.0.1 -e EGRESS_PORT=18801 \
  -p 127.0.0.1:18801:8888 \
  ghcr.io/grayit0/miniproxypool-node:latest
```

这样 18801 只能从这台服务器本机访问（安全组不用开），而 manager 依旧通过 docker 内网
`node1:8888` 探测它 —— 两件事互不干扰。客户端侧打隧道：

```bash
ssh -N -L 18801:127.0.0.1:18801 user@你的服务器
```

然后代码里照常写 `http://127.0.0.1:18801` 就行。隧道没建起来时这个端口是死的，
不存在「忘了改安全组结果变成公开开放代理」的风险。

另外：**别给 node 配 `UPSTREAMS`**。那个功能是「让本节点把流量转给别的代理」，你自己部署时用不上，
配错了会把出网路径绕到不确定的第三方上去。

---

## 6. 接到你的 requests.post 上

单机、一个固定节点的话，**只加一行**：

```python
PROXY = "http://<服务器公网IP>:18801"          # 多机就填你要用的那台

response = requests.post(
    'https://ssl.gongyi.qq.com/gygw-app/ed/finance/GetProjectFinanceListForC',
    params=params,
    cookies=cookies,
    headers=headers,
    json=json_data,
    timeout=4,
    proxies={"http": PROXY, "https": PROXY},   # ← 只加这一行
)
```

### 这一行里唯一容易错的地方

`proxies` 字典里 **`http` 和 `https` 两个键都要给**。目标 URL 是 `https://`，requests 只会去取
`https` 键；**只写 `http` 键不会报错，而是静默直连**，你的流量根本没进代理。

我实测过这个陷阱（探针把 http 键指向死端口 `127.0.0.1:9`）：

| 写法 | 结果 | node 的连接计数 |
| --- | --- | --- |
| `{"http": "http://127.0.0.1:9"}` | **HTTP 200**（成功，但没走代理） | 不增加 |
| `{"https": "http://127.0.0.1:9"}` | `ProxyError`，连不上 | — |
| `{"http": u, "https": u}` | HTTP 200 | +1（真的走了代理） |

### 想用池子（自动取代理 + 失败换节点）

直接用仓库里的 `examples/requests_embed.py`，它只依赖 `requests`，参数和你原来的调用一一对应：

```python
from requests_embed import post_finance_list

response = post_finance_list(
    params=params,
    cookies=cookies,
    headers=headers,
    json_data=json_data,
    timeout=4,                                    # 和原来一样
    manager="http://<服务器公网IP>:8000",          # 从池子里取
    sticky=True,                                  # 带 cookie 的接口用这个
)
```

自检（会真发一次请求，并打印用了哪个节点）：

```bash
python examples/requests_embed.py --proxy   http://<服务器IP>:18801
python examples/requests_embed.py --manager http://<服务器IP>:8000
```

### 两个只在真用起来之后才会暴露的问题

**① `timeout=4` 经代理后偏紧。** 经代理的第一个请求要多付一次 CONNECT + TLS 握手的开销
（我本机实测这个接口经代理 197~308 ms）。同城同运营商没问题；如果节点在境外、目标在境内，
往返会放大到秒级。建议改成元组把「连接」和「读取」分开：

```python
timeout=(4, 8),    # 连接阶段 4 秒，读取阶段 8 秒
```

**② 带 cookie / 登录态的接口不要每请求换 IP。** 同一份 cookie 中途换出口 IP，很容易触发目标站风控，
表现是莫名返回空列表或校验失败。这类接口用 `sticky=True`（固定一个节点，失败才换）；
只有在接口完全无状态、纯粹为了分散压力时才用每请求轮换。

---

## 7. 排错速查

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| `docker ps` 里 node 是 Up，但 `/api/proxies` 是空的 | manager 探不到 `NODE_HOST:PROXY_PORT` | 见第 2 节。多机部署就查这个端口有没有真的发布出去 |
| `/api/proxies` 一开始空、过十几秒才出现 | manager 巡检周期 15 秒 | 等一会儿，或看 `docker logs manager` |
| 客户端 `curl -x` 超时 | 安全组没放行 18801 | 放行你的客户端 IP |
| 加了 `proxies` 出口 IP 还是本机 | 只给了 `http` 键 | 补上 `https` 键 |
| 一直 `ProxyError` / `Connection refused` | 节点容器挂了、或端口没映射 | `docker ps`、`docker logs node1`、`ss -lntp \| grep 1880` |
| node 日志「未完成注册，退出」 | `MANAGER_URL` 不通（安全组/DNS） | `docker exec node1 wget -qO- http://<manager>:8000/health` |
| 想换镜像版本 | `IMAGE_TAG` | 每次 push 会同时打 `sha-<短sha>` 标签，`docker run` 时把 `:latest` 换成 `:sha-xxxxxxx` |

---

## 8. 附：用 compose 文件也行

仓库里的 `docker-compose.ghcr.yml` 就是第 3 节三个 `docker run` 的等价写法（单机、3 个 node）：

```bash
git clone https://github.com/grayit0/miniproxypool.git
cd miniproxypool
cp .env.example .env
# 编辑 .env：把 NODE1_EGRESS_HOST / NODE2_EGRESS_HOST / NODE3_EGRESS_HOST 改成公网 IP
docker compose -f docker-compose.ghcr.yml up -d
```

注意 compose 文件里用的是 `NODE_HOST=node1` + `PROXY_PORT=8888` + `-p 18801:8888` 的**容器内网探测**写法，
和第 2 节的规则一致，**不能把它的 `-p` 端口和 `PROXY_PORT` 各改一半**。
