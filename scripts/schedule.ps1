# Install / remove the Windows scheduled tasks that run the daily paper-trading cycle.
#   14:00 local (Europe/Zurich) = 08:00 ET (09:00 ET in the few DST-mismatch weeks): before the 09:28 ET
#          opening-auction cutoff -> submits market-on-open orders for the day
#   23:30 local = 17:30 ET, after the close + the 1h settle buffer -> syncs fills, equity and the model ledger (no orders are sent then)
# Tasks run as the current user while logged on; "StartWhenAvailable" catches up after sleep/shutdown.
param([switch]$Install, [switch]$Remove)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$cmd = Join-Path $root "scripts\daily.cmd"
$names = @("JMW-Systematic daily 1400", "JMW-Systematic sync 2330")
if ($Remove) {
    foreach ($n in $names) { Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue }
    Write-Output "Removed scheduled tasks: $($names -join ', ')"
    exit 0
}
if (-not $Install) { Write-Output "Usage: schedule.ps1 -Install | -Remove"; exit 1 }
$action = New-ScheduledTaskAction -Execute $cmd -WorkingDirectory $root
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $names[0] -Action $action -Settings $settings -Principal $principal -Force `
    -Trigger (New-ScheduledTaskTrigger -Daily -At "14:00") `
    -Description "Alpaca PAPER trading: refresh data, stops, month-end rebalance (market-on-open)" | Out-Null
Register-ScheduledTask -TaskName $names[1] -Action $action -Settings $settings -Principal $principal -Force `
    -Trigger (New-ScheduledTaskTrigger -Daily -At "23:30") `
    -Description "Alpaca PAPER trading: sync fills, equity and model ledger after the US close" | Out-Null
Get-ScheduledTask -TaskName $names | Select-Object TaskName, State | Format-Table -AutoSize | Out-String | Write-Output
