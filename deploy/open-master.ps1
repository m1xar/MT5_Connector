<#
  Opens the master terminal in portable mode, which is the only way it should
  ever be opened: without /portable it reads and writes %APPDATA%\MetaQuotes
  instead of its own folder, so brokers added there never reach the clones.

  Use this to add broker servers, then close the terminal fully before cloning -
  Config\servers.dat is written on exit.
#>
param([string]$Root = "D:\MT5")

$exe = Join-Path $Root "master\terminal64.exe"
if (-not (Test-Path $exe)) { throw "master terminal not found at $exe" }
Start-Process -FilePath $exe -ArgumentList "/portable"
