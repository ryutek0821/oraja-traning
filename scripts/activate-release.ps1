param(
    [Parameter(Mandatory = $true)]
    [string]$BundleDir,
    [string]$DataRoot = "$env:LOCALAPPDATA\oraja-training"
)

$ErrorActionPreference = "Stop"
$resolvedBundle = (Resolve-Path $BundleDir).Path
$required = @(
    "manifest.json",
    "session.json",
    "table\recommend\header.json",
    "table\recommend\score.json",
    "table\today\header.json",
    "table\today\score.json"
)
foreach ($relative in $required) {
    if (-not (Test-Path (Join-Path $resolvedBundle $relative) -PathType Leaf)) {
        throw "Bundle is missing $relative"
    }
}

$manifest = Get-Content (Join-Path $resolvedBundle "manifest.json") -Raw | ConvertFrom-Json
$releaseName = "$($manifest.menu_date)-$($manifest.seed)"
$releases = Join-Path $DataRoot "releases"
$destination = Join-Path $releases $releaseName
New-Item -ItemType Directory -Force -Path $releases | Out-Null
if (-not (Test-Path $destination)) {
    Copy-Item -Path $resolvedBundle -Destination $destination -Recurse
}

$pointer = Join-Path $DataRoot "active-release.txt"
$temporary = "$pointer.tmp"
Set-Content -Path $temporary -Value $destination -NoNewline -Encoding UTF8
Move-Item -Path $temporary -Destination $pointer -Force
Write-Output $destination
