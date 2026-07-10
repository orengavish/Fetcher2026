# Fetcher2026 Task Scheduler Setup
# Run as Administrator:
#   Start-Process powershell -Verb RunAs -ArgumentList '-File C:\Projects\Fetcher2026\scripts\install_scheduler.ps1'

$ProjectRoot = "C:\Projects\Fetcher2026"
$Python      = (Get-Command python -ErrorAction Stop).Source

# --- Task 1: Watchdog (every 5 min, restarts fetch_scheduler if it dies) ---
$A1 = New-ScheduledTaskAction -Execute $Python `
        -Argument "`"$ProjectRoot\trader\fetch_watchdog.py`"" `
        -WorkingDirectory $ProjectRoot
$T1 = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
$S1 = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 4) `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -StartWhenAvailable $true
$P1 = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName "GalgoFetcher2026" `
    -Action $A1 -Trigger $T1 -Settings $S1 -Principal $P1 -Force | Out-Null
Write-Host "OK: GalgoFetcher2026 (watchdog every 5 min)"

# --- Task 2: Dashboard (every 5 min, exits immediately if port 5050 already bound) ---
$A2 = New-ScheduledTaskAction -Execute $Python `
        -Argument "`"$ProjectRoot\dashboard.py`" --real" `
        -WorkingDirectory $ProjectRoot
$T2 = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
$S2 = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 4) `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -StartWhenAvailable $true
Register-ScheduledTask -TaskName "GalgoDashboard2026" `
    -Action $A2 -Trigger $T2 -Settings $S2 -Principal $P1 -Force | Out-Null
Write-Host "OK: GalgoDashboard2026 (dashboard every 5 min)"

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
