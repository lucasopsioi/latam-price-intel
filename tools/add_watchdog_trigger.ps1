# Add a repeating trigger to the LatamIntelHub scheduled task so the service
# self-heals when the supervisor process dies.
#
# Why this is needed (2026-09-07):
#   The task had only a LogonTrigger. RestartCount=999 only fires when the task
#   *fails*; when the whole process tree vanished the task counted as completed,
#   so nothing restarted it. The service stayed dead from ~01:45 to ~09:49 local
#   and the 07:30 daily collection never ran -- one full day of data lost.
#
# MultipleInstances is already IgnoreNew, so a repeating trigger is safe:
#   supervisor alive -> new instance ignored; supervisor dead -> it is started.
#
# ASCII only on purpose (this machine's ANSI codepage is cp936; a .ps1 with
# non-ASCII needs a BOM and is easy to corrupt through shell pipelines).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\add_watchdog_trigger.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\add_watchdog_trigger.ps1 -Remove

param([switch]$Remove)

$TaskName = 'LatamIntelHub'
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop

$logon = $task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' }
if (-not $logon) { Write-Host '[WARN] no logon trigger found; keeping whatever exists'; }

if ($Remove) {
    if ($logon) {
        Set-ScheduledTask -TaskName $TaskName -Trigger $logon | Out-Null
        Write-Host '[OK] repeating trigger removed; logon trigger kept'
    } else {
        Write-Host '[SKIP] no logon trigger to fall back to; nothing changed'
    }
    exit 0
}

$already = $task.Triggers | Where-Object {
    $_.Repetition -and $_.Repetition.Interval -eq 'PT15M'
}
if ($already) { Write-Host '[SKIP] repeating trigger already present'; exit 0 }

# Start 2 minutes from now, then repeat every 15 minutes forever.
$t2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
        -RepetitionInterval (New-TimeSpan -Minutes 15)

$triggers = @()
if ($logon) { $triggers += $logon }
$triggers += $t2

Set-ScheduledTask -TaskName $TaskName -Trigger $triggers | Out-Null

$after = (Get-ScheduledTask -TaskName $TaskName).Triggers
Write-Host ('[OK] triggers now: ' + (($after | ForEach-Object { $_.CimClass.CimClassName }) -join ', '))
foreach ($x in $after) {
    if ($x.Repetition -and $x.Repetition.Interval) {
        Write-Host ('     repetition interval = ' + $x.Repetition.Interval)
    }
}
Write-Host '[NOTE] MultipleInstances=IgnoreNew keeps this from starting a second supervisor.'
