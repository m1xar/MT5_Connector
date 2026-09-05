<#
  Prunes the price history the terminals cache under Bases\.

  Every instance keeps its own copy of every symbol it has ever charted, and
  that copy is not small: one three-hour minute window in 2022 pulls whole
  years of history and lands at ~170 MB for a single symbol. Ten instances
  across a handful of brokers add up fast.

  It is safe to delete. The terminal re-downloads on demand (measured at ~27s
  for one symbol), files it currently has open are simply skipped by Windows,
  and a running terminal keeps working while this runs - so this can sit on a
  schedule without coordinating with the pool.

  It is also cheap to delete, because the service only prices a position's
  MAE/MFE once: later syncs skip positions that already carry a figure, so the
  deep history behind them is never asked for again.

  The service does this itself after every sync, for the instance that ran it
  (MT5_API_PRUNE_CACHE_AFTER_SYNC). This script is what is left for the cases
  it cannot reach: an instance the pool is not currently syncing through, a
  one-off reclaim with the pool stopped, or a dry run to see the size first.

      .\prune-history.ps1                 # report only, delete nothing
      .\prune-history.ps1 -Apply          # keep this year and last
      .\prune-history.ps1 -Apply -KeepYears 1

  -Root is optional; without it the pool root comes from .env. See _root.ps1.
#>
param(
    [switch]$Apply,
    [int]$KeepYears = 2,
    [string]$Root
)

$ErrorActionPreference = "Stop"

. "$PSScriptRoot\_root.ps1"
$Root = Resolve-MT5Root $Root

# Enumerating a root that is not there reports 0 MB freed, which reads as "the
# cache was already clean" rather than "this looked in the wrong place".
if (-not (Test-Path $Root)) { throw "terminal root not found at $Root" }

$cutoff = (Get-Date).Year - $KeepYears + 1
Write-Host ("Keeping history from $cutoff onwards" +
            $(if ($Apply) { "" } else { "   (dry run - pass -Apply to delete)" }))
Write-Host ""

$totalFreed = 0.0
$totalLocked = 0

foreach ($instance in Get-ChildItem $Root -Directory | Where-Object { $_.Name -ne "master" }) {
    $history = Join-Path $instance.FullName "Bases"
    if (-not (Test-Path $history)) { continue }

    $freed = 0.0
    $locked = 0
    $touchedSymbols = New-Object System.Collections.Generic.HashSet[string]

    # Year files are <year>.hcc directly under each symbol directory.
    foreach ($file in Get-ChildItem $history -Recurse -File -Filter "*.hcc" -ErrorAction SilentlyContinue) {
        $year = 0
        if (-not [int]::TryParse([IO.Path]::GetFileNameWithoutExtension($file.Name), [ref]$year)) { continue }
        if ($year -ge $cutoff) { continue }

        $mb = $file.Length / 1MB
        if (-not $Apply) { $freed += $mb; [void]$touchedSymbols.Add($file.DirectoryName); continue }
        try {
            Remove-Item $file.FullName -Force
            $freed += $mb
            [void]$touchedSymbols.Add($file.DirectoryName)
        } catch {
            $locked++
        }
    }

    # The built timeseries under cache\ mirrors the year files, so it only
    # needs dropping for symbols that actually lost one. The terminal rebuilds
    # it the next time that symbol is charted.
    foreach ($symbolDir in $touchedSymbols) {
        $cache = Join-Path $symbolDir "cache"
        if (-not (Test-Path $cache)) { continue }
        foreach ($file in Get-ChildItem $cache -File -ErrorAction SilentlyContinue) {
            $mb = $file.Length / 1MB
            if (-not $Apply) { $freed += $mb; continue }
            try {
                Remove-Item $file.FullName -Force
                $freed += $mb
            } catch {
                $locked++
            }
        }
    }

    if ($freed -gt 0 -or $locked -gt 0) {
        $verb = if ($Apply) { "freed" } else { "would free" }
        Write-Host ("  {0,-10} {1} {2,8:N1} MB{3}" -f $instance.Name, $verb, $freed,
                    $(if ($locked) { "   ($locked file(s) in use, skipped)" } else { "" }))
    }
    $totalFreed += $freed
    $totalLocked += $locked
}

Write-Host ""
Write-Host ("Total: {0:N1} MB{1}" -f $totalFreed,
            $(if ($totalLocked) { "   ($totalLocked file(s) in use - they go on the next run)" } else { "" }))
