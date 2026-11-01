$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

# A working PATH Python does not imply that the optional py launcher exists.
# Probe candidates before starting the CLI; never let a stale launcher default
# hide a supported interpreter.
function Find-HarnessPython {
    $Candidates = @(
        @{ Command = 'python'; Prefix = @() },
        @{ Command = 'python3'; Prefix = @() },
        @{ Command = 'py'; Prefix = @('-3') }
    )
    foreach ($Version in @('3.12', '3.13', '3.11', '3.14', '3.15')) {
        $Candidates += @{ Command = 'py'; Prefix = @("-$Version") }
    }
    $Probe = 'import sys; sys.exit(3) if sys.version_info < (3, 11) else print(sys.executable)'
    foreach ($Candidate in $Candidates) {
        $Command = Get-Command $Candidate.Command -CommandType Application -ErrorAction SilentlyContinue
        if (-not $Command) { continue }
        try {
            $Prefix = $Candidate.Prefix
            $Output = @(& $Command.Source @Prefix -I -c $Probe 2>$null)
            if ($LASTEXITCODE -eq 0 -and $Output.Count -eq 1 -and
                (Test-Path -LiteralPath "$($Output[0])" -PathType Leaf)) {
                return "$($Output[0])"
            }
        } catch { continue }
    }
    throw 'Python 3.11+ is required. Install it and make python or py available on PATH.'
}

$Python = Find-HarnessPython
& $Python -m harness.cli @args
exit $LASTEXITCODE
