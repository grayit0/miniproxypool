package main

// 节点自省 HTTP 服务：/ping /stats /upstreams /config /health。
//
// 仅供运维排查，不做认证，不要暴露到公网。

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"strings"
	"time"
)

type adminServer struct {
	host   string
	port   int
	routes map[string]func() any

	server *http.Server
	ln     net.Listener
}

func newAdminServer(host string, port int, routes map[string]func() any) *adminServer {
	return &adminServer{host: host, port: port, routes: routes}
}

func (a *adminServer) start() error {
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		path := strings.TrimRight(r.URL.Path, "/")
		if path == "" {
			path = "/"
		}
		handler, ok := a.routes[path]
		if !ok {
			writeJSON(w, http.StatusNotFound, map[string]any{
				"ok":     false,
				"error":  "not found",
				"routes": sortedAnyKeys(a.routes),
			})
			return
		}
		defer func() {
			if rec := recover(); rec != nil {
				writeJSON(w, http.StatusInternalServerError, map[string]any{"ok": false, "error": fmt.Sprint(rec)})
			}
		}()
		writeJSON(w, http.StatusOK, handler())
	})

	ln, err := net.Listen("tcp", net.JoinHostPort(a.host, fmt.Sprintf("%d", a.port)))
	if err != nil {
		return err
	}
	a.ln = ln
	a.server = &http.Server{
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      10 * time.Second,
	}
	go func() {
		if err := a.server.Serve(ln); err != nil && err != http.ErrServerClosed {
			log.Printf("[node] 管理端口异常退出：%v", err)
		}
	}()
	log.Printf("[node] 管理端口已监听 %s:%d（%s）", a.host, a.port, strings.Join(sortedAnyKeys(a.routes), " "))
	return nil
}

func (a *adminServer) stop() {
	if a.server == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_ = a.server.Shutdown(ctx)
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	raw, err := json.Marshal(payload)
	if err != nil {
		http.Error(w, `{"ok":false,"error":"marshal failed"}`, http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_, _ = w.Write(raw)
}
