<#
.SYNOPSIS
  Remove the ghostpad logon task and stop the daemon it started.
.NOTES
  Stop-ScheduledTask returns before the process exits (about 1 s later for the one-folder
  build) and only ends the process the task launched. For a --onefile build that is the
  PyInstaller bootloader; its daemon child kept running and serving (seen on Windows 11).
  So after a short wait, any process still running the task's exe is stopped too.
#>
[CmdletBinding()]
param([string]$TaskName = 'ghostpad')

$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) { Write-Host "No task named '$TaskName'."; exit 0 }
$exe = $task.Actions | Select-Object -First 1 -ExpandProperty Execute
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed task '$TaskName'."

if ($exe) {
  $full = [System.IO.Path]::GetFullPath($exe)
  $left = $null
  foreach ($i in 1..10) {
    $left = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path -ieq $full }
    if (-not $left) { break }
    Start-Sleep -Milliseconds 300
  }
  if ($left) {
    $left | Stop-Process -Force -ErrorAction SilentlyContinue
    Write-Host "Stopped $(@($left).Count) leftover process(es) running $full."
  }
}
