<#
  Opens the master terminal in portable mode, which is the only way it should
  ever be opened: without /portable it reads and writes %APPDATA%\MetaQuotes
  instead of its own folder, so brokers added there never reach the clones.

  Use this to add broker servers, then close the terminal fully before cloning -
  Config\servers.dat is written on exit.

  -Root is optional; without it the pool root comes from .env. See _root.ps1.
#>
param([string]$Root)

# Without this a missing drive makes Join-Path fail *non-terminating*: $exe
# stays $null and the "master terminal not found" throw below never fires, so
# the run ends in four unrelated binding errors instead of the one that says
# what is wrong.
$ErrorActionPreference = "Stop"

. "$PSScriptRoot\_root.ps1"
$Root = Resolve-MT5Root $Root

$exe = Get-MT5MasterExe $Root
if (-not (Test-Path $exe)) { throw "master terminal not found at $exe" }
Start-Process -FilePath $exe -ArgumentList "/portable"
Write-Host "opened $exe /portable"
