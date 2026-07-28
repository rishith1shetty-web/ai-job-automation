<#
.SYNOPSIS
    Build the Windows setup program from the packaged application.

.DESCRIPTION
    Runs the checks that must not be skippable before handing anything to ISCC:

      1. locate Inno Setup (and stop with a clear status if it is absent);
      2. verify the PyInstaller onedir payload exists and is complete;
      3. re-run the secrets scan over that payload -- the installer publishes whatever the
         payload contains, so a stale build that was never scanned must not be shipped;
      4. verify the pinned PostgreSQL prerequisite's checksum and signature;
      5. read the version and identity from app\version.py -- the single source;
      6. run ISCC;
      7. verify the produced installer and report its size.

    This script never installs Inno Setup and never installs PostgreSQL.

.PARAMETER SkipPostgres
    Build without embedding the PostgreSQL prerequisite. Much smaller, but the resulting
    setup cannot install a database server on a machine that has none.

.PARAMETER IsccPath
    Use a specific ISCC.exe instead of searching.

.NOTES
    Exit codes:
      0  the installer was built and verified
      2  a prerequisite is missing (payload, version source, prerequisite file)
      3  INNO SETUP IS NOT INSTALLED -- nothing was built
      4  the payload failed the secrets scan, or contains private data
      5  the pinned PostgreSQL prerequisite failed verification
      6  ISCC failed
      7  the produced installer failed verification
#>
[CmdletBinding()]
param(
    [switch]$SkipPostgres,
    [string]$IsccPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$payload = Join-Path $projectRoot "dist\RisingPathRecruiters"
$installerDir = Join-Path $projectRoot "installer"
$issFile = Join-Path $installerDir "RisingPathRecruiters.iss"
$releaseDir = Join-Path $projectRoot "release"

function Write-Step { param([string]$Message) Write-Host "[installer] $Message" }

function Invoke-Native {
    <#
        Native tools write progress to stderr even on success; with $ErrorActionPreference
        set to "Stop" PowerShell turns that into a terminating NativeCommandError. The policy
        is suspended for the call and the result judged by $LASTEXITCODE alone.
        Out-Host keeps the command's own output out of this function's return value.
    #>
    param([Parameter(Mandatory)][string]$FilePath, [string[]]$Arguments = @())
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $FilePath @Arguments | Out-Host
        return $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previous }
}

function Find-Iscc {
    if ($IsccPath) {
        if (Test-Path $IsccPath) { return $IsccPath }
        throw "ISCC.exe not found at the supplied path: $IsccPath"
    }
    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) { return $candidate }
    }
    $onPath = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    return $null
}

# ---------------------------------------------------------------- 1. Inno Setup
Write-Step "Looking for Inno Setup."
$iscc = Find-Iscc
if (-not $iscc) {
    Write-Host ""
    Write-Host "Inno Setup 6 is not installed on this computer, so no installer was built."
    Write-Host ""
    Write-Host "  Install it from the official site:  https://jrsoftware.org/isdl.php"
    Write-Host "  Then run this script again."
    Write-Host ""
    Write-Host "Everything else in this build is unaffected: the .iss source, the packaged"
    Write-Host "payload and the verified prerequisite are all in place and were checked."
    exit 3
}
Write-Step "Inno Setup: $iscc"

# ---------------------------------------------------------------- 2. payload
Write-Step "Verifying the packaged application."
if (-not (Test-Path $python)) { Write-Error "Python environment not found at $python"; exit 2 }
if (-not (Test-Path $issFile)) { Write-Error "Installer script not found: $issFile"; exit 2 }

