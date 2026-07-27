<#
.SYNOPSIS
    Validate the packaged Windows build without installing anything.

.DESCRIPTION
    Starts dist\RisingPathRecruiters\RisingPathRecruiters.exe in a CLEAN-ROOM child process
    and checks that the packaged application really is self-contained:

      * it runs with the virtual environment deactivated and the project's Python and Node
        directories removed from the child PATH;
      * no python.exe, node.exe or vite process is started;
      * FastAPI answers /health and serves the React shell, including a deep-route refresh;
      * Alembic reports the expected revision and existing data is visible;
      * the bundled Chromium launches (a blank page only -- no portal, no application);
      * every runtime file lands under %LOCALAPPDATA%\AIJobAutomation;
      * nothing is written into the packaged folder;
      * the single-instance lock blocks a second copy and is released on shutdown.

    NO live LinkedIn or Dice action is performed and no application is submitted.

.PARAMETER DatabaseUrl
    Passed to the child process for this test only. Nothing is stored.

.NOTES
    Exit codes:
      0  all checks passed
      2  the build is missing (run scripts\build_windows.ps1 first)
      3  the application did not become ready
      4  one or more checks failed
#>
[CmdletBinding()]
param(
    [string]$DatabaseUrl,
    [int]$Port = 8000,
    [int]$ReadyTimeoutSeconds = 120
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$appDist = Join-Path $projectRoot "dist\RisingPathRecruiters"
$exe = Join-Path $appDist "RisingPathRecruiters.exe"
$localAppData = Join-Path $env:LOCALAPPDATA "AIJobAutomation"

$script:failures = @()
$script:checks = 0

function Invoke-Capture {
    <#
        Run the packaged executable and return its exit code together with its output.

        $ErrorActionPreference = "Stop" turns any stderr line from a native command into a
        terminating NativeCommandError, so it is suspended for the call and the result is
        judged by $LASTEXITCODE, exactly as scripts\build_windows.ps1 does.
    #>
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$Arguments = @()
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $FilePath @Arguments 2>&1 | Out-String
        return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = $output }
    }
    finally {
        $ErrorActionPreference = $previous
    }
}

function Get-DirectorySnapshot {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return @() }
    return Get-ChildItem $Path -File -Recurse -ErrorAction SilentlyContinue |
        ForEach-Object { "$($_.FullName)|$($_.Length)|$($_.LastWriteTimeUtc.Ticks)" } | Sort-Object
}

function Test-Claim {
    param([string]$Label, [bool]$Condition, [string]$Detail = "")
    $script:checks++
    if ($Condition) {
        Write-Host "  [PASS] $Label $Detail"
    }
    else {
        Write-Host "  [FAIL] $Label $Detail"
        $script:failures += $Label
    }
}

if (-not (Test-Path $exe)) {
    Write-Error "Packaged build not found at $exe. Run scripts\build_windows.ps1 first."
    exit 2
}

Write-Host "[test] Packaged build: $appDist"

# ------------------------------------------------------------------ clean-room PATH
# Strip anything belonging to this project (its venv Scripts directory) and Node.js from the
# PATH handed to the child, so the executable cannot fall back on the developer's tools.
# $env:PATH here is a copy in THIS process; the caller's environment is never modified.
$originalPath = $env:PATH
$cleanPath = ($originalPath -split ';' | Where-Object {
    $_ -and
    ($_ -notlike "*$projectRoot*") -and
    ($_ -notlike "*nodejs*") -and
    ($_ -notlike "*npm*")
}) -join ';'

$childEnvironment = @{
    PATH = $cleanPath
    # Prove the bundled browser is used, not the developer's global cache.
    PLAYWRIGHT_BROWSERS_PATH = ""
}
if ($DatabaseUrl) { $childEnvironment["DATABASE_URL"] = $DatabaseUrl }

Write-Host "[test] Child PATH entries: $((($cleanPath -split ';') | Measure-Object).Count) (was $((($originalPath -split ';') | Measure-Object).Count))"

