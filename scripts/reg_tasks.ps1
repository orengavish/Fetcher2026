$R = "C:\Projects\Fetcher2026"
$Py = (Get-Command python).Source
$P = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$S = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
$A1 = New-ScheduledTaskAction -Execute $Py -Argument "`"$R\trader\fetch_watchdog.py`"" -WorkingDirectory $R
$T1 = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "GalgoFetcher2026" -Force -Principal $P -Settings $S -Action $A1 -Trigger $T1 | Out-Null
Write-Host "OK: GalgoFetcher2026"
$A2 = New-ScheduledTaskAction -Execute $Py -Argument "`"$R\dashboard.py`" --real" -WorkingDirectory $R
$T2 = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "GalgoDashboard2026" -Force -Principal $P -Settings $S -Action $A2 -Trigger $T2 | Out-Null
Write-Host "OK: GalgoDashboard2026"
Get-ScheduledTask -TaskName "GalgoFetcher2026","GalgoDashboard2026" | Select-Object TaskName,State
