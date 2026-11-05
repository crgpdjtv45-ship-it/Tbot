param([int]$Port = 8000, [switch]$Terminate)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$entryPath = Join-Path $projectRoot 'run_webapp.py'
$tokenPath = Join-Path $projectRoot 'data\dashboard.token'
$headers = @{ Authorization = 'Bearer ' + (Get-Content -LiteralPath $tokenPath -Raw).Trim() }
$result = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/stop" -Method Post -Headers $headers -ContentType 'application/json' -Body '{}'
Write-Output "PAPER paused. Closed: $($result.closed.Count). Pending fresh quotes: $($result.pending.Count)."
if ($Terminate) {
    if ($result.pending.Count -gt 0) {
        throw 'Close requests are persisted; leave the service running until fresh quotes allow closure.'
    }
    $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -First 1
    $serviceProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($connection.OwningProcess)"
    if (-not $serviceProcess.CommandLine.Contains($entryPath)) {
        throw 'The listening process is not the expected project entrypoint; refusing to stop it.'
    }
    Stop-Process -Id $serviceProcess.ProcessId
    Write-Output 'Stopped the verified PAPER service process. Journal and paused state are preserved.'
}
