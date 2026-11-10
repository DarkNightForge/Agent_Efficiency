[CmdletBinding()]
param(
    [Parameter(Mandatory)][string] $ReleaseRoot,
    [Parameter(Mandatory)][string] $InstalledRoot,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-fA-F]{40}$')][string] $ExpectedCommit,
    [Parameter(Mandatory)][string] $ExpectedVersion
)

$ErrorActionPreference = 'Stop'
$sourceCheckout = (Resolve-Path -LiteralPath $ReleaseRoot).Path
$installedPackage = (Resolve-Path -LiteralPath $InstalledRoot).Path
$sourcePackage = Join-Path $sourceCheckout 'plugins/agent-efficiency'
$actualCommit = git --no-optional-locks -C $sourceCheckout rev-parse HEAD
if ($LASTEXITCODE -ne 0 -or $actualCommit -ne $ExpectedCommit) {
    throw 'The release checkout is not at the expected commit.'
}
$sourceChanges = git --no-optional-locks -C $sourceCheckout status --porcelain
if ($LASTEXITCODE -ne 0 -or $sourceChanges) {
    throw 'The release checkout is dirty or Git could not check it.'
}

foreach ($package in @($sourcePackage, $installedPackage)) {
    $manifestPath = Join-Path $package '.codex-plugin/plugin.json'
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.name -ne 'agent-efficiency' -or $manifest.version -ne $ExpectedVersion) {
        throw "Unexpected Agent Efficiency identity or version in $manifestPath"
    }
}

$prefix = 'plugins/agent-efficiency/'
$trackedFiles = @(git --no-optional-locks -C $sourceCheckout ls-files -- $prefix)
if ($LASTEXITCODE -ne 0 -or $trackedFiles.Count -eq 0) {
    throw 'Git did not return the tracked release payload.'
}
$expectedFiles = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
foreach ($trackedFile in $trackedFiles) {
    if (!$trackedFile.StartsWith($prefix, [StringComparison]::Ordinal)) {
        throw "Unexpected path returned by Git: $trackedFile"
    }
    $relativePath = $trackedFile.Substring($prefix.Length)
    $null = $expectedFiles.Add($relativePath)
    $sourceFile = Join-Path $sourcePackage $relativePath
    $installedFile = Join-Path $installedPackage $relativePath
    $sourceHash = (Get-FileHash -LiteralPath $sourceFile -Algorithm SHA256).Hash
    $installedHash = (Get-FileHash -LiteralPath $installedFile -Algorithm SHA256).Hash
    if ($sourceHash -ne $installedHash) {
        throw "Installed payload mismatch: $relativePath"
    }
}

$generated = @()
foreach ($file in Get-ChildItem -LiteralPath $installedPackage -File -Force -Recurse) {
    $relativePath = $file.FullName.Substring($installedPackage.TrimEnd('\', '/').Length + 1).Replace('\', '/')
    if ($expectedFiles.Contains($relativePath)) { continue }
    if ($relativePath -match '^scripts/__pycache__/[^/]+\.pyc$') {
        $generated += $relativePath
    } else {
        throw "Unexpected installed payload file: $relativePath"
    }
}

[pscustomobject]@{
    Commit = $actualCommit
    Version = $ExpectedVersion
    MatchedFiles = $expectedFiles.Count
    GeneratedBytecodeFiles = $generated.Count
}
