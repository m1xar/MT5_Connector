<#
  Builds the terminal pool by copying the cleaned master.

  Every instance is a portable install: its data lives beside terminal64.exe
  rather than in %APPDATA%\MetaQuotes, so a clone is a directory copy and the
  instances never share state.

  The master must already know every broker you intend to use. Add them there
  before cloning - `mt5.login` cannot reach a server the terminal has never
  heard of, and `Config\servers.dat` is only written when the terminal exits.

      .\clone.ps1 -Count 6
      .\clone.ps1 -Count 6 -Force            # rebuild existing clones
      .\clone.ps1 -Count 6 -Root E:\MT5
#>
param(
    [int]$Count = 6,
    [switch]$Force,
    [string]$Root = "D:\MT5"
)

$ErrorActionPreference = "Stop"
$master = Join-Path $Root "master"

if (-not (Test-Path (Join-Path $master "terminal64.exe"))) {
    throw "master not found at $master"
}
if (Get-Process -Name "terminal64" -ErrorAction SilentlyContinue) {
    throw "a terminal64.exe is still running - close every instance before cloning"
}

# Per-instance state: caches and logs that must not be carried into a clone.
$transient = @("logs", "Bases", "Tester", "llm-agent", "MQL5\logs", "MQL5\Files\Temp")

$paths = @()
for ($i = 1; $i -le $Count; $i++) {
    $dst = Join-Path $Root "t$i"

    if (Test-Path $dst) {
        if (-not $Force) { throw "$dst already exists - pass -Force to overwrite" }
        Remove-Item $dst -Recurse -Force
    }

    robocopy $master $dst /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed for $dst (exit $LASTEXITCODE)" }

    foreach ($t in $transient) {
        $p = Join-Path $dst $t
        if (Test-Path $p) {
            Get-ChildItem $p -Recurse -Force | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    Remove-Item (Join-Path $dst "Config\dnsperf.dat") -Force -ErrorAction SilentlyContinue

    $paths += Join-Path $dst "terminal64.exe"
    $mb = [math]::Round(((Get-ChildItem $dst -Recurse -File | Measure-Object Length -Sum).Sum)/1MB, 1)
    Write-Host ("  t{0} -> {1}  ({2} MB)" -f $i, $dst, $mb)
}

Write-Host ""
Write-Host "Put this in the connector's .env :"
Write-Host ""
Write-Host ("MT5_API_TERMINAL_PATHS=" + ($paths -join ";"))
