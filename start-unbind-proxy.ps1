# start-unbind-proxy.ps1
# 启动维修解绑模块的本地 Node.js 中转服务（带看门狗，崩溃自动重启）。
# 配置在 src/Platform/proxy/config.json：token 由 PC 登录目标系统后取得。

$ErrorActionPreference = "Stop"

$root    = "E:\workaaa\shengchanguanli"
$nodeExe = "C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2\node.exe"
$watchJs = "$root\src\Platform\proxy\watchdog.js"

if (-not (Test-Path $nodeExe)) {
    Write-Error "未找到 Node.js：$nodeExe"
    exit 1
}
if (-not (Test-Path $watchJs)) {
    Write-Error "未找到看门狗脚本：$watchJs"
    exit 1
}

$config = "$root\src\Platform\proxy\config.json"
$cfgJson = Get-Content $config -Raw | ConvertFrom-Json
if (-not $cfgJson.token -or $cfgJson.token.Trim() -eq '') {
    Write-Host "⚠️  警告：config.json 中的 token 字段为空。" -ForegroundColor Yellow
    Write-Host "     请先在 PC 浏览器登录 https://10.10.10.68:7443/，" -ForegroundColor Yellow
    Write-Host "     复制 JWT 粘贴到 $config 的 token 字段后再启动。" -ForegroundColor Yellow
}

Write-Host "启动维修解绑中转服务（看门狗模式，崩溃自动重启）..." -ForegroundColor Cyan
& $nodeExe $watchJs
