package main

// ProxyPool Node（Go 版）入口。
//
// 启动顺序：
//  1. 起本地代理服务（对客户端）；
//  2. 向 manager 注册，带上「manager 用来探测的地址」和「客户端用来访问的地址」；
//  3. 起心跳任务，顺便从心跳响应同步共享上游代理；
//  4. 起管理端口用于排查。
//
// 配置既支持命令行参数，也支持环境变量（Docker 里用环境变量），
// 环境变量名与 Python 版完全一致，可以直接替换容器镜像。

import (
	"context"
	"fmt"
	"log"
	"net"
	"os"
	"os/signal"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const (
	version    = "2.0.0"
	versionTag = "2.0.0"
)

type config struct {
	managerURL     string
	host           string
	proxyHost      string
	proxyPort      int
	egressHost     string
	egressPort     int
	adminPort      int
	region         string
	tags           []string
	upstreams      []string
	maxConns       int
	connectTimeout time.Duration
	heartbeat      time.Duration
	nodeID         string
	name           string
}

// ------------------------------------------------------------------ 环境变量

func envStr(name, def string) string {
	if v := strings.TrimSpace(os.Getenv(name)); v != "" {
		return v
	}
	return def
}

func envInt(name string, def int) int {
	if v := strings.TrimSpace(os.Getenv(name)); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func envFloat(name string, def float64) float64 {
	if v := strings.TrimSpace(os.Getenv(name)); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			return f
		}
	}
	return def
}

func envList(name string) []string {
	out := []string{}
	for _, part := range strings.Split(os.Getenv(name), ",") {
		if p := strings.TrimSpace(part); p != "" {
			out = append(out, p)
		}
	}
	return out
}

func buildConfig() *config {
	hostname, _ := os.Hostname()
	cfg := &config{
		managerURL:     envStr("MANAGER_URL", "http://manager:8000"),
		host:           envStr("NODE_HOST", hostname),
		proxyHost:      envStr("PROXY_HOST", "0.0.0.0"),
		proxyPort:      envInt("PROXY_PORT", 8888),
		region:         envStr("REGION", "local"),
		tags:           envList("TAGS"),
		upstreams:      envList("UPSTREAMS"),
		maxConns:       envInt("MAX_CONNS", 512),
		connectTimeout: time.Duration(envFloat("CONNECT_TIMEOUT", 10.0) * float64(time.Second)),
		heartbeat:      time.Duration(envInt("HEARTBEAT_INTERVAL", 10)) * time.Second,
		nodeID:         envStr("NODE_ID", ""),
		name:           envStr("NODE_NAME", hostname),
		adminPort:      envInt("ADMIN_PORT", 8000),
	}
	cfg.egressHost = envStr("EGRESS_HOST", "127.0.0.1")
	if p := envInt("EGRESS_PORT", 0); p != 0 {
		cfg.egressPort = p
	} else {
		cfg.egressPort = cfg.proxyPort
	}
	return cfg
}

// ------------------------------------------------------------------ 主流程

func run(cfg *config) int {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	proxy := newProxyServer(cfg.proxyHost, cfg.proxyPort, cfg.maxConns, cfg.connectTimeout)
	// 登记本节点自己的地址：共享上游池是全局的，里面很可能有自己，
	// 不过滤掉会变成「自己连自己」的无限递归。
	proxy.upstreams.addSelf(
		hostPortKey(cfg.host, cfg.proxyPort),
		hostPortKey(cfg.egressHost, cfg.egressPort),
		hostPortKey(cfg.name, cfg.proxyPort),
		hostPortKey("127.0.0.1", cfg.proxyPort),
		hostPortKey("localhost", cfg.proxyPort),
		hostPortKey(cfg.proxyHost, cfg.proxyPort),
	)
	proxy.upstreams.set(cfg.upstreams)
	if err := proxy.start(); err != nil {
		log.Printf("[node] 代理端口监听失败：%v", err)
		return 1
	}

	reg := newRegistrar(
		cfg.managerURL,
		map[string]any{
			"host":        cfg.host,
			"port":        cfg.proxyPort,
			"egress_host": cfg.egressHost,
			"egress_port": cfg.egressPort,
			"scheme":      "http",
			"region":      cfg.region,
			"tags":        cfg.tags,
			"version":     version,
			"meta": map[string]any{
				"name":    cfg.name,
				"runtime": "go",
				"go":      runtime.Version(),
			},
		},
		cfg.nodeID,
		5*time.Second,
		cfg.heartbeat,
		cfg.upstreams,
	)

	if cfg.nodeID != "" {
		// 固定 id 时首次注册也要带上去
		reg.nodeID = cfg.nodeID
	}
	if !reg.registerWithRetry(ctx, 30*time.Second) {
		log.Printf("[node] 未完成注册，退出")
		return 1
	}
	proxy.upstreams.set(append(reg.Upstreams(), cfg.upstreams...))

	go reg.heartbeatLoop(ctx, proxy)

	admin := newAdminServer("0.0.0.0", cfg.adminPort, map[string]func() any{
		"/ping": func() any {
			return map[string]any{"ok": true, "node_id": reg.NodeID(), "version": version, "engine": "go"}
		},
		"/health": func() any {
			return map[string]any{"status": "ok", "node_id": reg.NodeID(), "version": version}
		},
		"/stats": func() any { return proxy.snapshot() },
		"/upstreams": func() any {
			return map[string]any{"ok": true, "upstreams": proxy.upstreams.list()}
		},
		"/config": func() any {
			return map[string]any{
				"manager_url":        cfg.managerURL,
				"host":               cfg.host,
				"proxy_host":         cfg.proxyHost,
				"proxy_port":         cfg.proxyPort,
				"egress_host":        cfg.egressHost,
				"egress_port":        cfg.egressPort,
				"admin_port":         cfg.adminPort,
				"region":             cfg.region,
				"tags":               cfg.tags,
				"max_conns":          cfg.maxConns,
				"connect_timeout":    cfg.connectTimeout.Seconds(),
				"heartbeat_interval": cfg.heartbeat.Seconds(),
				"node_id":            reg.NodeID(),
				"name":               cfg.name,
				"version":            version,
			}
		},
	})
	if err := admin.start(); err != nil {
		log.Printf("[node] 管理端口监听失败：%v", err)
	}

	log.Printf("[node] 节点就绪 | node_id=%s | 监听 %s:%d | 对外 %s:%d | region=%s | manager=%s",
		reg.NodeID(), cfg.proxyHost, cfg.proxyPort, cfg.egressHost, cfg.egressPort, cfg.region, cfg.managerURL)

	<-ctx.Done()
	log.Printf("[node] 收到退出信号，开始关闭……")
	reg.unregister()
	admin.stop()
	proxy.stop()
	log.Printf("[node] 节点已退出")
	return 0
}

