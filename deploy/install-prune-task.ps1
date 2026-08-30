<#
  Registers prune-history.ps1 as a recurring Windows scheduled task.

  Without this the price cache is never cleared and grows without bound: each
  instance keeps its own copy of every symbol it has ever charted, and one
  three-hour minute window in 2022 pulls whole years of history at ~170 MB for
  a single symbol. Across ten instances that is days, not months.

  The task runs whether or not anyone is logged in, survives reboots, and is
  safe against a live pool - files the terminals hold open are skipped, and
  everything else is re-downloaded on demand.

      .\install-prune-task.ps1                      # every 30 minutes
      .\install-prune-task.ps1 -IntervalMinutes 60
      .\install-prune-task.ps1 -KeepYears 1 -Root E:\MT5
      .\install-prune-task.ps1 -Remove

  Needs an elevated PowerShell, because the task runs as SYSTEM.
#>
param(
    [int]$IntervalMinutes = 30,
    [int]$KeepYears = 2,
    [string]$Root = "D:\MT5",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$taskName = "MT5 prune history cache"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "Removed scheduled task '$taskName'."
    } else {
        Write-Host "No scheduled task named '$taskName'."
    }
    return
}

if ($IntervalMinutes -lt 5 -or $IntervalMinutes -gt 1440) {
    throw "-IntervalMinutes must be between 5 and 1440"
}

$script = Join-Path $PSScriptRoot "prune-history.ps1"
if (-not (Test-Path $script)) {
    throw "prune-history.ps1 not found next to this script at $script"
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument ("-NoProfile -NonInteractive -ExecutionPolicy Bypass " +
               "-File `"$script`" -Apply -KeepYears $KeepYears -Root `"$Root`"")

# Repeats for as long as the machine is up: a one-day duration renewed at every
# boot is what keeps this running indefinitely without a second trigger.
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Repetition = (New-ScheduledTaskTrigger `
    -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration ([TimeSpan]::MaxValue)).Repetition

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

$principal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Prunes the MT5 terminal price cache under $Root\*\Bases." `
    -Force | Out-Null

Write-Host "Registered '$taskName': every $IntervalMinutes min, keeping $KeepYears year(s), root $Root."
Write-Host ""
Write-Host "  verify : Get-ScheduledTask -TaskName '$taskName' | Get-ScheduledTaskInfo"
Write-Host "  run now: Start-ScheduledTask -TaskName '$taskName'"
Write-Host "  remove : .\install-prune-task.ps1 -Remove"

# Prove it works now rather than at the next interval.
Start-ScheduledTask -TaskName $taskName
Write-Host ""
Write-Host "Kicked off a first run."
