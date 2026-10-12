# 给 proxypool 批量补 node 容器（不重启 compose）。
#
# 用法（在仓库根目录执行）：
#   .\scripts\add_nodes.ps1 -Count 3
#   .\scripts\add_nodes.ps1 -Count 5 -StartHostPort 18810 -Region us
#   .\scripts\add_nodes.ps1 -Count 2 -UseGhcr -EgressHost 203.0.113.10   # 公网机器
#
# 注意 EgressHost 是「客户端该用什么地址访问节点」：
#   本机测试填 127.0.0.1；公网服务器填那台机器的公网 IP 或域名。
param(
    [int]$Count = 1,
    [int]$StartHostPort = 18804,
    [int]$ProxyPort = 8888,
    [int]$AdminPort = 8000,
    [string]$Region = "local",
    [string]$Network = "proxypool_default",
    [string]$ManagerUrl = "http://manager:8000",
    [string]$EgressHost = "127.0.0.1",
    [string]$Image = "proxypool-node",
    [string]$GhcrPrefix = "ghcr.io/grayit0/miniproxypool",
    [string]$GhcrTag = "latest",
    [string]$NamePrefix = "node",
    [switch]$UseGhcr
)

if ($UseGhcr) {
    $Image = "$GhcrPrefix-node:$GhcrTag"
}

$existing = (docker ps -a --format "{{.Names}}")
for ($i = 0; $i -lt $Count; $i++) {
    $hostPort = $StartHostPort + $i
    $name = "$NamePrefix-extra-$hostPort"
    if ($existing -contains $name) {
        Write-Host "跳过已存在的容器 $name"
        continue
    }
    Write-Host "启动 $name -> 宿主 $hostPort / 容器 $ProxyPort（镜像 $Image）"
    docker run -d `
        --name $name `
        --hostname $name `
        --network $Network `
        --restart unless-stopped `
        -e "MANAGER_URL=$ManagerUrl" `
        -e "NODE_HOST=$name" `
        -e "NODE_NAME=$name" `
        -e "PROXY_PORT=$ProxyPort" `
        -e "ADMIN_PORT=$AdminPort" `
        -e "EGRESS_HOST=$EgressHost" `
        -e "EGRESS_PORT=$hostPort" `
        -e "REGION=$Region" `
        -p "${hostPort}:$ProxyPort" `
        $Image | Out-Null
}

Write-Host ""
Write-Host "当前 proxypool 相关容器："
docker ps --filter "name=proxypool" --filter "name=$NamePrefix" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
