package main

// ProxyPool Node —— Go 版代理服务。
//
// 只用标准库，支持：
//   - CONNECT 隧道（HTTPS）
//   - 绝对 URI 形式的明文 HTTP（GET http://a.b/c）
//   - 相对路径 + Host 头
//   - 可选的上游代理链（round-robin + 失败熔断）
//
// 与 Python 版的两点有意差异：
//  1. 每个连接的读写用 goroutine + net.Conn 直连，没有 asyncio 的事件循环开销；
//  2. 明文 HTTP 转发时会注入 `Connection: close`，强制「一个连接一个请求」，
//     这样客户端复用连接时不会把第二个绝对 URI 请求直接透传给源服务器（会被 nginx 之类拒绝）。

import (
	"bufio"
	"bytes"
	"encoding/base64"
	"errors"
	"fmt"
	"log"
	"net"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const (
	readHeadTimeout   = 15 * time.Second
	halfCloseGrace    = 10 * time.Second
	maxHeadBytes      = 1 << 16
	copyBufferSize    = 64 << 10
	upstreamFailLimit = 3
)

var errHeadTooLarge = errors.New("请求头过大")

// ------------------------------------------------------------------ 统计

type stats struct {
	active   int64
	total    int64
	errs     int64
	bytesIn  int64
	bytesOut int64
}

func (s *stats) snapshot() map[string]int64 {
	return map[string]int64{
		"active":    atomic.LoadInt64(&s.active),
		"total":     atomic.LoadInt64(&s.total),
		"errors":    atomic.LoadInt64(&s.errs),
		"bytes_in":  atomic.LoadInt64(&s.bytesIn),
		"bytes_out": atomic.LoadInt64(&s.bytesOut),
	}
}

// ------------------------------------------------------------------ 上游池

type upstreamPool struct {
	mu    sync.Mutex
	urls  []string
	fails map[string]int
	rr    int
	// self 是本节点自己的地址集合（host:port）。
	// manager 的共享上游池是「所有人的」，里面很可能就有自己 —— 不过滤掉的话，
	// 本节点会把请求 CONNECT 给自己，自己再 CONNECT 给自己，无限递归到超时。
	self map[string]bool
}

// addSelf 登记本节点自己的地址（host:port 形式，大小写不敏感）。
func (p *upstreamPool) addSelf(addrs ...string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.self == nil {
		p.self = make(map[string]bool)
	}
	for _, a := range addrs {
		a = strings.ToLower(strings.TrimSpace(a))
		if a == "" || strings.HasPrefix(a, "0.0.0.0:") || strings.HasPrefix(a, "[::]:") {
			continue
		}
		p.self[a] = true
	}
}

func (p *upstreamPool) isSelf(u string) bool {
	key := urlHostPort(u)
	if key == "" {
		return false
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.self[key]
}

func urlHostPort(raw string) string {
	u, err := url.Parse(raw)
	if err != nil || u.Hostname() == "" {
		return ""
	}
	port := u.Port()
	if port == "" {
		port = "80"
	}
	return strings.ToLower(net.JoinHostPort(u.Hostname(), port))
}

// hostPortKey 拼出 host:port 形式的比较键；host 为空返回空串。
func hostPortKey(host string, port int) string {
	host = strings.ToLower(strings.TrimSpace(host))
	if host == "" || host == "0.0.0.0" || host == "::" || host == "[::]" {
		return ""
	}
	return net.JoinHostPort(host, strconv.Itoa(port))
}

func normalizeUpstream(raw string) (string, bool) {
	u := strings.TrimSpace(raw)
	if u == "" {
		return "", false
	}
	if !strings.Contains(u, "://") {
		u = "http://" + u
	}
	p, err := url.Parse(u)
	if err != nil || p.Host == "" {
		return "", false
	}
	if p.Scheme != "http" {
		log.Printf("[node] 暂不支持的上游协议，已忽略：%s", u)
		return "", false
	}
	return u, true
}

func (p *upstreamPool) set(list []string) {
	norm := make([]string, 0, len(list))
	seen := make(map[string]bool, len(list))
	for _, raw := range list {
		u, ok := normalizeUpstream(raw)
		if !ok || seen[u] {
			continue
		}
		if p.isSelf(u) {
			log.Printf("[node] 共享上游里指向本节点自己，已忽略：%s", u)
			continue
		}
		seen[u] = true
		norm = append(norm, u)
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if equalStrings(norm, p.urls) {
		return
	}
	p.urls = norm
	p.fails = make(map[string]int, len(norm))
	log.Printf("[node] 上游列表已更新：%d 条", len(norm))
}

func (p *upstreamPool) list() []string {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := make([]string, len(p.urls))
	copy(out, p.urls)
	return out
}

func (p *upstreamPool) failCounts() map[string]int {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := make(map[string]int)
	for k, v := range p.fails {
		if v > 0 {
			out[k] = v
		}
	}
	return out
}

func (p *upstreamPool) pick() string {
	p.mu.Lock()
	defer p.mu.Unlock()
	n := len(p.urls)
	if n == 0 {
		return ""
	}
	for i := 0; i < n; i++ {
		u := p.urls[p.rr%n]
		p.rr++
		if p.fails[u] < upstreamFailLimit {
			return u
		}
	}
	// 全部处于熔断状态：清零后重试一轮
	p.fails = make(map[string]int, n)
	u := p.urls[p.rr%n]
	p.rr++
	return u
}

func (p *upstreamPool) mark(u string, ok bool) {
	if u == "" {
		return
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if ok {
		p.fails[u] = 0
		return
	}
	p.fails[u] = p.fails[u] + 1
}

// ------------------------------------------------------------------ 代理服务

type proxyServer struct {
	host           string
	port           int
	maxConns       int
	connectTimeout time.Duration

	upstreams upstreamPool
	st        stats
	startedAt time.Time

	limiter chan struct{}
	ln      net.Listener
	wg      sync.WaitGroup
	closing atomic.Bool
}

func newProxyServer(host string, port, maxConns int, connectTimeout time.Duration) *proxyServer {
	if maxConns < 8 {
		maxConns = 8
	}
	return &proxyServer{
		host:           host,
		port:           port,
		maxConns:       maxConns,
		connectTimeout: connectTimeout,
		limiter:        make(chan struct{}, maxConns),
		startedAt:      time.Now(),
		upstreams:      upstreamPool{self: make(map[string]bool)},
	}
}

func (s *proxyServer) start() error {
	ln, err := net.Listen("tcp", net.JoinHostPort(s.host, strconv.Itoa(s.port)))
	if err != nil {
		return err
	}
	s.ln = ln
	log.Printf("[node] 代理服务已监听 %s", ln.Addr())
	s.wg.Add(1)
	go s.acceptLoop()
	return nil
}

func (s *proxyServer) stop() {
	s.closing.Store(true)
	if s.ln != nil {
		_ = s.ln.Close()
	}
	done := make(chan struct{})
	go func() { s.wg.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
	}
}

func (s *proxyServer) acceptLoop() {
	defer s.wg.Done()
	var delay time.Duration
	for {
		conn, err := s.ln.Accept()
		if err != nil {
			if s.closing.Load() {
				return
			}
			if delay == 0 {
				delay = 5 * time.Millisecond
			} else if delay < time.Second {
				delay *= 2
			}
			log.Printf("[node] 接受连接失败：%v（%s 后重试）", err, delay)
			time.Sleep(delay)
			continue
		}
		delay = 0
		s.wg.Add(1)
		go func() {
			defer s.wg.Done()
			s.handle(conn)
		}()
	}
}

func (s *proxyServer) snapshot() map[string]any {
	out := map[string]any{
		"ok":             true,
		"listen":         net.JoinHostPort(s.host, strconv.Itoa(s.port)),
		"engine":         "go",
		"uptime_sec":     round1(time.Since(s.startedAt).Seconds()),
		"upstreams":      s.upstreams.list(),
		"upstream_fails": s.upstreams.failCounts(),
		"max_conns":      s.maxConns,
	}
	for k, v := range s.st.snapshot() {
		out[k] = v
	}
	return out
}

func (s *proxyServer) handle(conn net.Conn) {
	s.limiter <- struct{}{}
	defer func() { <-s.limiter }()

	atomic.AddInt64(&s.st.active, 1)
	atomic.AddInt64(&s.st.total, 1)
	defer atomic.AddInt64(&s.st.active, -1)

	if tc, ok := conn.(*net.TCPConn); ok {
		_ = tc.SetNoDelay(true)
	}
	defer func() {
		if r := recover(); r != nil {
			atomic.AddInt64(&s.st.errs, 1)
			log.Printf("[node] 处理连接时出现异常：%v", r)
		}
		_ = conn.Close()
	}()

	clientReader := bufio.NewReaderSize(conn, 16<<10)
	head, err := readHeadWithTimeout(conn, clientReader, readHeadTimeout)
	if err != nil {
		if !errors.Is(err, errHeadTooLarge) && !isNetClosed(err) {
			log.Printf("[node] 读取请求头失败：%v", err)
		}
		if errors.Is(err, errHeadTooLarge) {
			reply(conn, 431, "Request Header Fields Too Large")
		}
		return
	}

	method, target, _, err := parseRequestLine(head)
	if err != nil {
		reply(conn, 400, "Bad Request")
		return
	}

	var (
		host         string
		port         int
		absoluteForm bool
	)
	if method == "CONNECT" {
		host, port = splitAuthority(target, 443)
	} else if u, uerr := url.Parse(target); uerr == nil && u.Host != "" {
		absoluteForm = true
		host = u.Hostname()
		if u.Port() != "" {
			port, _ = strconv.Atoi(u.Port())
		} else if u.Scheme == "https" {
			port = 443
		} else {
			port = 80
		}
	} else {
		// 相对路径：退化成用 Host 头
		hv := headerValue(head, "host")
		if hv == "" {
			reply(conn, 400, "Bad Request: 缺少 Host")
			return
		}
		host, port = splitAuthority(hv, 80)
	}
	if host == "" {
		reply(conn, 400, "Bad Request: 目标地址为空")
		return
	}

	remote, remoteReader, upstreamUsed, err := s.openRemote(host, port)
	if err != nil {
		s.upstreams.mark(upstreamUsed, false)
		atomic.AddInt64(&s.st.errs, 1)
		log.Printf("[node] 连接 %s:%d 失败：%v", host, port, err)
		reply(conn, 502, "Bad Gateway")
		return
	}
	defer func() { _ = remote.Close() }()
	s.upstreams.mark(upstreamUsed, true)

	if method == "CONNECT" {
		if _, err := conn.Write([]byte("HTTP/1.1 200 Connection Established\r\nProxy-Agent: proxypool-node-go\r\n\r\n")); err != nil {
			return
		}
	} else {
		forward := head
		if absoluteForm {
			forward = toOriginForm(head, target)
		} else {
			forward = ensureClose(head)
		}
		if _, err := remote.Write(forward); err != nil {
			atomic.AddInt64(&s.st.errs, 1)
			return
		}
	}

	s.tunnel(conn, clientReader, remote, remoteReader)
}

// openRemote 直连或经上游代理连接目标，返回 (连接, 读缓冲, 用到的上游, 错误)。
func (s *proxyServer) openRemote(host string, port int) (net.Conn, *bufio.Reader, string, error) {
	authority := net.JoinHostPort(host, strconv.Itoa(port))
	upstream := s.upstreams.pick()
	if upstream == "" {
		conn, err := net.DialTimeout("tcp", authority, s.connectTimeout)
		if err != nil {
			return nil, nil, "", err
		}
		return conn, bufio.NewReaderSize(conn, 16<<10), "", nil
	}

	u, err := url.Parse(upstream)
	if err != nil {
		return nil, nil, "", fmt.Errorf("上游地址非法 %s: %w", upstream, err)
	}
	upAddr := u.Host
	if u.Port() == "" {
		upAddr = net.JoinHostPort(u.Hostname(), "80")
	}
	conn, err := net.DialTimeout("tcp", upAddr, s.connectTimeout)
	if err != nil {
		return nil, nil, upstream, err
	}
	reader := bufio.NewReaderSize(conn, 16<<10)

	var sb strings.Builder
	fmt.Fprintf(&sb, "CONNECT %s HTTP/1.1\r\nHost: %s\r\n", authority, authority)
	if u.User != nil {
		pass, _ := u.User.Password()
		cred := base64.StdEncoding.EncodeToString([]byte(u.User.Username() + ":" + pass))
		fmt.Fprintf(&sb, "Proxy-Authorization: Basic %s\r\n", cred)
	}
	sb.WriteString("Proxy-Connection: keep-alive\r\n\r\n")
	if _, err := conn.Write([]byte(sb.String())); err != nil {
		_ = conn.Close()
		return nil, nil, upstream, err
	}

	resp, err := readHeadWithTimeout(conn, reader, s.connectTimeout)
	if err != nil {
		_ = conn.Close()
		return nil, nil, upstream, err
	}
	statusLine := strings.SplitN(string(resp), "\r\n", 2)[0]
	if !strings.Contains(statusLine, " 200") {
		_ = conn.Close()
		return nil, nil, upstream, fmt.Errorf("上游 %s 拒绝了 CONNECT：%s", upstream, statusLine)
	}
	return conn, reader, upstream, nil
}

// tunnel 双向转发，任一侧先结束就收尾。
func (s *proxyServer) tunnel(client net.Conn, clientReader *bufio.Reader, remote net.Conn, remoteReader *bufio.Reader) {
	done := make(chan int, 2)
	go func() { s.pipe(remote, clientReader, &s.st.bytesIn); done <- 1 }()
	go func() { s.pipe(client, remoteReader, &s.st.bytesOut); done <- 2 }()

	first := <-done
	if first == 1 {
		// 客户端请求发送完毕：等响应回来，最多 halfCloseGrace
		select {
		case <-done:
		case <-time.After(halfCloseGrace):
		}
	} else {
		<-done
	}
}

func (s *proxyServer) pipe(dst net.Conn, src *bufio.Reader, counter *int64) {
	buf := make([]byte, copyBufferSize)
	for {
		n, err := src.Read(buf)
		if n > 0 {
			atomic.AddInt64(counter, int64(n))
			if _, werr := dst.Write(buf[:n]); werr != nil {
				break
			}
		}
		if err != nil {
			break
		}
	}
	if cw, ok := dst.(interface{ CloseWrite() error }); ok {
		_ = cw.CloseWrite()
	}
}

// ------------------------------------------------------------------ 报文工具

func readHeadWithTimeout(conn net.Conn, reader *bufio.Reader, timeout time.Duration) ([]byte, error) {
	if timeout > 0 {
		_ = conn.SetReadDeadline(time.Now().Add(timeout))
		defer func() { _ = conn.SetReadDeadline(time.Time{}) }()
	}
	var buf []byte
	for {
		line, err := reader.ReadBytes('\n')
		if len(buf)+len(line) > maxHeadBytes {
			return nil, errHeadTooLarge
		}
		buf = append(buf, line...)
		if err != nil {
			return nil, err
		}
		if bytes.HasSuffix(buf, []byte("\r\n\r\n")) {
			return buf, nil
		}
		if len(line) == 1 && line[0] == '\n' {
			return buf, nil
		}
	}
}

func parseRequestLine(head []byte) (method, target, proto string, err error) {
	line := head
	if idx := bytes.Index(head, []byte("\r\n")); idx >= 0 {
		line = head[:idx]
	} else if idx := bytes.IndexByte(head, '\n'); idx >= 0 {
		line = head[:idx]
	}
	parts := strings.SplitN(strings.TrimRight(string(line), "\r\n"), " ", 3)
	if len(parts) < 2 {
		return "", "", "", errors.New("请求行不合法")
	}
	method = strings.ToUpper(parts[0])
	target = parts[1]
	if len(parts) > 2 {
		proto = parts[2]
	}
	return method, target, proto, nil
}

// splitAuthority 解析 `host:port` / `[::1]:8080` / `host`。
func splitAuthority(authority string, defaultPort int) (string, int) {
	a := strings.TrimSpace(authority)
	if a == "" {
		return "", defaultPort
	}
	if strings.HasPrefix(a, "[") {
		if end := strings.Index(a, "]"); end > 0 {
			host := a[1:end]
			rest := strings.TrimPrefix(a[end+1:], ":")
			if p, err := strconv.Atoi(rest); err == nil {
				return host, p
			}
			return host, defaultPort
		}
	}
	host, portStr, ok := strings.Cut(a, ":")
	if !ok {
		return a, defaultPort
	}
	if p, err := strconv.Atoi(portStr); err == nil {
		return host, p
	}
	return a, defaultPort
}

func headerValue(head []byte, name string) string {
	want := strings.ToLower(name)
	lines := bytes.Split(head, []byte("\r\n"))
	for _, line := range lines[1:] {
		key, value, ok := bytes.Cut(line, []byte(":"))
		if !ok {
			continue
		}
		if strings.ToLower(strings.TrimSpace(string(key))) == want {
			return strings.TrimSpace(string(value))
		}
	}
	return ""
}

// hopByHop 永远不该被转发到源服务器的头。
var hopByHop = map[string]bool{
	"proxy-connection":    true,
	"proxy-authorization": true,
	"proxy-authenticate":  true,
	"connection":          true,
	"keep-alive":          true,
}

// toOriginForm 把 `GET http://a.b/c?d=1 HTTP/1.1` 改写为 `GET /c?d=1 HTTP/1.1`，
// 并丢掉逐跳头、补上 `Connection: close`（明文转发强制一连接一请求）。
func toOriginForm(head []byte, target string) []byte {
	path := "/"
	if u, err := url.Parse(target); err == nil {
		if u.Path != "" {
			path = u.Path
		}
		if u.RawQuery != "" {
			path += "?" + u.RawQuery
		}
	}

	method, _, proto, err := parseRequestLine(head)
	if err != nil || proto == "" {
		proto = "HTTP/1.1"
	}

	// Connection 头里列出的自定义逐跳头也要一并去掉
	extraHop := map[string]bool{}
	for _, tok := range strings.Split(headerValue(head, "connection"), ",") {
		tok = strings.ToLower(strings.TrimSpace(tok))
		if tok != "" && tok != "close" && tok != "keep-alive" {
			extraHop[tok] = true
		}
	}

	var sb strings.Builder
	fmt.Fprintf(&sb, "%s %s %s\r\n", method, path, proto)
	for _, line := range bytes.Split(head, []byte("\r\n"))[1:] {
		if len(line) == 0 {
			continue
		}
		key, _, ok := bytes.Cut(line, []byte(":"))
		if !ok {
			continue
		}
		k := strings.ToLower(strings.TrimSpace(string(key)))
		if hopByHop[k] || extraHop[k] {
			continue
		}
		sb.Write(line)
		sb.WriteString("\r\n")
	}
	sb.WriteString("Connection: close\r\n\r\n")
	return []byte(sb.String())
}

// ensureClose 相对路径形式（少见）时也补上 Connection: close。
func ensureClose(head []byte) []byte {
	if headerValue(head, "connection") != "" {
		return head
	}
	trimmed := bytes.TrimSuffix(head, []byte("\r\n\r\n"))
	out := make([]byte, 0, len(head)+20)
	out = append(out, trimmed...)
	out = append(out, []byte("\r\nConnection: close\r\n\r\n")...)
	return out
}

func reply(conn net.Conn, status int, message string) {
	body := fmt.Sprintf("%d %s", status, message)
	_, _ = fmt.Fprintf(conn,
		"HTTP/1.1 %d %s\r\nContent-Type: text/plain; charset=utf-8\r\nConnection: close\r\nContent-Length: %d\r\n\r\n%s",
		status, message, len(body), body)
}

func round1(v float64) float64 {
	return float64(int64(v*10+0.5)) / 10
}

func isNetClosed(err error) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, net.ErrClosed) {
		return true
	}
	msg := err.Error()
	return strings.Contains(msg, "use of closed network connection") ||
		strings.Contains(msg, "connection reset by peer") ||
		strings.Contains(msg, "EOF")
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func sortedAnyKeys(m map[string]func() any) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