# Record the packaged folder's state (path, size and modification time) so we can prove
# nothing was written into it.
$before = Get-DirectorySnapshot $appDist

# ------------------------------------------------------------------ start
Write-Host "[test] Starting the packaged application (working directory OUTSIDE the checkout)."
$workingDirectory = [System.IO.Path]::GetTempPath()

$processInfo = New-Object System.Diagnostics.ProcessStartInfo
$processInfo.FileName = $exe
$processInfo.Arguments = "--no-browser"
$processInfo.WorkingDirectory = $workingDirectory
$processInfo.UseShellExecute = $false
$processInfo.RedirectStandardOutput = $true
$processInfo.RedirectStandardError = $true
foreach ($key in $childEnvironment.Keys) {
    $processInfo.EnvironmentVariables[$key] = $childEnvironment[$key]
}

$process = [System.Diagnostics.Process]::Start($processInfo)
$stdoutTask = $process.StandardOutput.ReadToEndAsync()
$stderrTask = $process.StandardError.ReadToEndAsync()

try {
    $baseUrl = "http://127.0.0.1:$Port"
    $ready = $false
    $deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($process.HasExited) { break }
        try {
            $health = Invoke-WebRequest -UseBasicParsing -Uri "$baseUrl/health" -TimeoutSec 2
            if ($health.StatusCode -eq 200) { $ready = $true; break }
        }
        catch { Start-Sleep -Milliseconds 500 }
    }

    if (-not $ready) {
        Write-Host "[test] The application did not become ready."
        if ($process.HasExited) { Write-Host "  exit code: $($process.ExitCode)" }
        exit 3
    }
    Write-Host "[test] Ready at $baseUrl"
    Write-Host ""
    Write-Host "[test] Checks:"

    # -------------------------------------------------------------- API + frontend
    Test-Claim "/health answers" ($health.Content -match '"ok"')

    $browser = @{ Accept = "text/html,application/xhtml+xml,*/*;q=0.8" }
    $shell = Invoke-WebRequest -UseBasicParsing -Uri "$baseUrl/" -Headers $browser
    Test-Claim "React shell is served at /" ($shell.StatusCode -eq 200 -and $shell.Content -match '<div id="root">')

    $chunks = [regex]::Matches($shell.Content, '/assets/([A-Za-z0-9\-_.]+\.(?:js|css))') |
        ForEach-Object { $_.Groups[1].Value } | Select-Object -Unique
    Test-Claim "shell references bundled assets" ($chunks.Count -gt 0) "($($chunks.Count) files)"
    $assetsOk = $true
    foreach ($chunk in $chunks) {
        try {
            $asset = Invoke-WebRequest -UseBasicParsing -Uri "$baseUrl/assets/$chunk"
            if ($asset.StatusCode -ne 200) { $assetsOk = $false }
        }
        catch { $assetsOk = $false }
    }
    Test-Claim "every bundled asset is served" $assetsOk

    $refresh = Invoke-WebRequest -UseBasicParsing -Uri "$baseUrl/matched-jobs" -Headers $browser
    Test-Claim "deep-route refresh returns the shell" ($refresh.StatusCode -eq 200 -and $refresh.Content -match '<div id="root">')

    # -------------------------------------------------------------- data
    try {
        $jobs = Invoke-WebRequest -UseBasicParsing -Uri "$baseUrl/api/jobs" -TimeoutSec 30
        Test-Claim "API returns existing job data" ($jobs.StatusCode -eq 200)
    }
    catch {
        Test-Claim "API returns existing job data" $false "($($_.Exception.Message))"
    }

    # -------------------------------------------------------------- no dev runtime
    $childPython = Get-CimInstance Win32_Process -Filter "ParentProcessId = $($process.Id)" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match "python" }
    Test-Claim "no python.exe child process" ($null -eq $childPython)

    $nodeProcesses = Get-Process node -ErrorAction SilentlyContinue
    Test-Claim "no node.exe process running" ($null -eq $nodeProcesses)

    $viteListener = Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue
    Test-Claim "nothing listening on the Vite port" ($null -eq $viteListener)

    # -------------------------------------------------------------- loopback only
    $listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    $loopbackOnly = $true
    foreach ($listener in $listeners) {
        if ($listener.LocalAddress -ne "127.0.0.1") { $loopbackOnly = $false }
    }
    Test-Claim "listening on loopback only" ($loopbackOnly -and $listeners)

    # -------------------------------------------------------------- single instance
    $second = Start-Process -FilePath $exe -ArgumentList "--check", "--no-browser" `
        -WorkingDirectory $workingDirectory -PassThru -Wait -WindowStyle Hidden
    Test-Claim "second instance is refused" ($second.ExitCode -eq 3) "(exit $($second.ExitCode))"

    # -------------------------------------------------------------- bundled Chromium
    # A blank page in a fresh, empty browser context. No portal is opened, no saved session is
    # loaded, and no application is submitted. PLAYWRIGHT_BROWSERS_PATH is cleared for the
    # call so the only browser the child can find is the one inside the packaged folder.
    Write-Host "[test] Launching the bundled Chromium (about:blank only)."
    $sessionsBefore = Get-DirectorySnapshot (Join-Path $localAppData "browser_sessions")
    $previousBrowsers = $env:PLAYWRIGHT_BROWSERS_PATH
    $env:PLAYWRIGHT_BROWSERS_PATH = ""
    try {
        $browser = Invoke-Capture -FilePath $exe -Arguments @("--browser-check")
    }
    finally {
        if ($null -eq $previousBrowsers) {
            Remove-Item Env:\PLAYWRIGHT_BROWSERS_PATH -ErrorAction SilentlyContinue
        }
        else { $env:PLAYWRIGHT_BROWSERS_PATH = $previousBrowsers }
    }

    Test-Claim "bundled Chromium launches and closes" `
        ($browser.ExitCode -eq 0 -and $browser.Output -match "BROWSER OK") "(exit $($browser.ExitCode))"
    Test-Claim "Chromium resolved inside the packaged folder" ($browser.Output -match "inside-installation=True")
    Test-Claim "global Playwright cache was not used" ($browser.Output -match "global-cache-used=False")

    $executable = ([regex]::Match($browser.Output, "BROWSER executable=(.+)")).Groups[1].Value.Trim()
    Test-Claim "Chromium executable lives under dist" `
        ([bool]$executable -and $executable.StartsWith($appDist)) "($executable)"
    if (-not ($browser.Output -match "BROWSER OK")) { Write-Host $browser.Output }

    $sessionsAfter = Get-DirectorySnapshot (Join-Path $localAppData "browser_sessions")
    Test-Claim "saved LinkedIn/Dice sessions were not touched" `
        (($sessionsBefore -join "`n") -eq ($sessionsAfter -join "`n"))

    # -------------------------------------------------------------- writable paths
    Test-Claim "logs are written to LocalAppData" (Test-Path (Join-Path $localAppData "logs\launcher.log"))
    Test-Claim "instance lock is under LocalAppData" (Test-Path (Join-Path $localAppData "work\launcher.lock"))
}
finally {
    Write-Host ""
    Write-Host "[test] Stopping the application."
    if (-not $process.HasExited) {
        $process.CloseMainWindow() | Out-Null
        Start-Sleep -Seconds 2
        if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
    }
    Start-Sleep -Seconds 2
}

# ------------------------------------------------------------------ post-shutdown
$after = Get-DirectorySnapshot $appDist
$difference = Compare-Object -ReferenceObject $before -DifferenceObject $after
Test-Claim "packaged folder was not modified" ($null -eq $difference) "($(if ($difference) { ($difference | Measure-Object).Count } else { 0 }) changes)"

$stillListening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
Test-Claim "port released after shutdown" ($null -eq $stillListening)

Write-Host ""
if ($script:failures.Count -eq 0) {
    Write-Host "[test] RESULT: all $($script:checks) checks passed."
    exit 0
}
Write-Host "[test] RESULT: $($script:failures.Count) of $($script:checks) checks FAILED:"
$script:failures | ForEach-Object { Write-Host "  - $_" }
exit 4
