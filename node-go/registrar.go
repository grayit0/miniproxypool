package main

// 与 manager 通信：注册 / 心跳 / 注销。
//
// 关键点：HTTP client 显式把 Proxy 设为 nil，忽略 HTTP_PROXY 等环境变量 ——
// 否则宿主机/容器里存在全局代理时，注册请求本身也会被代理掉。

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"sync"
	"time"
)

type registrar struct {
	managerURL        string
	payload           map[string]any
	nodeID            string
	timeout           time.Duration
	heartbeatInterval time.Duration
	localUpstreams    []string

	mu        sync.Mutex
	upstreams []string

	client *http.Client
}

func newRegistrar(managerURL string, payload map[string]any, nodeID string, timeout, heartbeat time.Duration, localUpstreams []string) *registrar {
	transport := &http.Transport{
		Proxy: nil, // 明确绕过系统代理
		DialContext: (&net.Dialer{
			Timeout:   timeout,
			KeepAlive: 30 * time.Second,
		}).DialContext,
		MaxIdleConns:          8,
		IdleConnTimeout:       30 * time.Second,
		TLSHandshakeTimeout:   10 * time.Second,
		ExpectContinueTimeout: time.Second,
	}
	return &registrar{
		managerURL:        trimSlash(managerURL),
		payload:           payload,
		nodeID:            nodeID,
		timeout:           timeout,
		heartbeatInterval: heartbeat,
		localUpstreams:    append([]string(nil), localUpstreams...),
		client:            &http.Client{Transport: transport, Timeout: timeout},
	}
}

func trimSlash(s string) string {
	for len(s) > 0 && s[len(s)-1] == '/' {
		s = s[:len(s)-1]
	}
	return s
}

func (r *registrar) request(method, path string, payload any) (int, map[string]any, error) {
	var body io.Reader
	if payload != nil {
		raw, err := json.Marshal(payload)
		if err != nil {
			return 0, nil, err
		}
		body = bytes.NewReader(raw)
	}
	req, err := http.NewRequest(method, r.managerURL+path, body)
	if err != nil {
		return 0, nil, err
	}
	if payload != nil {
		req.Header.Set("Content-Type", "application/json; charset=utf-8")
	}
	req.Header.Set("User-Agent", "proxypool-node-go/"+versionTag)

	resp, err := r.client.Do(req)
	if err != nil {
		return 0, map[string]any{"detail": err.Error()}, err
	}
	defer func() { _ = resp.Body.Close() }()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))

	out := map[string]any{}
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &out); err != nil {
			out = map[string]any{"detail": string(raw)}
		}
	}
	return resp.StatusCode, out, nil
}

// NodeID 返回当前节点 id（注册后才有值）。
func (r *registrar) NodeID() string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.nodeID
}

// Upstreams 返回 manager 下发的共享上游列表。
func (r *registrar) Upstreams() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]string, len(r.upstreams))
	copy(out, r.upstreams)
	return out
}

func (r *registrar) setUpstreams(list []string) {
	r.mu.Lock()
	r.upstreams = append([]string(nil), list...)
	r.mu.Unlock()
}

func (r *registrar) registerOnce() bool {
	payload := make(map[string]any, len(r.payload)+1)
	for k, v := range r.payload {
		payload[k] = v
	}
	if r.NodeID() != "" {
		payload["node_id"] = r.NodeID()
	}

	status, body, err := r.request(http.MethodPost, "/api/nodes/register", payload)
	if err != nil {
		log.Printf("[node] 注册请求失败：%v", err)
		return false
	}
	if status != 200 {
		log.Printf("[node] 注册失败 status=%d %v", status, body["detail"])
		return false
	}
	id, _ := body["node_id"].(string)
	if id == "" {
		log.Printf("[node] 注册响应缺少 node_id：%v", body)
		return false
	}
	interval := r.heartbeatInterval
	if v, ok := toFloat(body["heartbeat_interval"]); ok && v > 0 {
		interval = time.Duration(v * float64(time.Second))
	}

	r.mu.Lock()
	r.nodeID = id
	r.heartbeatInterval = interval
	r.upstreams = toStrings(body["upstreams"])
	r.mu.Unlock()

	log.Printf("[node] 注册成功 node_id=%s 心跳周期=%s 共享上游=%d 条",
		id, interval, len(r.Upstreams()))
	return true
}

func (r *registrar) registerWithRetry(ctx context.Context, maxBackoff time.Duration) bool {
	delay := time.Second
	for {
		if r.registerOnce() {
			return true
		}
		log.Printf("[node] %s 后重试注册 manager=%s", delay, r.managerURL)
		select {
		case <-ctx.Done():
			return false
		case <-time.After(delay):
		}
		delay *= 2
		if delay > maxBackoff {
			delay = maxBackoff
		}
	}
}

func (r *registrar) heartbeatLoop(ctx context.Context, proxy *proxyServer) {
	timer := time.NewTimer(r.heartbeatIntervalValue())
	defer timer.Stop()
	consecutiveFail := 0

	for {
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
		}

		snap := proxy.st.snapshot()
		body := map[string]any{
			"active_conns": snap["active"],
			"served":       snap["total"],
		}
		status, resp, err := r.request(http.MethodPost,
			fmt.Sprintf("/api/nodes/%s/heartbeat", r.NodeID()), body)

		if err == nil && status == 200 {
			consecutiveFail = 0
			if ups, ok := resp["upstreams"]; ok {
				list := toStrings(ups)
				r.setUpstreams(list)
				proxy.upstreams.set(append(append([]string(nil), list...), r.localUpstreams...))
			}
			timer.Reset(r.heartbeatIntervalValue())
			continue
		}

		consecutiveFail++
		if err != nil {
			log.Printf("[node] 心跳异常：%v（连续 %d 次）", err, consecutiveFail)
		} else {
			log.Printf("[node] 心跳失败 status=%d %v（连续 %d 次）", status, resp["detail"], consecutiveFail)
		}

		if (err == nil && (status == 404 || status == 410)) || consecutiveFail >= 3 {
			log.Printf("[node] 重新注册节点……")
			if r.registerWithRetry(ctx, 30*time.Second) {
				proxy.upstreams.set(append(r.Upstreams(), r.localUpstreams...))
				consecutiveFail = 0
			}
		}
		timer.Reset(r.heartbeatIntervalValue())
	}
}

func (r *registrar) heartbeatIntervalValue() time.Duration {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.heartbeatInterval
}

func (r *registrar) unregister() {
	id := r.NodeID()
	if id == "" {
		return
	}
	status, _, err := r.request(http.MethodPost, fmt.Sprintf("/api/nodes/%s/unregister", id), map[string]any{})
	if err != nil {
		log.Printf("[node] 注销失败：%v", err)
		return
	}
	log.Printf("[node] 已向 manager 注销 node_id=%s status=%d", id, status)
}

// ------------------------------------------------------------------ 小工具

func toFloat(v any) (float64, bool) {
	switch n := v.(type) {
	case float64:
		return n, true
	case int:
		return float64(n), true
	case int64:
		return float64(n), true
	case json.Number:
		f, err := n.Float64()
		return f, err == nil
	}
	return 0, false
}

func toStrings(v any) []string {
	raw, ok := v.([]any)
	if !ok {
		return nil
	}
	out := make([]string, 0, len(raw))
	for _, item := range raw {
		if s, ok := item.(string); ok && s != "" {
			out = append(out, s)
		}
	}
	return out
}