$required = @(
    (Join-Path $payload "RisingPathRecruiters.exe"),
    (Join-Path $payload "_internal\frontend\dist\index.html"),
    (Join-Path $payload "_internal\alembic.ini"),
    (Join-Path $payload "_internal\alembic\versions")
)
$missing = @()
foreach ($item in $required) { if (-not (Test-Path $item)) { $missing += $item } }
if ($missing.Count -gt 0) {
    Write-Error "The packaged application is missing or incomplete. Run scripts\build_windows.ps1 first.`n  $($missing -join "`n  ")"
    exit 2
}

# ---------------------------------------------------------------- 3. secrets
# Re-run rather than trust that build_windows.ps1 did it: this script may be run against a
# payload built days ago, and the installer publishes whatever that payload contains.
Write-Step "Scanning the payload for secrets and user data."
$code = Invoke-Native -FilePath $python -Arguments @("-m", "scripts.scan_build_secrets", "--path", $payload)
if ($code -ne 0) {
    Write-Error "The payload contains secrets or user data. The installer was NOT built."
    exit 4
}

# ---------------------------------------------------------------- 4. prerequisite
$includePostgres = "1"
if ($SkipPostgres) {
    $includePostgres = "0"
    Write-Step "Building WITHOUT the PostgreSQL prerequisite (-SkipPostgres)."
}
else {
    Write-Step "Verifying the pinned PostgreSQL prerequisite."
    $code = Invoke-Native -FilePath "powershell.exe" -Arguments @(
        "-ExecutionPolicy", "Bypass", "-NoProfile", "-File",
        (Join-Path $PSScriptRoot "prepare_installer_prerequisites.ps1"), "-Offline"
    )
    if ($code -ne 0) {
        Write-Error "The PostgreSQL prerequisite failed verification (exit $code). The installer was NOT built."
        exit 5
    }
}

# ---------------------------------------------------------------- 5. version
Write-Step "Reading the application version."
$previous = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$versionJson = & $python -m app.version
$ErrorActionPreference = $previous
if ($LASTEXITCODE -ne 0) { Write-Error "Could not read app\version.py"; exit 2 }
$version = ($versionJson | Out-String | ConvertFrom-Json)
Write-Step "Version $($version.version), AppId $($version.app_id)"

$manifest = Get-Content (Join-Path $installerDir "prerequisites.json") -Raw | ConvertFrom-Json

# ---------------------------------------------------------------- 6. compile
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null
Write-Step "Compiling the installer (this takes a while for a ~1 GB payload)."

$definitions = @(
    "/DAppVersion=$($version.version)",
    "/DAppFileVersion=$($version.file_version)",
    "/DAppName=$($version.app_name)",
    "/DAppPublisher=$($version.publisher)",
    "/DExeName=$($version.executable_name)",
    "/DAppIdValue=$($version.inno_app_id)",
    "/DInstallDirName=$($version.install_dir_name)",
    "/DStartMenuGroup=$($version.start_menu_group)",
    "/DDataDirName=$($version.data_dir_name)",
    "/DAppDescription=$($version.description)",
    "/DAppCopyright=$($version.copyright)",
    "/DPostgresInstaller=$($manifest.postgresql.filename)",
    "/DPostgresVersion=$($manifest.postgresql.version)",
    "/DIncludePostgres=$includePostgres",
    "/DOutputDir=$releaseDir"
)
# ISCC's own output names every file it embeds. Captured, because it is the only
# authoritative answer to "what is actually inside the installer" that does not require
# unpacking the finished binary with a third-party tool.
$buildLog = Join-Path $releaseDir "$($version.executable_name)-Setup-$($version.version).build.log"

# A previous run that was interrupted can leave ISCC compiling in the background with this
# log still open. Tee-Object would then fail to create it and -- because it sits in the same
# pipeline -- take the compile down with it, reporting a misleading "ISCC failed (exit -1)".
if (Test-Path $buildLog) {
    try { Remove-Item -LiteralPath $buildLog -Force -ErrorAction Stop }
    catch {
        Write-Error @"
$buildLog is locked, which means an earlier compile is probably still running.
Wait for it to finish, or stop the ISCC process, and run this script again.
"@
        exit 6
    }
}

$previous = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $iscc @definitions $issFile 2>&1 | Tee-Object -FilePath $buildLog | Out-Host
$code = $LASTEXITCODE
$ErrorActionPreference = $previous
if ($code -ne 0) { Write-Error "ISCC failed (exit $code)."; exit 6 }

# ---------------------------------------------------------------- 6b. embedded file list
# ISCC appends the file version after the path for files it read version info from, e.g.
#   "   Compressing: C:\...\postgresql-16.9-1-windows-x64.exe   (1.0.0.0)"
# so the trailing "(n.n.n.n)" has to come off before the value is treated as a path.
$embedded = Select-String -Path $buildLog -Pattern '^\s+Compressing:\s+(.+?)(?:\s{2,}\([\d.]+\))?$' |
    ForEach-Object { $_.Matches[0].Groups[1].Value.Trim() }
$manifestList = Join-Path $releaseDir "$($version.executable_name)-Setup-$($version.version).files.txt"
# WriteAllLines rather than Set-Content -Encoding utf8: Windows PowerShell 5.1 writes a BOM,
# which becomes part of the first path for anything that reads the list back.
[System.IO.File]::WriteAllLines($manifestList, [string[]]$embedded)
Write-Step "Embedded $($embedded.Count) files (listed in $(Split-Path -Leaf $manifestList))."

# Containment: every embedded file must come from the packaged payload or be the pinned
# prerequisite. This is deliberately stronger than matching forbidden directory names --
# it excludes the source checkout, .venv, node_modules, the project's tests, .git and the
# user's runtime data in one rule, and it cannot be fooled by a name that happens to appear
# inside a third-party package (pyarrow ships its own "tests" data directory, for instance).
$payloadRoot = [System.IO.Path]::GetFullPath($payload)
$prerequisitePath = [System.IO.Path]::GetFullPath(
    (Join-Path $projectRoot "installer\prerequisites\$($manifest.postgresql.filename)"))

# @(...) because Where-Object yields $null when nothing matches, and Set-StrictMode turns
# $null.Count into a terminating error rather than 0.
$outside = @($embedded | Where-Object {
    $full = [System.IO.Path]::GetFullPath($_)
    (-not $full.StartsWith($payloadRoot, [StringComparison]::OrdinalIgnoreCase)) -and
    ($full -ne $prerequisitePath)
})
if ($outside.Count -gt 0) {
    Write-Error "The installer embeds files from outside the packaged payload:`n  $($outside -join "`n  ")"
    exit 4
}

