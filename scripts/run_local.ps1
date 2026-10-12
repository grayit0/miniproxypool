#Requires -Version 5.1
<#
.SYNOPSIS
    Windows 上一条命令把 proxypool 跑起来（不需要 Docker）。

.DESCRIPTION
    先找一个装了 fastapi/uvicorn/requests 的 Python，顺序是：
      1. 环境变量 PROXYPOOL_PYTHON
      2. 仓库里的 .venv
      3. PATH 上的 python / python3
    都没有就用系统 python 建一个 .venv 并装依赖（只做一次）。
    然后把参数原样交给 scripts/run_local.py，真正的进程管理在那边（Python 的
    subprocess，比 PowerShell 的 Start-Process 可靠：后者在本机 -RedirectStandardOutput
    拿不到任何输出）。

.EXAMPLE
    .\scripts\run_local.ps1 start -n 3        # 起 manager + 3 个节点
.EXAMPLE
    .\scripts\run_local.ps1 status            # 看进程和池子状态
.EXAMPLE
    .\scripts\run_local.ps1 logs node1 -f     # 跟日志
.EXAMPLE
    .\scripts\run_local.ps1 stop              # 全部停掉
.EXAMPLE
    $env:PROXYPOOL_PYTHON = "D:\venvs\pool\Scripts\python.exe"
    .\scripts\run_local.ps1 start             # 指定解释器
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

# 刻意用 Continue 而不是 Stop：这个脚本要反复调用外部 python，子进程往 stderr 写
# traceback / warning 时，Stop 模式在 PowerShell 5.1 下会被当成致命错误直接把整个
# 脚本带走（探到一个没装依赖的解释器就崩）。该终止的地方后面都显式判断 exit code。
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

$Root = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $PSScriptRoot "run_local.py"

if (-not (Test-Path $Runner)) {
    Write-Error "找不到 $Runner"
    exit 1
}

function Test-Interpreter {
    param([string]$Exe)
    if ([string]::IsNullOrWhiteSpace($Exe)) { return $false }
    if (-not (Test-Path $Exe)) { return $false }
    # 探测依赖是否齐。注意用单引号，别让 PowerShell 去解析里面的内容。
    # 关键：$ErrorActionPreference = "Stop" 时，子进程往 stderr 写东西（比如
    # ModuleNotFoundError 的 traceback）会被 PowerShell 5.1 当成致命错误直接中断脚本，
    # 所以这里必须先放宽，否则探到一个没装依赖的解释器就会把整个脚本带走。
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $null = & $Exe -c 'import fastapi, uvicorn, requests' 2>&1
        return ($LASTEXITCODE -eq 0)
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Resolve-Interpreter {
    $candidates = New-Object System.Collections.ArrayList
    if ($env:PROXYPOOL_PYTHON) { [void]$candidates.Add($env:PROXYPOOL_PYTHON) }
    [void]$candidates.Add((Join-Path $Root ".venv\Scripts\python.exe"))
    foreach ($name in @("python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source) { [void]$candidates.Add($cmd.Source) }
    }
    foreach ($cand in $candidates) {
        if (Test-Interpreter $cand) { return $cand }
    }
    return ""
}

$py = Resolve-Interpreter

if (-not $py) {
    $base = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $base) {
        Write-Host "没找到可用的 Python。先装一个 Python 3.9+，再设一下解释器路径：" -ForegroundColor Yellow
        Write-Host '    $env:PROXYPOOL_PYTHON = "C:\path\to\python.exe"'
        exit 1
    }
    Write-Host "没有装了 fastapi/uvicorn/requests 的解释器，正在创建 .venv 并装依赖（只需一次）……" -ForegroundColor Cyan
    & $base -m venv (Join-Path $Root ".venv")
    if ($LASTEXITCODE -ne 0) { Write-Error "创建 .venv 失败"; exit 1 }
    $venvPy = Join-Path $Root ".venv\Scripts\python.exe"
    & $venvPy -m pip install -q -r (Join-Path $Root "manager\requirements.txt") -r (Join-Path $Root "client\requirements.txt")
    if ($LASTEXITCODE -ne 0) { Write-Error "安装依赖失败"; exit 1 }
    $py = $venvPy
}

if (-not $py) { Write-Error "仍然没有可用的解释器"; exit 1 }

& $py $Runner @Rest
exit $LASTEXITCODE
