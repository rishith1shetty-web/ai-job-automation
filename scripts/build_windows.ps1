<#
.SYNOPSIS
    Build the portable Windows application folder (PyInstaller --onedir).

.DESCRIPTION
    Deterministic, repeatable, and safe to run twice:

      1. verify the toolchain
      2. clean ONLY the two guarded build directories
      3. npm ci + npm run build, then verify frontend/dist really has index.html + assets
      4. stage Chromium into build/ms-playwright (never the developer's global cache)
      5. run PyInstaller against RisingPathRecruiters.spec
      6. verify every expected file landed in dist/
      7. scan dist/ for secrets and user data, and FAIL the build if any is found

    After step 3 the packaged application needs no Node.js. This script does not create an
    installer and does not install PostgreSQL.

.PARAMETER SkipFrontend
    Reuse an existing frontend/dist. Intended for iterating on the Python side only.

.PARAMETER SkipBrowsers
    Do not stage Chromium. Produces a much smaller build that cannot run automation.

.NOTES
    Exit codes:
      0  build succeeded and passed verification
      2  prerequisite missing (Python venv, npm, spec file)
      3  frontend build failed or produced no usable dist
      4  Playwright browser staging failed
      5  PyInstaller failed
      6  expected output files missing
      7  SECRETS OR USER DATA FOUND IN dist/  (build rejected)
#>
[CmdletBinding()]
param(
    [switch]$SkipFrontend,
    [switch]$SkipBrowsers
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$frontend = Join-Path $projectRoot "frontend"
$frontendDist = Join-Path $frontend "dist"
$specFile = Join-Path $projectRoot "RisingPathRecruiters.spec"

# The ONLY directories this script is allowed to delete.
$buildDir = Join-Path $projectRoot "build"
$distDir = Join-Path $projectRoot "dist"
$appDist = Join-Path $distDir "RisingPathRecruiters"
$stagedBrowsers = Join-Path $buildDir "ms-playwright"

function Write-Step { param([string]$Message) Write-Host "[build] $Message" }

function Invoke-Native {
    <#
        Run an external program and return its exit code.

        Native tools (npm, playwright, PyInstaller) write progress and deprecation notices to
        stderr even on success. With $ErrorActionPreference = "Stop", PowerShell turns any
        stderr line from a native command into a terminating NativeCommandError -- so a build
        would "fail" on an npm deprecation warning. Suspending that policy for the call and
        judging the result by $LASTEXITCODE alone is the correct behaviour.
    #>
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$Arguments = @()
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        # Out-Host, not a bare call: the command's stdout must reach the console WITHOUT
        # entering this function's pipeline, or the returned value would be the whole build
        # log with the exit code appended to it.
        & $FilePath @Arguments | Out-Host
        return $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
}

function Remove-GuardedDirectory {
    param([string]$Path)
    # Refuse to delete anything that is not one of our two known build outputs. A typo in
    # a variable must never turn into "delete the project".
    $resolved = [System.IO.Path]::GetFullPath($Path)
    $allowed = @(
        [System.IO.Path]::GetFullPath($buildDir),
        [System.IO.Path]::GetFullPath($distDir),
        [System.IO.Path]::GetFullPath((Join-Path $buildDir "pyinstaller"))
    )
    if ($allowed -notcontains $resolved) {
        throw "Refusing to delete '$resolved': only the build and dist directories may be cleaned."
    }
    if (Test-Path $resolved) {
        Write-Step "Cleaning $resolved"
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

# ---------------------------------------------------------------- 1. prerequisites
Write-Step "Checking prerequisites."
if (-not (Test-Path $python)) {
    Write-Error "Python environment not found at $python"
    exit 2
}
if (-not (Test-Path $specFile)) {
    Write-Error "Spec file not found at $specFile"
    exit 2
}
$code = Invoke-Native -FilePath $python -Arguments @("-c", "import PyInstaller")
if ($code -ne 0) {
    Write-Error "PyInstaller is not installed in the virtual environment."
    exit 2
}
if (-not $SkipFrontend) {
    $npm = (Get-Command npm.cmd -ErrorAction SilentlyContinue)
    if (-not $npm) { $npm = (Get-Command npm -ErrorAction SilentlyContinue) }
    if (-not $npm) {
        Write-Error "npm was not found. Node.js is required to BUILD (not to run) the app."
        exit 2
    }
}

# ---------------------------------------------------------------- 2. clean
# dist/ is rebuilt from scratch every time. build/ is NOT wiped wholesale: it holds the
# staged Chromium, and deleting it would re-download ~230 MB on every rebuild. Only the
# PyInstaller work directory inside it is cleared.
Remove-GuardedDirectory $distDir
Remove-GuardedDirectory (Join-Path $buildDir "pyinstaller")

# ---------------------------------------------------------------- 3. React
if ($SkipFrontend) {
    Write-Step "Skipping the frontend build (-SkipFrontend)."
}
else {
    Write-Step "Installing frontend dependencies (npm ci)."
    Push-Location $frontend
    try {
        $code = Invoke-Native -FilePath $npm.Source -Arguments @("ci")
        if ($code -ne 0) { Write-Error "npm ci failed (exit $code)."; exit 3 }
        Write-Step "Building the React production bundle (npm run build)."
        $code = Invoke-Native -FilePath $npm.Source -Arguments @("run", "build")
        if ($code -ne 0) { Write-Error "npm run build failed (exit $code)."; exit 3 }
    }
    finally {
        Pop-Location
    }
}

Write-Step "Verifying the frontend build output."
$indexHtml = Join-Path $frontendDist "index.html"
$assetsDir = Join-Path $frontendDist "assets"
if (-not (Test-Path $indexHtml)) { Write-Error "Missing $indexHtml"; exit 3 }
if (-not (Test-Path $assetsDir)) { Write-Error "Missing $assetsDir"; exit 3 }
$assetCount = (Get-ChildItem $assetsDir -File | Measure-Object).Count
if ($assetCount -lt 1) { Write-Error "No files in $assetsDir"; exit 3 }
Write-Step "Frontend OK: index.html plus $assetCount asset files."

# ---------------------------------------------------------------- 4. Chromium
New-Item -ItemType Directory -Force -Path $buildDir | Out-Null
if ($SkipBrowsers) {
    Write-Step "Skipping Playwright browsers (-SkipBrowsers). Automation will not run."
}
else {
    Write-Step "Staging Playwright Chromium into build\ms-playwright."
    # Installed into a directory owned by THIS build, so the output never depends on the
    # developer's global cache. Playwright downloads only if the version is absent.
    $env:PLAYWRIGHT_BROWSERS_PATH = $stagedBrowsers
    try {
        $code = Invoke-Native -FilePath $python -Arguments @("-m", "playwright", "install", "chromium")
        if ($code -ne 0) { Write-Error "playwright install chromium failed (exit $code)."; exit 4 }
    }
    finally {
        # Scoped to this process only; the caller's environment is untouched.
        Remove-Item Env:\PLAYWRIGHT_BROWSERS_PATH -ErrorAction SilentlyContinue
    }
    $chromeExe = Get-ChildItem $stagedBrowsers -Recurse -Filter "chrome.exe" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $chromeExe) { Write-Error "No chrome.exe under $stagedBrowsers"; exit 4 }
    Write-Step "Chromium staged: $($chromeExe.FullName)"
}

# ---------------------------------------------------------------- 5. PyInstaller
Write-Step "Running PyInstaller (--onedir)."
$code = Invoke-Native -FilePath $python -Arguments @(
    "-m", "PyInstaller", "--noconfirm", "--clean",
    "--distpath", $distDir,
    "--workpath", (Join-Path $buildDir "pyinstaller"),
    $specFile
)
if ($code -ne 0) { Write-Error "PyInstaller failed (exit $code)."; exit 5 }

# ---------------------------------------------------------------- 6. verify
Write-Step "Verifying the packaged output."
$expected = @(
    (Join-Path $appDist "RisingPathRecruiters.exe"),
    (Join-Path $appDist "_internal\frontend\dist\index.html"),
    (Join-Path $appDist "_internal\frontend\dist\assets"),
    (Join-Path $appDist "_internal\alembic.ini"),
    (Join-Path $appDist "_internal\alembic\versions")
)
$missing = @()
foreach ($path in $expected) { if (-not (Test-Path $path)) { $missing += $path } }
if ($missing.Count -gt 0) {
    Write-Error "Missing from the build:`n  $($missing -join "`n  ")"
    exit 6
}
if (-not $SkipBrowsers) {
    $packagedChrome = Get-ChildItem (Join-Path $appDist "_internal\ms-playwright") -Recurse `
        -Filter "chrome.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $packagedChrome) { Write-Error "No bundled chrome.exe in the build."; exit 6 }
}
Write-Step "All expected files are present."

# ---------------------------------------------------------------- 7. secrets scan
Write-Step "Scanning the build for secrets and user data."
$code = Invoke-Native -FilePath $python -Arguments @("-m", "scripts.scan_build_secrets", "--path", $appDist)
if ($code -ne 0) {
    Write-Error "The build contains secrets or user data and has been rejected."
    exit 7
}

$sizeMb = [math]::Round((Get-ChildItem $appDist -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB, 1)
Write-Host ""
Write-Step "Build complete."
Write-Host "  Output : $appDist"
Write-Host "  Size   : $sizeMb MB"
Write-Host "  Run    : $appDist\RisingPathRecruiters.exe"
exit 0
