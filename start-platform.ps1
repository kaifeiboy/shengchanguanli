# start-platform.ps1
# PowerShell 5.1 compatible launcher.
# - Kills stale processes on port 5000 and cloudflared
# - Clean rebuilds the Platform project
# - Starts the ASP.NET Core service on port 5000 (hidden)
# - Starts cloudflared quick tunnel (hidden)
# - Captures the live tunnel URL into data\tunnel_url.txt
# Child processes keep running detached after this script exits.

$ErrorActionPreference = "Stop"

$root        = "E:\workaaa\shengchanguanli"
$dataDir     = "$root\data"
$projDir     = "$root\src\Platform"
$binDir      = "$projDir\bin\Debug\net8.0"
$objDir      = "$projDir\obj\Debug\net8.0"
$tunnelExe   = "$root\cloudflared.exe"
$urlFile     = "$dataDir\tunnel_url.txt"
$serverLog   = "$dataDir\server_run.log"
$serverErr   = "$dataDir\server_run_err.log"
$tunnelLog   = "$dataDir\tunnel_run.log"
$tunnelErr   = "$dataDir\tunnel_run_err.log"

# Ensure data dir exists
if (-not (Test-Path $dataDir)) { New-Item -ItemType Directory -Path $dataDir -Force | Out-Null }

# ---------------------------------------------------------------
# 1. Kill stale processes occupying port 5000 and any cloudflared
# ---------------------------------------------------------------
$netstat = netstat -ano
foreach ($line in $netstat) {
    if ($line -match "0\.0\.0\.0:5000\s+.*LISTENING") {
        $pidCol = ($line -split "\s+")[-1]
        if ($pidCol -match "^\d+$") {
            try { Stop-Process -Id $pidCol -Force -ErrorAction SilentlyContinue } catch {}
        }
    }
}
Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

# Small grace for ports to free / processes to die
Start-Sleep -Seconds 2

# ---------------------------------------------------------------
# 2. Clean rebuild the Platform project
# ---------------------------------------------------------------
# 注意：本机 safe-delete 包装对目录删除会 fail-closed（抛错），直接 Remove-Item
# 会让整条脚本中断。这里用 try/catch 包住：删不掉（如被 safe-delete 拦）就跳过——
# dotnet build 本就增量重建改动文件，删 bin/obj 只是强制全清、非必需。
if (Test-Path $binDir) { try { Remove-Item -Recurse -Force $binDir -ErrorAction SilentlyContinue } catch { Write-Host "[warn] skip bin cleanup" } }
if (Test-Path $objDir) { try { Remove-Item -Recurse -Force $objDir -ErrorAction SilentlyContinue } catch { Write-Host "[warn] skip obj cleanup" } }

$buildProc = Start-Process -FilePath "dotnet" -ArgumentList "build","-v","quiet" `
    -WorkingDirectory $projDir -Wait -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput "$dataDir\build_out.log" -RedirectStandardError "$dataDir\build_err.log"
if ($buildProc.ExitCode -ne 0) {
    Write-Error "dotnet build failed with exit code $($buildProc.ExitCode). See $dataDir\build_err.log"
    exit 1
}

# ---------------------------------------------------------------
# 3. Start the service in background (hidden), wait for port 5000
# ---------------------------------------------------------------
Start-Process -FilePath "dotnet" -ArgumentList "run","--no-build" `
    -WorkingDirectory $projDir `
    -RedirectStandardOutput $serverLog -RedirectStandardError $serverErr `
    -WindowStyle Hidden

$portReady = $false
for ($i = 0; $i -lt 40; $i++) {
    $ns = netstat -ano
    foreach ($line in $ns) {
        if ($line -match "0\.0\.0\.0:5000\s+.*LISTENING") { $portReady = $true; break }
    }
    if ($portReady) { break }
    Start-Sleep -Seconds 1
}
if (-not $portReady) {
    Write-Error "Timed out waiting for port 5000 to be listening. See $serverErr"
    exit 1
}

# ---------------------------------------------------------------
# 4. Start cloudflared in background (hidden), capture live URL
# ---------------------------------------------------------------
# Use cloudflared's native --logfile so the URL is flushed straight to
# tunnel_run.log (reliable). Avoid piping stdout via Start-Process, which
# can buffer and lose the line if the process is torn down.
# Use 127.0.0.1 (not "localhost"): app binds 0.0.0.0:5000 (IPv4 only);
# "localhost" resolves to ::1 first in Go and cloudflared fails to reach the
# origin (HTTP 530). Port stays 5000.
Start-Process -FilePath $tunnelExe -ArgumentList "tunnel","--url","http://127.0.0.1:5000","--no-autoupdate","--logfile",$tunnelLog `
    -WindowStyle Hidden

$tunnelUrl = $null
for ($i = 0; $i -lt 30; $i++) {
    if (Test-Path $tunnelLog) {
        $content = Get-Content -Path $tunnelLog -Raw
        if ($content -match "https://[a-z0-9.-]+\.trycloudflare\.com") {
            $tunnelUrl = $matches[0].Trim()
            break
        }
    }
    Start-Sleep -Seconds 1
}

# Write the URL (no BOM, UTF-8, single line) if found
if ($tunnelUrl) {
    # Use a StreamWriter to guarantee no BOM and no trailing newline junk
    $sw = New-Object System.IO.StreamWriter($urlFile, $false, (New-Object System.Text.UTF8Encoding($false)))
    $sw.Write($tunnelUrl)
    $sw.Close()
}

# Script exits; child processes (dotnet + cloudflared) remain detached.
exit 0
