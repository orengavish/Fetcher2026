# Fetcher2026 Task Scheduler Setup
# Run as Administrator:
#   Start-Process powershell -Verb RunAs -ArgumentList '-File C:\Projects\Fetcher2026\scripts\install_scheduler.ps1'

$ProjectRoot = "C:\Projects\Fetcher2026"
$Python      = (Get-Command python -ErrorAction Stop).Source
$PythonW     = Join-Path (Split-Path $Python) "pythonw.exe"

# --- Task 1: Watchdog (every 5 min, restarts fetch_scheduler if it dies) ---
# Principal must be the interactive user, not SYSTEM: SYSTEM can't resolve
# %USERPROFILE%-relative IBC\config.ini, which caused a real 19-day silent outage.
# pythonw (not python) so the watchdog itself has no console window; it already
# logs everything to logs/fetch_watchdog.log via lib.logger.
$A1 = New-ScheduledTaskAction -Execute $PythonW `
        -Argument "`"$ProjectRoot\trader\fetch_watchdog.py`"" `
        -WorkingDirectory $ProjectRoot
# fetch_watchdog.py is meant to run forever (self-checks hourly). The hourly trigger +
# IgnoreNew is a keep-alive: relaunch only if it actually died (up to 1h to notice).
# ExecutionTimeLimit 0 = "do not stop" -- a 4-min limit here killed the healthy watchdog
# every 4 min, so Task Scheduler spawned a fresh one (new console window) every 5 min.
$T1 = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 3650)
$S1 = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 0) `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -StartWhenAvailable
$P1 = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
try {
    Register-ScheduledTask -TaskName "GalgoFetcher2026" `
        -Action $A1 -Trigger $T1 -Settings $S1 -Principal $P1 -Force -ErrorAction Stop | Out-Null
    Write-Host "OK: GalgoFetcher2026 (watchdog, windowless, checked hourly)"
} catch {
    Write-Host "FAILED: GalgoFetcher2026 -- $_"
}

# --- Task 2: Dashboard (every 5 min, exits immediately if port 5050 already bound) ---
$A2 = New-ScheduledTaskAction -Execute $Python `
        -Argument "`"$ProjectRoot\dashboard.py`" --real" `
        -WorkingDirectory $ProjectRoot
# Same rationale as Task 1: dashboard.py is a long-lived Flask server; keep-alive re-fire
# + no execution-time limit so it isn't killed and restarted every few minutes.
$T2 = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
$S2 = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 0) `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -StartWhenAvailable
try {
    Register-ScheduledTask -TaskName "GalgoDashboard2026" `
        -Action $A2 -Trigger $T2 -Settings $S2 -Principal $P1 -Force -ErrorAction Stop | Out-Null
    Write-Host "OK: GalgoDashboard2026 (dashboard every 5 min)"
} catch {
    Write-Host "FAILED: GalgoDashboard2026 -- $_"
}

# --- Firewall rule for port 5050 ---
if (-not (Get-NetFirewallRule -DisplayName "Fetcher2026 Dashboard" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "Fetcher2026 Dashboard" `
        -Direction Inbound -Protocol TCP -LocalPort 5050 -Action Allow | Out-Null
    Write-Host "OK: Firewall rule added for port 5050"
} else {
    Write-Host "OK: Firewall rule already exists for port 5050"
}

Write-Host ""
Write-Host "All done. Fetcher2026 is now supervised by Task Scheduler."
Write-Host "Dashboard: http://localhost:5050"
