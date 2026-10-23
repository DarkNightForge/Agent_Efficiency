param(
    [Parameter(Mandatory = $true)]
    [string]$Script,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ScriptArgs
)

$ErrorActionPreference = 'Stop'

function Test-AgentEfficiencyPython {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,
        [string[]]$PrefixArgs = @()
    )

    $nonce = [Guid]::NewGuid().ToString('N')
    $probe = "import sys; print('$nonce' if sys.version_info.major == 3 else '')"
    try {
        $output = @(& $Executable @PrefixArgs -c $probe 2>$null)
        $code = $LASTEXITCODE
    }
    catch {
        return $false
    }
    return $code -eq 0 -and ($output -join "`n").Trim() -eq $nonce
}

$candidates = @()

if ($env:AGENT_EFFICIENCY_PYTHON) {
    if (Test-Path -LiteralPath $env:AGENT_EFFICIENCY_PYTHON -PathType Leaf) {
        $candidates += [PSCustomObject]@{ Executable = $env:AGENT_EFFICIENCY_PYTHON; PrefixArgs = @() }
    }
    else {
        [Console]::Error.WriteLine('AGENT_EFFICIENCY_PYTHON does not point to a file; trying other Python 3 runtimes.')
    }
}

$pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
if ($pyLauncher) {
    $candidates += [PSCustomObject]@{ Executable = $pyLauncher.Source; PrefixArgs = @('-3') }
}

$userHome = if ($env:USERPROFILE) { $env:USERPROFILE } else { $env:HOME }
$runtimeRoot = Join-Path $userHome '.cache\codex-runtimes'
$bundledCandidates = @(
    (Join-Path $runtimeRoot 'codex-primary-runtime\dependencies\python\python.exe'),
    (Join-Path $runtimeRoot 'codex-primary-runtime/dependencies/python/bin/python3')
)
if (Test-Path -LiteralPath $runtimeRoot -PathType Container) {
    $bundledCandidates += Get-ChildItem -LiteralPath $runtimeRoot -Directory -ErrorAction SilentlyContinue |
        ForEach-Object {
            Join-Path $_.FullName 'dependencies\python\python.exe'
            Join-Path $_.FullName 'dependencies/python/bin/python3'
        }
}
foreach ($candidate in $bundledCandidates | Select-Object -Unique) {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        $candidates += [PSCustomObject]@{ Executable = $candidate; PrefixArgs = @() }
    }
}

foreach ($commandName in @('python3', 'python')) {
    $pythonCommand = Get-Command $commandName -ErrorAction SilentlyContinue
    if ($pythonCommand -and $pythonCommand.Source -notlike '*\WindowsApps\*') {
        $candidates += [PSCustomObject]@{ Executable = $pythonCommand.Source; PrefixArgs = @() }
    }
}

$seen = @{}
foreach ($candidate in $candidates) {
    $identity = "$($candidate.Executable)|$($candidate.PrefixArgs -join ' ')"
    if ($seen.ContainsKey($identity)) {
        continue
    }
    $seen[$identity] = $true
    if (Test-AgentEfficiencyPython -Executable $candidate.Executable -PrefixArgs $candidate.PrefixArgs) {
        $prefixArgs = @($candidate.PrefixArgs)
        & $candidate.Executable @prefixArgs $Script @ScriptArgs
        exit $LASTEXITCODE
    }
}

[Console]::Error.WriteLine(
    'Agent Efficiency needs Python 3. Set AGENT_EFFICIENCY_PYTHON or install Python 3.'
)
exit 127
