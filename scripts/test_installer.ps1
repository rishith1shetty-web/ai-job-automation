<#
.SYNOPSIS
    Validate the installer without changing this computer.

.DESCRIPTION
    Every check here is non-mutating. This script NEVER installs PostgreSQL, never runs the
    setup program in a mode that writes to Program Files, never touches the real database and
    never overwrites the stored credential.

    It validates the SHIPPED deployment model: PostgreSQL 16 is installed by hand, before
    setup runs. Rising Path Recruiters never downloads, bundles, verifies or installs a
    database server -- it detects one, requires it to be running on port 5432, asks for the
    postgres password on a masked page, and provisions its own role, database, migrations and
    DPAPI-protected credential.

    What it checks:

      * the .iss source is present and states the things it must state;
      * NO PostgreSQL installer is embedded and no automatic-installation code remains;
      * setup requires PostgreSQL 16 and refuses to install anything without it;
      * the masked password page and the DPAPI handoff are present and carry no plain text;
      * an upgrade keeps the existing database;
      * PostgreSQL detection runs read-only and reports what this machine actually has;
      * the built installer, if one exists, has the expected identity, size and payload.

.PARAMETER Deep
    Also list the files embedded in a built installer. Requires 7-Zip; skipped with a note
    when it is not available.

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
        processes are never touched, that an earlier version embedded a PostgreSQL installer.
        Matching raw text would fail on the explanation instead of on the code.

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
Test-Claim "credential steps run as the original user" ($iss -match "ExecAsOriginalUser")
Test-Claim "uninstall does not delete the data directory" (-not ($code -match "\{localappdata\}\\\{#DataDirName\}`""))
Test-Claim "uninstall contains no database deletion" (
    (-not ($code -match "DROP DATABASE")) -and (-not ($code -match "dropdb")) -and (-not ($code -match "DROP ROLE")))
Test-Claim "no source checkout path is referenced" (
    (-not ($code -match "\.venv")) -and (-not ($code -match "node_modules")) -and (-not ($code -match "[\\/]tests[\\/]")))
Test-Claim "no unrelated process is ever terminated" (
    (-not ($code -match "taskkill")) -and (-not ($code -match "python\.exe")) -and (-not ($code -match "postgres\.exe")))

# ------------------------------------------ 2. the automatic-installation flow is GONE
Write-Host ""
Write-Host "[test] No automatic PostgreSQL installation:"

# Every identifier the removed flow used. If any of them comes back, so has the flow.
#
# Some entries are deliberately truncated: they are substrings of the real identifier, which
# is all a -match needs, and writing the full form would place the forbidden literal into
# THIS file. The repository's own guards in tests/test_installer.py assert that no script
# contains those literals, and they are right to.
$removed = @("InstallPostgres", "NeedsPostgresInstall", "prepare-postgres", "IncludePostgres",
             "PostgresInstaller", "ExtractTemporaryFile", "dontcopy", "optionfile",
             "superpassword")
$revived = @($removed | Where-Object { $code -match [regex]::Escape($_) })
Test-Claim "no automatic-installation code remains" ($revived.Count -eq 0) "($($revived -join ', '))"

$fileSection = [regex]::Match($iss, '(?ms)^\[Files\](.*?)^\[').Value
Test-Claim "no PostgreSQL installer is embedded" (-not ($fileSection -match '(?i)postgresql.*\.exe'))
$sources = @([regex]::Matches($fileSection, 'Source: "([^"]+)"') | ForEach-Object { $_.Groups[1].Value })
Test-Claim "every embedded file comes from the packaged payload" (
    ($sources.Count -gt 0) -and (@($sources | Where-Object { -not $_.StartsWith('{#PayloadDir}') }).Count -eq 0))

$prereqDir = Join-Path $installerDir "prerequisites"
if (Test-Path $prereqDir) {
    $leftovers = @(Get-ChildItem $prereqDir -Filter "postgresql*.exe" -ErrorAction SilentlyContinue)
    if ($leftovers.Count -gt 0) {
        Write-Skip "downloaded prerequisite left on disk" "$($leftovers.Count) file(s) in installer\prerequisites -- unused by the build, safe to delete"
    }
}

$buildScript = Get-Content (Join-Path $PSScriptRoot "build_installer.ps1") -Raw
Test-Claim "the build refuses to embed a PostgreSQL installer" ($buildScript -match 'postgresBinaries')
Test-Claim "the build no longer verifies a pinned download" (-not ($buildScript -match 'prepare_installer'))

# ------------------------------------------ 3. PostgreSQL 16 is REQUIRED, and checked first
Write-Host ""
Write-Host "[test] PostgreSQL 16 requirement:"
Test-Claim "a required major version is defined" ($iss -match "RequiredPostgresMajor")
Test-Claim "the supported port is 5432" ($iss -match '#define PostgresPort "5432"')
Test-Claim "the gate runs before anything is installed" (
    ($code -match "function InitializeSetup") -and
    ($code -match "(?s)function InitializeSetup.*?RequirePostgres"))
Test-Claim "the version is read from the 64-bit registry view" (
    ($code -match "HKLM64") -and (-not ($code -match "RegQueryStringValue\(HKLM,")))

# The four states setup must refuse, each with its own message.
Test-Claim "refuses when PostgreSQL is absent" ($code -match "PostgreSQL is not installed on this computer")
Test-Claim "refuses when the major version is too old" ($code -match "PostgresMajor < ")
Test-Claim "refuses when the service is not running" ($code -match "ServiceIsRunning")
Test-Claim "refuses when nothing answers on the port" ($code -match "ServerIsAccepting")
Test-Claim "tells the user how to fix it" (
    ($code -match "x86-64") -and ($code -match "run this setup again"))

# ------------------------------------------ 4. password page and secure handoff
Write-Host ""
Write-Host "[test] Masked password page and secure handoff:"
Test-Claim "a masked password field is used" ($code -match "TPasswordEdit")
Test-Claim "the prompt is the agreed wording" (
    $code -match "Enter the PostgreSQL administrator password created during PostgreSQL installation")
Test-Claim "the fixed connection details are shown" (
    ($code -match "User name: postgres") -and ($code -match "Port: \{#PostgresPort\}"))
Test-Claim "the password is DPAPI-protected before it reaches disk" (
    ($code -match "CryptProtectData") -and ($code -match "CryptUnprotectData"))
Test-Claim "the wizard keeps ciphertext, never the typed value" ($code -match "HandoffHex")
Test-Claim "the password never reaches a command line or the environment" (
    (-not ($code -match "PGPASSWORD")) -and
    (-not ($code -match "AI_JOB_AUTOMATION_ADMIN_PASSWORD")) -and
    (-not ($code -match "SetEnvironment")))
Test-Claim "a rejected password can be retried" ($code -match "AuthenticateWithRetries")
Test-Claim "a failed installation is rolled back" ($code -match "RollBackInstallation")
Test-Claim "the handoff is destroyed on every exit path" (
    ($code -match "DestroyHandoff") -and ($code -match "procedure DeinitializeSetup"))

# ------------------------------------------ 5. upgrades keep the existing database
Write-Host ""
Write-Host "[test] Upgrade behaviour:"
Test-Claim "an existing installation is asked before provisioning" (
    $code -match "RunAppAsUser\('--installer status'")
Test-Claim "an already-provisioned database is kept" ($code -match "The existing database was kept")
# Bound the search to the section itself. A lazy ".*?" across the whole file happily runs
# past the section end and matches a constant in [Code], which is how this first reported a
# false failure against a correct [UninstallDelete].
$uninstallSection = [regex]::Match($iss, '(?ms)^\[UninstallDelete\](.*?)^\[').Value
Test-Claim "uninstall preserves the user's data directory" (
    ($uninstallSection.Length -gt 0) -and (-not ($uninstallSection -match '(?i)localappdata')))

# ------------------------------------------ 6. read-only detection on this machine
Write-Host ""
Write-Host "[test] PostgreSQL detection (read-only):"
$previous = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $python -m app.installer_bridge detect --json | Out-Null
$detectCode = $LASTEXITCODE
& $python -m app.installer_bridge require-postgres --json | Out-Null
$requireCode = $LASTEXITCODE
$ErrorActionPreference = $previous

# 0 usable, 10 missing, 11 port taken by something else, 20 installed but stopped,
# 23 present but too old. Anything else means detection itself is broken.
Test-Claim "detection completes and returns a known state" ($detectCode -in @(0, 10, 11, 20)) "(exit $detectCode)"
Test-Claim "the requirement gate returns a known state" ($requireCode -in @(0, 10, 11, 20, 23)) "(exit $requireCode)"
if ($requireCode -eq 0) {
    Test-Claim "this machine satisfies the PostgreSQL requirement" $true "(exit 0)"
}
else {
    Write-Skip "requirement satisfied on this machine" "exit $requireCode -- setup would stop here and instruct the user"
}

# ------------------------------------------ 7. unit tests
Write-Host ""
Write-Host "[test] Installer unit tests:"
$ErrorActionPreference = "Continue"
& $python -m unittest tests.test_installer -v 2>&1 | Select-String -Pattern "^(Ran|OK|FAILED)" | ForEach-Object { Write-Host "  $_" }
$unitCode = $LASTEXITCODE
$ErrorActionPreference = $previous
Test-Claim "installer unit tests pass" ($unitCode -eq 0)

# ------------------------------------------ 8. built installer
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

    # The build writes the list of everything ISCC embedded. It is the authoritative answer
    # to "what is actually inside", and it costs nothing to re-check here.
    $fileList = [System.IO.Path]::ChangeExtension($built, $null) + "files.txt"
    if (Test-Path $fileList) {
        $embedded = Get-Content $fileList
        Test-Claim "no PostgreSQL installer was embedded" (
            @($embedded | Where-Object { $_ -match '(?i)postgresql.*\.exe$' }).Count -eq 0)
        Test-Claim "the React build was embedded" (
            @($embedded | Where-Object { $_ -match 'frontend\\dist\\index\.html$' }).Count -eq 1)
    }
    else { Write-Skip "embedded file list" "no .files.txt beside the installer" }

    if ($Deep) {
        $sevenZip = Get-Command 7z.exe -ErrorAction SilentlyContinue
        if ($sevenZip) {
            $ErrorActionPreference = "Continue"
            $listing = & $sevenZip.Source l $built 2>&1 | Out-String
            $ErrorActionPreference = $previous
            Test-Claim "payload contains the application executable" ($listing -match "RisingPathRecruiters.exe")
            Test-Claim "payload contains no PostgreSQL installer" (-not ($listing -match "(?i)postgresql.*\.exe"))
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
