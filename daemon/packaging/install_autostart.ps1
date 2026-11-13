<#
.SYNOPSIS
  Register a Task Scheduler task that starts ghostpad at logon (hidden). UNTESTED on Windows.
.EXAMPLE
  .\install_autostart.ps1 -Token SECRET
  .\install_autostart.ps1 -Exe C:\Tools\ghostpad.exe -Port 47800 -Bind 0.0.0.0
.NOTES
  Runs as the current user, limited rights, at that user's logon. No admin needed (not
  verified; ViGEmBus must already be installed). The token ends up in the task definition,
  readable by that user. Remove with uninstall_autostart.ps1.
#>
[CmdletBinding()]
param(
  [string]$Exe = (Join-Path $PSScriptRoot 'dist\ghostpad.exe'),
  [string]$TaskName = 'ghostpad',
  [int]$Port = 47800,
  [string]$Bind = '0.0.0.0',
  [string]$Token = '',
  [string]$ExtraArgs = ''
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Exe)) { throw "ghostpad exe not found: $Exe (run build_exe.py, or pass -Exe)" }
$Exe = (Resolve-Path -LiteralPath $Exe).Path

$argList = @('--port', $Port, '--bind', $Bind)
if ($Token) { $argList += @('--token', $Token) }
$argString = ($argList -join ' ') + $(if ($ExtraArgs) { " $ExtraArgs" } else { '' })

$user = "$env:USERDOMAIN\$env:USERNAME"
$action    = New-ScheduledTaskAction -Execute $Exe -Argument $argString -WorkingDirectory (Split-Path $Exe)
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
  -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Registered task '$TaskName': $Exe $argString"
Write-Host "Starts at next logon. Start now: Start-ScheduledTask -TaskName $TaskName"
Write-Host "Note: 'Hidden' hides the task in the UI; for no console window build with --noconsole."
