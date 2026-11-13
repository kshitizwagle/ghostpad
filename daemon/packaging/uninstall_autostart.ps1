<#
.SYNOPSIS
  Remove the ghostpad logon task and stop it if running. UNTESTED on Windows.
#>
[CmdletBinding()]
param([string]$TaskName = 'ghostpad')

$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) { Write-Host "No task named '$TaskName'."; exit 0 }
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed task '$TaskName'."