# Belt and braces on the actual embedded list: the payload was scanned above, but these are
# the names that would be catastrophic to publish, so they are re-checked on what shipped.
$secretNames = @(
    '\\\.env$', 'database_credential', 'linkedin_state\.json', 'dice_state\.json',
    '\.pgpass$', '\.dump$', '\.bak$', '\.sqlite3?$', '\.pfx$', '\.key$'
)
$leaks = @()
foreach ($pattern in $secretNames) {
    $leaks += @($embedded | Where-Object { $_ -match $pattern })
}
if ($leaks.Count -gt 0) {
    Write-Error "The installer embeds secret or user data:`n  $($leaks -join "`n  ")"
    exit 4
}
Write-Step "Every embedded file comes from the packaged payload or the pinned prerequisite."

# ---------------------------------------------------------------- 7. verify
$expected = Join-Path $releaseDir $version.installer_filename
if (-not (Test-Path $expected)) {
    Write-Error "ISCC reported success but $expected does not exist."
    exit 7
}
$size = (Get-Item $expected).Length
if ($size -lt 100MB) {
    Write-Error "The installer is only $([math]::Round($size / 1MB, 1)) MB, which is far too small to contain the application payload."
    exit 7
}

$info = (Get-Item $expected).VersionInfo
Write-Host ""
Write-Step "Installer built."
Write-Host "  File            : $expected"
Write-Host "  Size            : $([math]::Round($size / 1MB, 1)) MB"
Write-Host "  Product version : $($info.ProductVersion)"
Write-Host "  Company         : $($info.CompanyName)"
Write-Host "  PostgreSQL      : $(if ($SkipPostgres) { 'not embedded' } else { $manifest.postgresql.version })"
Write-Host ""
Write-Step "Not code-signed. SmartScreen will warn on first run until signing is added."
exit 0
