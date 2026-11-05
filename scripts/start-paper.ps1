param([int]$Port = 8000)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$entryPath = Join-Path $projectRoot 'run_webapp.py'
$dataPath = Join-Path $projectRoot 'data'
New-Item -ItemType Directory -Force -Path $dataPath | Out-Null
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "Port $Port is already in use. Inspect the existing service before starting another."
}
$botProcess = Start-Process -FilePath $pythonPath -ArgumentList @('-u', ('"{0}"' -f $entryPath), '--port', $Port, '--data-dir', ('"{0}"' -f $dataPath)) -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $dataPath 'stdout.log') -RedirectStandardError (Join-Path $dataPath 'stderr.log')
$botProcess.Id | Set-Content -LiteralPath (Join-Path $dataPath 'paper.pid')
Write-Output "Started PAPER process $($botProcess.Id). Dashboard: http://127.0.0.1:$Port"
Write-Output "Access token file: $(Join-Path $dataPath 'dashboard.token')"
Write-Output 'Keep this computer awake and connected to the internet for the test.'
