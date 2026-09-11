# cleanup_redundant.ps1
# Moves redundant / expired files to the Recycle Bin (recoverable, NOT permanent delete).
# Default: PREVIEW ONLY - lists what would be moved, does nothing.
# Use  -Execute  to actually move the targets to Recycle Bin.
#
# IMPORTANT: Run this on the LOCAL Windows machine (not inside the agent sandbox),
# because the sandbox has no Recycle Bin and will refuse/soft-fail the deletion.
# On the local machine, files go to the Recycle Bin and stay recoverable.

param([switch]$Execute)

$ErrorActionPreference = 'Stop'
$root = "E:\workaaa\shengchanguanli"
$targets = @()

# --- Category A: session temp diagnostic files (temp/_*.txt) ---
$tmpDir = Join-Path $root "temp"
if (Test-Path $tmpDir) {
    Get-ChildItem $tmpDir -File -Filter "_*.txt" -ErrorAction SilentlyContinue | ForEach-Object { $targets += $_.FullName }
}

# --- Category B: root .md reports from the 2026-08-29 unreliable refactor cluster ---
# Selected by date filter (ASCII-safe, avoids hardcoding Chinese filenames).
$refDate = [datetime]::Parse("2026-08-29")
Get-ChildItem $root -File -Filter "*.md" -ErrorAction SilentlyContinue | Where-Object {
    $_.LastWriteTime.Date -eq $refDate
} | ForEach-Object { $targets += $_.FullName }

# --- Category C: archived debug directory ---
$arch = Join-Path $root "_archived_debug"
if (Test-Path $arch) { $targets += $arch }

# --- Preview ---
Write-Host "=== PREVIEW: $($targets.Count) target(s) ==="
foreach ($t in $targets) {
    if (-not (Test-Path $t)) { Write-Host "  [MISSING] $t"; continue }
    $item = Get-Item $t
    if ($item.PSIsContainer) {
        $dirSz = (Get-ChildItem $t -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
        Write-Host ("  [DIR ] {0} ({1} MB)" -f $t, [math]::Round(($dirSz/1MB),1))
    } else {
        Write-Host ("  [FILE] {0} ({1} bytes)" -f $t, $item.Length)
    }
}

if (-not $Execute) {
    Write-Host ""
    Write-Host "PREVIEW ONLY. Review the list above, then re-run with -Execute to move these to Recycle Bin."
    exit 0
}

# --- Execute: move to Recycle Bin ---
Add-Type -AssemblyName Microsoft.VisualBasic
$moved = 0; $failed = 0
foreach ($t in $targets) {
    if (-not (Test-Path $t)) { Write-Host "SKIP (missing): $t"; continue }
    try {
        $item = Get-Item $t
        if ($item.PSIsContainer) {
            [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($t, "OnlyErrorDialogs", "SendToRecycleBin")
        } else {
            [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($t, "OnlyErrorDialogs", "SendToRecycleBin")
        }
        Write-Host "MOVED: $t"
        $moved++
    } catch {
        Write-Host ("FAILED: {0} -> {1}" -f $t, $_.Exception.Message)
        $failed++
    }
}
Write-Host ""
Write-Host ("Done. Moved={0} Failed={1}" -f $moved, $failed)
