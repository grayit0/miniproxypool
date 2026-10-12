<#
.SYNOPSIS
    把 .github/workflows/*.yml 推到 GitHub。

.DESCRIPTION
    这一步必须用带 `workflow` scope 的凭证。GitHub 对任何写入 .github/workflows/
    的请求都做这个校验：token 没有 `workflow` scope 时不会返回 403，而是直接
    返回 404，git push 则会报：

        refusing to allow a Personal Access Token to create or update workflow
        `.github/workflows/xxx.yml` without `workflow` scope

    所以如果你看到 404，先确认 token 的 scope。

    怎么拿到带 workflow scope 的 token（两种都行）：
      A. classic PAT：Settings → Developer settings → Personal access tokens →
         Tokens (classic) → 编辑现有 token → 勾上 `workflow` → Update。
      B. fine-grained PAT：Settings → Developer settings → Personal access tokens →
         Fine-grained tokens → 新建 → Repository permissions →
         Workflows = Read and write。

.PARAMETER Token
    带 workflow scope 的 PAT。不会写进任何文件，也不会打印出来。

.EXAMPLE
    .\scripts\push_workflows.ps1 -Token ghp_xxxxxxxx
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Token,

    [string]$Owner = "grayit0",
    [string]$Repo = "miniproxypool",
    [string]$Branch = "main",
    [string]$Message = "ci: add GitHub Actions workflows (build-images / e2e-go-node / run-pool)"
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$repoRoot = Split-Path -Parent $PSScriptRoot
$wfDir = Join-Path $repoRoot ".github\workflows"

if (-not (Test-Path $wfDir)) {
    Write-Error "找不到目录：$wfDir"
    return
}

$files = @(Get-ChildItem -Path $wfDir -File | Where-Object { $_.Extension -in @(".yml", ".yaml") })
if ($files.Count -eq 0) {
    Write-Error "$wfDir 里没有 .yml 文件。"
    return
}

$headers = @{
    "User-Agent"    = "proxypool-push-workflows"
    "Accept"        = "application/vnd.github+json"
    "Authorization" = "token $Token"
}
$api = "https://api.github.com/repos/$Owner/$Repo/contents"

# 先自检 scope，避免拿到 404 之后还要猜原因。
try {
    $probe = Invoke-WebRequest -Uri "https://api.github.com/user" -Headers $headers -Method GET -UseBasicParsing
    $scopes = $probe.Headers["X-OAuth-Scopes"]
    Write-Host "token scopes: $scopes"
    if ($scopes -and ($scopes -notmatch "workflow")) {
        Write-Warning "这个 token 的 scope 里没有 workflow，推送 .github/workflows/ 会失败（GitHub 会返回 404）。"
    }
}
catch {
    Write-Warning "无法读取 token 信息（继续尝试）：$($_.Exception.Message)"
}

$ok = 0
$failed = @()

foreach ($f in $files) {
    # GitHub Contents API 的路径用正斜杠，且需要带上 .github/ 前缀。
    $rel = ".github/workflows/" + $f.Name
    $uri = "$api/$rel"

    $sha = $null
    try {
        $existing = Invoke-RestMethod -Uri "$uri`?ref=$Branch" -Headers $headers -Method GET
        $sha = $existing.sha
        Write-Host ("{0}  -> 远端已存在，将更新（sha {1}）" -f $f.Name, $sha.Substring(0, 7))
    }
    catch {
        $code = $null
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        if ($code -eq 404) {
            Write-Host ("{0}  -> 远端不存在，将新建" -f $f.Name)
        }
        else {
            Write-Warning ("{0}  -> 读取失败（HTTP {1}）：{2}" -f $f.Name, $code, $_.Exception.Message)
        }
    }

    $bytes = [System.IO.File]::ReadAllBytes($f.FullName)
    $body = @{
        message = $Message
        content = [System.Convert]::ToBase64String($bytes)
        branch  = $Branch
    }
    if ($sha) { $body.sha = $sha }

    $json = $body | ConvertTo-Json -Compress

    try {
        $res = Invoke-RestMethod -Uri $uri -Headers $headers -Method PUT `
            -ContentType "application/json; charset=utf-8" `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($json))
        Write-Host ("    OK  提交 {0}" -f $res.commit.sha.Substring(0, 7)) -ForegroundColor Green
        $ok++
    }
    catch {
        $code = $null
        $detail = $_.Exception.Message
        if ($_.Exception.Response) {
            $code = [int]$_.Exception.Response.StatusCode
            try {
                $sr = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
                $detail = $sr.ReadToEnd()
            }
            catch { }
        }
        Write-Host ("    FAIL  HTTP {0}  {1}" -f $code, $detail) -ForegroundColor Red
        $failed += $f.Name

        if ($code -eq 404) {
            Write-Host "    （404 = token 缺少 workflow scope，这是最可能的原因）" -ForegroundColor Yellow
        }
    }
}

Write-Host ""
Write-Host ("完成：{0}/{1} 个 workflow 文件推送成功。" -f $ok, $files.Count)

if ($failed.Count -gt 0) {
    Write-Host ("失败的：{0}" -f ($failed -join ", ")) -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "现在去 GitHub → Actions 页面看："
Write-Host "  * build-images  会立刻构建并推送 GHCR 镜像，然后跑冒烟测试"
Write-Host "  * e2e-go-node   跑 Go 节点的端到端测试"
Write-Host "  * run-pool      手动触发，在 runner 上把整套池子跑起来并给出公网地址"
