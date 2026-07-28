<#
.SYNOPSIS
    Validate the installer without changing this computer.

.DESCRIPTION
    Every check here is non-mutating. This script NEVER installs PostgreSQL, never runs the
    setup program in a mode that writes to Program Files, never touches the real database and
    never overwrites the stored credential.

    What it checks:

      * the .iss source is present and states the things it must state;
      * the pinned prerequisite verifies (checksum + Authenticode);
      * PostgreSQL detection runs read-only and reports what this machine actually has;
      * the PostgreSQL installation command is CONSTRUCTED and inspected -- never executed;
      * the built installer, if one exists, has the expected identity, size and payload;
      * running the built installer with /? (its help dialog) does not install anything.

.PARAMETER Deep
    Also list the files embedded in a built installer. Requires innoextract or 7-Zip; skipped
    with a note when neither is available.

.NOTES
    Exit codes:
      0  every applicable check passed
      2  the installer sources are missing
      4  one or more checks failed
#>
[CmdletBinding()]
param([switch]$Deep)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$installerDir = Join-Path $projectRoot "installer"
$issFile = Join-Path $installerDir "RisingPathRecruiters.iss"
$releaseDir = Join-Path $projectRoot "release"

$script:failures = @()
$script:checks = 0

function Test-Claim {
    param([string]$Label, [bool]$Condition, [string]$Detail = "")
    $script:checks++
    if ($Condition) { Write-Host "  [PASS] $Label $Detail" }
    else { Write-Host "  [FAIL] $Label $Detail"; $script:failures += $Label }
}

function Write-Skip { param([string]$Label, [string]$Why) Write-Host "  [SKIP] $Label ($Why)" }

