<#
 .SYNOPSIS 手动一键拉起全部服务（开机自启由计划任务 SCG_Platform / SCG_UnbindProxy 负责）
 本脚本仅用于「不重启机器」时手动恢复：把两个看门狗作为分离的后台进程拉起后自己退出。
 看门狗本身已带端口占用保护，重复执行不会产生第二个实例。
#>
$ErrorActionPreference = "Continue"

$root      = "E:\workaaa\shengchanguanli"
$nodeExe   = "C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2\node.exe"
$watchdogPs = "$root\platform-watchdog.ps1"
$watchdogJs = "$root\src\Platform\proxy\watchdog.js"
$proxyDir   = "$root\src\Platform\proxy"

# 平台看门狗（PowerShell 后台分离）
Start-Process -FilePath "powershell.exe" `
    -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$watchdogPs`"" `
    -WindowStyle Hidden
Write-Host "已后台启动平台看门狗"

# 中转看门狗（node 后台分离）
Start-Process -FilePath $nodeExe `
    -ArgumentList $watchdogJs `
    -WorkingDirectory $proxyDir `
    -WindowStyle Hidden
Write-Host "已后台启动中转看门狗"

Write-Host "两个看门狗已分离启动，本脚本退出。可用 netstat 验证 5000/5443/5001 监听。"
