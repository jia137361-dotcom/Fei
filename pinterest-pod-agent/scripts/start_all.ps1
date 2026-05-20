# Start all Pinterest POD Agent backend services.
# Usage: powershell -File scripts\start_all.ps1

param(
    [switch]$NoStopExisting
)

$ProjectRoot = "c:\nanobot\pinterest-pod-agent"
$VenvRoot   = "c:\nanobot\.venv"

# Production defaults. Set these environment variables before running the
# script when you need a dry run or a paused scheduler.
if (-not $env:SCHEDULER_ENABLED) { $env:SCHEDULER_ENABLED = "true" }
if (-not $env:SCHEDULER_DRY_RUN) { $env:SCHEDULER_DRY_RUN = "false" }
if (-not $env:SCHEDULER_AUTO_DISPATCH_ENABLED) { $env:SCHEDULER_AUTO_DISPATCH_ENABLED = "false" }

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Pinterest POD Agent - Start All" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

if (-not $NoStopExisting) {
    Write-Host "[0/3] Stopping old project FastAPI/Celery processes..." -ForegroundColor Yellow
    $oldCelery = Get-CimInstance Win32_Process -Filter "name = 'celery.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -like "*app.celery_app*" -or
            $_.CommandLine -like "*pinterest-pod-agent*"
        }
    foreach ($proc in $oldCelery) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "  Stopped old Celery process: $($proc.ProcessId)" -ForegroundColor DarkYellow
    }

    $fastApiPids = Get-NetTCPConnection -LocalPort 8900 -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($processId in $fastApiPids) {
        if ($processId) {
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
            Write-Host "  Stopped old process on port 8900: $processId" -ForegroundColor DarkYellow
        }
    }
}

# -- FastAPI --------------------------------------------------
Write-Host "[1/3] Starting FastAPI (port 8900)..." -ForegroundColor Green
Start-Process -FilePath "$VenvRoot\Scripts\python.exe" `
    -ArgumentList "-m uvicorn app.main:app --host 127.0.0.1 --port 8900" `
    -WorkingDirectory $ProjectRoot `
    -WindowStyle Normal

# -- Celery Worker --------------------------------------------
Write-Host "[2/3] Starting Celery Worker..." -ForegroundColor Green
Start-Process -FilePath "$VenvRoot\Scripts\celery.exe" `
    -ArgumentList "-A app.celery_app worker -Q publish,media,engagement,trend --loglevel=info --concurrency=1 --pool=solo" `
    -WorkingDirectory $ProjectRoot `
    -WindowStyle Normal

# -- Celery Beat ----------------------------------------------
Write-Host "[3/3] Starting Celery Beat..." -ForegroundColor Green
Start-Process -FilePath "$VenvRoot\Scripts\celery.exe" `
    -ArgumentList "-A app.celery_app beat --loglevel=info" `
    -WorkingDirectory $ProjectRoot `
    -WindowStyle Normal

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Services started in separate windows" -ForegroundColor Cyan
Write-Host "  FastAPI       -> http://127.0.0.1:8900" -ForegroundColor Yellow
Write-Host "  Celery Worker -> queues: publish/media/engagement/trend" -ForegroundColor Yellow
Write-Host "  Celery Beat   -> scheduler + stale-task reclaim" -ForegroundColor Yellow
Write-Host "  Dry run       -> $env:SCHEDULER_DRY_RUN" -ForegroundColor Yellow
Write-Host "  Auto dispatch -> $env:SCHEDULER_AUTO_DISPATCH_ENABLED" -ForegroundColor Yellow
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next: powershell -File scripts\nanobot.ps1 `"start today's operations`"" -ForegroundColor Magenta
