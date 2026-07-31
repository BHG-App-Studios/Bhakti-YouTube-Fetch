[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Workspace
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$sourceArchive = Join-Path $Workspace 'Firefox\Firefox.zip'

if (-not (Test-Path -LiteralPath $sourceArchive -PathType Leaf)) {
    throw "Firefox profile archive was not found: $sourceArchive"
}

$archiveHeader = Get-Content -LiteralPath $sourceArchive -TotalCount 1 -ErrorAction Stop
if ($archiveHeader -match '^version https://git-lfs.github.com/spec/') {
    throw 'Firefox.zip is a Git LFS pointer instead of the real archive.'
}

if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw 'winget is not available on this Windows runner.'
}

# --- Step 1: Uninstall existing Firefox & clear old profile data ---

Write-Host "Uninstalling any existing Firefox..."
$uninstallPaths = @(
    Join-Path $env:ProgramFiles 'Mozilla Firefox\uninstall\helper.exe'
)
if (${env:ProgramFiles(x86)}) {
    $uninstallPaths += Join-Path ${env:ProgramFiles(x86)} 'Mozilla Firefox\uninstall\helper.exe'
}

foreach ($helper in $uninstallPaths) {
    if (Test-Path -LiteralPath $helper) {
        Write-Host "Running uninstaller silently: $helper"
        Start-Process -FilePath $helper -ArgumentList '/S' -Wait -NoNewWindow
    }
}

$roamingDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::ApplicationData)
$mozillaDirectory = Join-Path $roamingDirectory 'Mozilla'

if (Test-Path -LiteralPath $mozillaDirectory) {
    Write-Host "Deleting old Mozilla data at $mozillaDirectory"
    Remove-Item -LiteralPath $mozillaDirectory -Recurse -Force -ErrorAction SilentlyContinue
}

# --- Step 2: Fresh Install Firefox 153.0.1 ---

Write-Host 'Installing Firefox 153.0.1 via winget...'
$wingetArguments = @(
    'install'
    '-e'
    '--id', 'Mozilla.Firefox'
    '-v', '153.0.1'
    '--silent'
    '--accept-package-agreements'
    '--accept-source-agreements'
    '--disable-interactivity'
)

Write-Host "Running: winget $($wingetArguments -join ' ')"
$process = Start-Process `
    -FilePath 'winget.exe' `
    -ArgumentList $wingetArguments `
    -NoNewWindow `
    -Wait `
    -PassThru

if ($process.ExitCode -ne 0) {
    throw "Firefox installation failed with winget exit code $($process.ExitCode)."
}

$firefoxCandidates = @(
    (Join-Path $env:ProgramFiles 'Mozilla Firefox\firefox.exe')
)

if (${env:ProgramFiles(x86)}) {
    $firefoxCandidates += Join-Path ${env:ProgramFiles(x86)} 'Mozilla Firefox\firefox.exe'
}

$firefoxExecutable = $firefoxCandidates |
    Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
    Select-Object -First 1

if (-not $firefoxExecutable) {
    throw 'Firefox executable was not found after install.'
}

$firefoxVersion = (Get-Item -LiteralPath $firefoxExecutable).VersionInfo.ProductVersion
Write-Host "Firefox executable: $firefoxExecutable"
Write-Host "Firefox version: $firefoxVersion"

# --- Step 3: Extract the backed-up profile ---

New-Item -ItemType Directory -Path $mozillaDirectory -Force | Out-Null

Write-Host "Extracting Firefox profile archive to $mozillaDirectory"
Expand-Archive -LiteralPath $sourceArchive -DestinationPath $mozillaDirectory -Force

$expectedProfilesIni = Join-Path $mozillaDirectory 'Firefox\profiles.ini'
if (-not (Test-Path -LiteralPath $expectedProfilesIni -PathType Leaf)) {
    throw "Expected profiles.ini was not found after extraction: $expectedProfilesIni"
}

Write-Host 'Firefox profile restored successfully.'

if ($env:GITHUB_OUTPUT) {
    "firefox_executable=$firefoxExecutable" |
        Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    "firefox_version=$firefoxVersion" |
        Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
}