// ------------------------------------------------------------------ 自检模式

func runHealthcheck() int {
	port := envInt("ADMIN_PORT", 8000)
	addr := net.JoinHostPort("127.0.0.1", strconv.Itoa(port))
	conn, err := net.DialTimeout("tcp", addr, 2*time.Second)
	if err != nil {
		fmt.Fprintf(os.Stderr, "healthcheck: %v\n", err)
		return 1
	}
	_ = conn.Close()
	return 0
}

func usage() {
	fmt.Fprintf(os.Stderr, `proxypool-node %s —— ProxyPool 代理节点（Go 版）

用法: proxypool-node [选项]

选项:
  --manager-url URL   manager 地址（默认 http://manager:8000，或环境变量 MANAGER_URL）
  --host HOST         manager 用来探测本节点的地址（默认容器主机名，或 NODE_HOST）
  --port PORT         本地代理监听端口（默认 8888，或 PROXY_PORT）
  --egress-host HOST  下发给客户端的地址（默认 127.0.0.1，或 EGRESS_HOST）
  --egress-port PORT  下发给客户端的端口（默认同 --port，或 EGRESS_PORT）
  --node-id ID        固定 node_id，便于容器重启后保持身份（或 NODE_ID）
  --healthcheck       只做一次本地健康检查后退出（容器 HEALTHCHECK 用）
  --version           打印版本后退出

环境变量: MANAGER_URL NODE_HOST NODE_NAME PROXY_HOST PROXY_PORT EGRESS_HOST EGRESS_PORT
          ADMIN_PORT REGION TAGS UPSTREAMS MAX_CONNS CONNECT_TIMEOUT HEARTBEAT_INTERVAL NODE_ID
`, version)
}

func main() {
	log.SetFlags(log.LstdFlags)
	log.SetOutput(os.Stdout)

	cfg := buildConfig()
	// 记录 EGRESS_PORT 是否被显式指定：没指定时它跟随 PROXY_PORT
	egressExplicit := envInt("EGRESS_PORT", 0) != 0

	args := os.Args[1:]
	take := func(i *int, inline string) (string, bool) {
		if inline != "" {
			return inline, true
		}
		if *i+1 < len(args) {
			*i++
			return args[*i], true
		}
		return "", false
	}

	for i := 0; i < len(args); i++ {
		a := args[i]
		key, inline, _ := strings.Cut(a, "=")
		value := func() string {
			v, ok := take(&i, inline)
			if !ok {
				fmt.Fprintf(os.Stderr, "参数 %s 缺少取值\n\n", key)
				usage()
				os.Exit(2)
			}
			return v
		}
		apply := func(name, v string) bool {
			switch name {
			case "--manager-url":
				cfg.managerURL = v
			case "--host":
				cfg.host = v
			case "--port":
				cfg.proxyPort = atoiOr(v, cfg.proxyPort)
				if !egressExplicit {
					cfg.egressPort = cfg.proxyPort
				}
			case "--egress-host":
				cfg.egressHost = v
			case "--egress-port":
				cfg.egressPort = atoiOr(v, cfg.egressPort)
			case "--admin-port":
				cfg.adminPort = atoiOr(v, cfg.adminPort)
			case "--node-id":
				cfg.nodeID = v
			case "--region":
				cfg.region = v
			default:
				return false
			}
			return true
		}

		switch key {
		case "--help", "-h":
			usage()
			return
		case "--version":
			fmt.Printf("proxypool-node %s (%s)\n", version, runtime.Version())
			return
		case "--healthcheck":
			os.Exit(runHealthcheck())
		}
		if !apply(key, value()) {
			fmt.Fprintf(os.Stderr, "未知参数: %s\n\n", a)
			usage()
			os.Exit(2)
		}
	}

	log.Printf("[node] proxypool-node v%s 启动（%s）", version, runtime.Version())
	os.Exit(run(cfg))
}

func atoiOr(s string, def int) int {
	if n, err := strconv.Atoi(strings.TrimSpace(s)); err == nil {
		return n
	}
	return def
}
