<#
  Where the terminal pool lives, for every script in this folder.

  The README works against D:\MT5, but the pool follows whatever machine it was
  built on and the connector's own .env already records where it went:
  MT5_API_TERMINAL_PATHS points at the clones, and the master sits beside them
  one level up. So an explicit -Root always wins, .env answers when there is
  none - which is what a double-clicked .cmd passes - and D:\MT5 is only the
  last resort.

  Dot-source it:  . "$PSScriptRoot\_root.ps1"
#>

function Resolve-MT5Root {
    param([string]$Root)

    if ($Root) { return $Root.TrimEnd('\') }

    $envFile = Join-Path (Split-Path $PSScriptRoot -Parent) ".env"
    if (Test-Path $envFile) {
        $line = Select-String -Path $envFile -Pattern '^MT5_API_TERMINAL_PATHS=' |
                Select-Object -First 1
        if ($line) {
            $first = ($line.Line -replace '^MT5_API_TERMINAL_PATHS=', '').Split(';')[0].Trim()
            # ...\<root>\t1\terminal64.exe - the root is two levels up.
            if ($first) { return (Split-Path (Split-Path $first -Parent) -Parent) }
        }
    }

    return "D:\MT5"
}

function Get-MT5MasterExe {
    param([string]$Root)

    # Join-Path resolves the drive and throws its own error first, which buries
    # the caller's - and the caller's is the diagnosis, since a wrong root is
    # the whole failure mode here.
    return "$($Root.TrimEnd('\'))\master\terminal64.exe"
}
