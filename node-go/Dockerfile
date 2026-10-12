# 多阶段构建：编译出来的静态二进制只有几 MB，最终镜像 ~15MB（Python 版约 150MB）。
FROM golang:1.23-alpine AS build

WORKDIR /src

# 先只拷 go.mod，最大化利用构建缓存（本项目零三方依赖）
COPY go.mod ./
RUN go mod download

COPY *.go ./
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/proxypool-node .

FROM alpine:3.20

# ca-certificates：MANAGER_URL 走 https 时需要
RUN apk add --no-cache ca-certificates \
    && adduser -D -u 10001 app

COPY --from=build /out/proxypool-node /usr/local/bin/proxypool-node

ENV MANAGER_URL=http://manager:8000 \
    NODE_HOST=node \
    PROXY_HOST=0.0.0.0 \
    PROXY_PORT=8888 \
    ADMIN_PORT=8000 \
    EGRESS_HOST=127.0.0.1 \
    REGION=local

USER app

EXPOSE 8888 8000

# 用自己的 --healthcheck 子模式，镜像里不需要 curl/wget
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD ["/usr/local/bin/proxypool-node", "--healthcheck"]

ENTRYPOINT ["/usr/local/bin/proxypool-node"]
