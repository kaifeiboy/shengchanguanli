$ErrorActionPreference = 'SilentlyContinue'
try {
    $r = Invoke-WebRequest -Uri 'http://localhost:5000/api/modules' -UseBasicParsing -TimeoutSec 5
    Write-Host "Status: $($r.StatusCode)"
    Write-Host "Content: $($r.Content)"
} catch {
    Write-Host "FAILED: $($_.Exception.Message)"
}