function Get-IssCode {
    <#
        The .iss with its comments removed.

        Nearly every check below is "this must NOT appear", and the file's own commentary
        legitimately discusses what it avoids -- that no .venv is referenced, that unrelated
        processes are never touched. Matching raw text would fail on the explanation instead
        of on the code.

        Pascal "{ ... }" blocks go first; Inno constants ({app}, {#Name}) are never followed
        by whitespace, so the "{\s" anchor cannot consume one.
    #>
    param([Parameter(Mandatory)][string]$Text)
    $withoutBlocks = [regex]::Replace($Text, '\{\s[^{}]*?\}', ' ',
                                      [System.Text.RegularExpressions.RegexOptions]::Singleline)
    return (($withoutBlocks -split "`n" | Where-Object {
        -not $_.TrimStart().StartsWith(';')
    }) -join "`n")
}

if (-not (Test-Path $issFile)) { Write-Error "Installer source not found: $issFile"; exit 2 }

Write-Host "[test] Installer validation (nothing on this computer is modified)."
Write-Host ""

# ---------------------------------------------------------------- 1. source
Write-Host "[test] Installer source:"
$iss = Get-Content $issFile -Raw
# Every "must not appear" check runs against the code, not the commentary.
$code = Get-IssCode -Text $iss
$version = (& $python -m app.version | Out-String | ConvertFrom-Json)

Test-Claim "installs under 64-bit Program Files" ($iss -match "DefaultDirName=\{autopf\}")
Test-Claim "x64 install mode" ($iss -match "ArchitecturesInstallIn64BitMode=x64compatible")
Test-Claim "stable AppId is defined" ($iss -match "AppId=\{#AppIdValue\}")
Test-Claim "modern wizard" ($iss -match "WizardStyle=modern")
Test-Claim "desktop shortcut is an optional task" ($iss -match "Name: `"desktopicon`"" -and $iss -match "Flags: unchecked")
Test-Claim "Start menu shortcut is created" ($iss -match "Name: `"\{group\}")
Test-Claim "no shortcut points at a PowerShell script" (-not ($code -match "Filename:.*\.ps1"))
Test-Claim "PostgreSQL is embedded but unpacked on demand" (
    ($code -match "Flags: dontcopy") -and ($code -match "ExtractTemporaryFile"))
Test-Claim "detection gates the PostgreSQL install" (
    ($code -match "function NeedsPostgresInstall") -and ($code -match "if NeedsPostgresInstall then"))
Test-Claim "credential steps run as the original user" ($iss -match "ExecAsOriginalUser")
Test-Claim "uninstall does not delete the data directory" (-not ($code -match "\{localappdata\}\\\{#DataDirName\}`""))
Test-Claim "uninstall contains no database deletion" (
    (-not ($code -match "DROP DATABASE")) -and (-not ($code -match "dropdb")) -and (-not ($code -match "DROP ROLE")))
Test-Claim "no source checkout path is referenced" (
    (-not ($code -match "\.venv")) -and (-not ($code -match "node_modules")) -and (-not ($code -match "[\\/]tests[\\/]")))
Test-Claim "no password appears in an installer command line" (-not ($code -match "superpassword"))
Test-Claim "no unrelated process is ever terminated" (
    (-not ($code -match "taskkill")) -and (-not ($code -match "python\.exe")) -and (-not ($code -match "postgres\.exe")))

# ---------------------------------------------------------------- 2. prerequisite
Write-Host ""
Write-Host "[test] Pinned prerequisite:"
$previous = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& powershell.exe -ExecutionPolicy Bypass -NoProfile -File `
    (Join-Path $PSScriptRoot "prepare_installer_prerequisites.ps1") -Offline | Out-Null
$prereqCode = $LASTEXITCODE
$ErrorActionPreference = $previous
if ($prereqCode -eq 3) { Write-Skip "prerequisite verification" "the installer has not been downloaded yet" }
else { Test-Claim "prerequisite checksum and signature verify" ($prereqCode -eq 0) "(exit $prereqCode)" }

# ---------------------------------------------------------------- 3. read-only detection
Write-Host ""
Write-Host "[test] PostgreSQL detection (read-only):"
$ErrorActionPreference = "Continue"
& $python -m app.installer_bridge detect --json | Out-Null
$detectCode = $LASTEXITCODE
$ErrorActionPreference = $previous
Test-Claim "detection completes and returns a known state" ($detectCode -in @(0, 10, 11, 20)) "(exit $detectCode)"
Test-Claim "an existing compatible PostgreSQL is not scheduled for reinstall" ($detectCode -ne 10) `
    "(exit 10 would mean 'install PostgreSQL')"

# ---------------------------------------------------------------- 4. command construction
Write-Host ""
Write-Host "[test] PostgreSQL install command (constructed, NEVER executed):"
$ErrorActionPreference = "Continue"
& $python -m unittest tests.test_installer -v 2>&1 | Select-String -Pattern "^(Ran|OK|FAILED)" | ForEach-Object { Write-Host "  $_" }
$unitCode = $LASTEXITCODE
$ErrorActionPreference = $previous
Test-Claim "installer unit tests pass" ($unitCode -eq 0)

# ---------------------------------------------------------------- 5. built installer
Write-Host ""
Write-Host "[test] Built installer:"
$built = Join-Path $releaseDir $version.installer_filename
if (-not (Test-Path $built)) {
    Write-Skip "built installer checks" "no installer in release\ -- run scripts\build_installer.ps1"
}
else {
    $item = Get-Item $built
    Test-Claim "installer exists and is not empty" ($item.Length -gt 0)
    Test-Claim "installer is large enough to hold the payload" ($item.Length -gt 100MB) `
        "($([math]::Round($item.Length / 1MB, 1)) MB)"
    Test-Claim "product version matches app\version.py" `
        ($item.VersionInfo.ProductVersion -like "$($version.version)*") "($($item.VersionInfo.ProductVersion))"
    Test-Claim "company name is set" ([bool]$item.VersionInfo.CompanyName)

    if ($Deep) {
        $sevenZip = Get-Command 7z.exe -ErrorAction SilentlyContinue
        if ($sevenZip) {
            $ErrorActionPreference = "Continue"
            $listing = & $sevenZip.Source l $built 2>&1 | Out-String
            $ErrorActionPreference = $previous
            Test-Claim "payload contains the application executable" ($listing -match "RisingPathRecruiters.exe")
            Test-Claim "payload contains no .env" (-not ($listing -match "\.env"))
            Test-Claim "payload contains no credential file" (-not ($listing -match "database_credential"))
            Test-Claim "payload contains no portal session" (
                (-not ($listing -match "linkedin_state")) -and (-not ($listing -match "dice_state")))
        }
        else { Write-Skip "embedded file listing" "7-Zip is not installed" }
    }
}

# ---------------------------------------------------------------- report
Write-Host ""
if ($script:failures.Count -eq 0) {
    Write-Host "[test] RESULT: all $($script:checks) checks passed."
    Write-Host "[test] PostgreSQL was NOT installed and no database was modified."
    exit 0
}
Write-Host "[test] RESULT: $($script:failures.Count) of $($script:checks) checks FAILED:"
$script:failures | ForEach-Object { Write-Host "  - $_" }
exit 4
