# 一键部署新修复（缓存头改 no-cache）
# 用法：以管理员权限运行 PowerShell，执行此脚本
# .\\_deploy.ps1

$ErrorActionPreference = "Stop"
$projectRoot = "E:\workaaa\shengchanguanli\src\Platform"

Write-Host "=== 1. 停止 Platform + watchdog node ==="
Get-Process -Name Platform -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
$watchNodes = Get-Process node -ErrorAction SilentlyContinue | Where-Object {
    try { (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine -match 'watchdog' } catch { $false }
}
$watchNodes | ForEach-Object { Write-Host "  杀 watchdog node PID=$($_.Id)"; Stop-Process -Id $_.Id -Force }
Start-Sleep -Seconds 2

Write-Host "=== 2. 重新编译 ==="
Set- & -LiteralPath $projectRoot
& dotnet build Platform.csproj -c Debug --no-incremental --nologo
if ($LASTEXITCODE -ne 0) { Write-Host "编译失败，请检查"; exit 1 }

Write-Host "=== 3. 验证 obj dll 含新字符串 ==="
$objDll = Join-Path $projectRoot "obj\Debug\net8.0\Platform.dll"
$match = & grep -c "must-revalidate" $objDll
if ($match -lt 1) { Write-Host "⚠️ 编译后 dll 不含新字符串，请检查源码"; exit 1 }
Write-Host "  ✅ obj dll 已含 must-revalidate"

Write-Host "=== 4. 复制到 bin（watchdog 会自动重启 Platform 加载新 dll）==="
$binDll = Join-Path $projectRoot "bin\Debug\net8.0\Platform.dll"
$binExe = Join-Path $projectRoot "bin\Debug\net8.0\Platform.exe"
$objExe = Join-Path $projectRoot "obj\Debug\net8.0\apphost.exe"
Copy-Item -Path $objDll -Destination $binDll -Force
Copy-Item -Path $objExe -Destination $binExe -Force
Write-Host "  ✅ 已复制 bin"

Write-Host "=== 5. 等 watchdog 重启 Platform（约 10s）==="
Start-Sleep -Seconds 10
$running = Get-Process -Name Platform -ErrorAction SilentlyContinue
if ($running) {
    Write-Host "  ✅ Platform PID=$($running.Id) 已运行"
} else {
    Write-Host "  ⚠️ Platform 未运行，请手动启动"
}

Write-Host "=== 6. 验证 HTTP Cache-Control ==="
Start-Sleep -Seconds 3
$headers = (curl -s -D - -o \$null "http://127.0.0.1:5000/api/defecthistory/image?row=38&i=0\&thumb=true") 2>\$null | Select-String "Cache-Control"
if ($headers -match "no-cache") {
    Write-Host "  ✅ Cache-Control: $($headers.Line)"
} else {
    Write-Host "  ❌ Cache-Control 未生效: $($headers.Line)"
}