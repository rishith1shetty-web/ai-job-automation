<#
.SYNOPSIS
    Fetch and verify the pinned PostgreSQL installer the setup program embeds.

.DESCRIPTION
    Reproducible: the version, the download URL, the expected SHA-256 and the expected
    Authenticode signer all come from installer\prerequisites.json. Nothing here decides for
    itself which version is "latest", and nothing here ever runs the installer it downloads.

    Two independent checks have to pass:

      * SHA-256 against the pinned value -- proves the file is byte-identical to the one that
        was reviewed when the pin was established;
      * Authenticode -- proves EDB published it, whatever route it arrived by. This is the
        stronger check: a checksum can only ever confirm a decision someone already made.

    Offline by default when a verified copy is already present: it is re-verified and
    nothing is downloaded.

.PARAMETER Force
    Download again even when a verified copy is present.

.PARAMETER Offline
    Never download. Fails if a verified copy is not already present.

.PARAMETER Pin
    Establish the trust anchor: download, verify the signature, and PRINT the checksum for a
    human to record in prerequisites.json. Deliberately does not write the value itself --
    pinning is a decision, not a build step, and a script that updated its own expected
    checksum would verify nothing.

.NOTES
    Exit codes:
      0  the verified prerequisite is present (or, with -Pin, was fetched and reported)
      2  the manifest is missing, unreadable, or has no pinned checksum
      3  the download failed
      4  CHECKSUM MISMATCH -- the file is not the one that was pinned
      5  the Authenticode signature is missing, invalid, or from another publisher
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$Offline,
    [switch]$Pin
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$installerDir = Join-Path $projectRoot "installer"
$manifestFile = Join-Path $installerDir "prerequisites.json"
$prerequisiteDir = Join-Path $installerDir "prerequisites"

function Write-Step { param([string]$Message) Write-Host "[prereq] $Message" }

# ---------------------------------------------------------------- 1. manifest
if (-not (Test-Path $manifestFile)) {
    Write-Error "Manifest not found: $manifestFile"
    exit 2
}
$manifest = Get-Content $manifestFile -Raw | ConvertFrom-Json
$entry = $manifest.postgresql

foreach ($field in @("version", "filename", "url")) {
    if (-not $entry.$field) {
        Write-Error "prerequisites.json is missing 'postgresql.$field'."
        exit 2
    }
}

$expectedHash = $entry.sha256
$expectedSigner = $entry.authenticode_subject
$target = Join-Path $prerequisiteDir $entry.filename

Write-Step "PostgreSQL $($entry.version)"
Write-Step "Source    : $($entry.url)"
Write-Step "Target    : $target"

if (-not $entry.url.StartsWith("https://")) {
    Write-Error "The prerequisite URL must be https. Refusing to download over $($entry.url)."
    exit 2
}
if ((-not $expectedHash) -and (-not $Pin)) {
    Write-Error @"
prerequisites.json has no pinned sha256, so nothing can be verified.
Establish the trust anchor deliberately:
    scripts\prepare_installer_prerequisites.ps1 -Pin
then record the printed checksum in installer\prerequisites.json.
"@
    exit 2
}

# ---------------------------------------------------------------- 2. verification helpers
function Test-Checksum {
    param([string]$Path, [string]$Expected)
    $actual = (Get-FileHash -Path $Path -Algorithm SHA256).Hash
    return @{
        Actual  = $actual.ToLowerInvariant()
        Matches = ($actual.ToLowerInvariant() -eq $Expected.ToLowerInvariant())
    }
}

function Test-Signature {
    param([string]$Path, [string]$ExpectedSubject)
    $signature = Get-AuthenticodeSignature -FilePath $Path
    $valid = ($signature.Status -eq "Valid")
    $subject = ""
    if ($signature.SignerCertificate) { $subject = $signature.SignerCertificate.Subject }
    $subjectOk = $true
    if ($ExpectedSubject) { $subjectOk = ($subject -eq $ExpectedSubject) }
    return @{
        Status    = $signature.Status
        Subject   = $subject
        Acceptable = ($valid -and $subjectOk)
    }
}

# ---------------------------------------------------------------- 3. acquire
New-Item -ItemType Directory -Force -Path $prerequisiteDir | Out-Null

$present = Test-Path $target
$needsDownload = $Force -or (-not $present)

if ($present -and -not $Force) {
    Write-Step "A copy is already present; re-verifying it instead of downloading."
}

if ($needsDownload) {
    if ($Offline) {
        Write-Error "Offline mode was requested but $($entry.filename) is not present."
        exit 3
    }
    Write-Step "Downloading $([math]::Round($entry.size_bytes / 1MB, 1)) MB from the official source."
    $previousProgress = $ProgressPreference
    $ProgressPreference = "SilentlyContinue"
    try {
        # A partial file must never be left where a later run would treat it as verified.
        $partial = "$target.partial"
        Invoke-WebRequest -Uri $entry.url -OutFile $partial -TimeoutSec 3600 -UseBasicParsing
        Move-Item -LiteralPath $partial -Destination $target -Force
    }
    catch {
        Write-Error "Download failed: $($_.Exception.Message)"
        if (Test-Path "$target.partial") { Remove-Item "$target.partial" -Force }
        exit 3
    }
    finally {
        $ProgressPreference = $previousProgress
    }
}

# ---------------------------------------------------------------- 4. verify
$signature = Test-Signature -Path $target -ExpectedSubject $expectedSigner
Write-Step "Authenticode: $($signature.Status)"
Write-Step "Signer      : $($signature.Subject)"
if (-not $signature.Acceptable) {
    Write-Error @"
The Authenticode signature is not acceptable.
  expected signer: $expectedSigner
  actual signer  : $($signature.Subject)
  status         : $($signature.Status)
This file was not published by the expected publisher. Do not use it, and do not update the
manifest to make this pass.
"@
    exit 5
}

$comparisonHash = ""
if ($expectedHash) { $comparisonHash = $expectedHash }
$hash = Test-Checksum -Path $target -Expected $comparisonHash

if ($Pin) {
    Write-Host ""
    Write-Step "Trust anchor for installer\prerequisites.json:"
    Write-Host "    `"sha256`": `"$($hash.Actual)`","
    Write-Host "    `"size_bytes`": $((Get-Item $target).Length),"
    Write-Host "    `"authenticode_subject`": `"$($signature.Subject)`""
    Write-Host ""
    Write-Step "Record these values by hand. This script never writes them itself."
    exit 0
}

if (-not $hash.Matches) {
    Write-Error @"
CHECKSUM MISMATCH -- this is not the file that was pinned.
  expected: $expectedHash
  actual  : $($hash.Actual)
Investigate. Do not edit prerequisites.json to make this pass.
"@
    exit 4
}

# ---------------------------------------------------------------- 5. report
$size = (Get-Item $target).Length
Write-Host ""
Write-Step "Verified."
Write-Host "  Version   : $($entry.version)"
Write-Host "  File      : $target"
Write-Host "  Size      : $([math]::Round($size / 1MB, 1)) MB"
Write-Host "  SHA-256   : $($hash.Actual)"
Write-Host "  Signed by : $($signature.Subject)"
Write-Host ""
Write-Step "The installer was NOT executed. This script only fetches and verifies it."
exit 0
