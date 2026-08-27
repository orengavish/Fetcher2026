# DEPRECATED: prefer install_scheduler.ps1 (interactive-user principal). This variant
# registers the tasks as SYSTEM, whose %USERPROFILE% doesn't resolve to your profile, so
# the watchdog can't find IBC\config.ini and Gateway auto-restart silently fails -- the
# 2026-07-29..08-17 19-day outage. Kept only for the "must run with no user logged in"
# case; if you use it, make IBC's config path absolute first.
# ExecutionTimeLimit 0 (was 4 min): fetch_watchdog.py/dashboard.py run forever; a 4-min
# limit killed the healthy process every 4 min so a fresh one spawned every 5.
$R = "C:\Projects\Fetcher2026"
$Py = (Get-Command python).Source
$P = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$S = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 0) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
$A1 = New-ScheduledTaskAction -Execute $Py -Argument "`"$R\trader\fetch_watchdog.py`"" -WorkingDirectory $R
$T1 = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "GalgoFetcher2026" -Force -Principal $P -Settings $S -Action $A1 -Trigger $T1 | Out-Null
Write-Host "OK: GalgoFetcher2026"
$A2 = New-ScheduledTaskAction -Execute $Py -Argument "`"$R\dashboard.py`" --real" -WorkingDirectory $R
$T2 = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "GalgoDashboard2026" -Force -Principal $P -Settings $S -Action $A2 -Trigger $T2 | Out-Null
Write-Host "OK: GalgoDashboard2026"
Get-ScheduledTask -TaskName "GalgoFetcher2026","GalgoDashboard2026" | Select-Object TaskName,State
