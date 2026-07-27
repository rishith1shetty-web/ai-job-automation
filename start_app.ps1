param(
    [switch]$NoBrowser,
    [switch]$WithStreamlit,
    # Packaged mode: FastAPI serves the built React app itself. No Node.js, no Vite dev
    # server, and the browser opens the FastAPI URL. This is how deployed computers run.
    [switch]$Packaged
)

$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$frontend = Join-Path $projectRoot "frontend"
$frontendDist = Join-Path $frontend "dist"
# The single source of truth for which URL to open and which processes to start.
$apiUrl = "http://127.0.0.1:8000"
$viteUrl = "http://127.0.0.1:5173"
$appUrl = if ($Packaged) { $apiUrl } else { $viteUrl }
# What a browser sends when NAVIGATING to a page. FastAPI serves the React shell only for a
# navigation (app/static_site.py), so the readiness probe must send this too.
$browserAccept = @{ Accept = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8" }
# Playwright browsers are a bundled ASSET, not mutable runtime data, so they stay with the
# installation rather than moving into AppData.
$playwrightBrowsers = Join-Path $projectRoot "storage\ms-playwright"

if (-not (Test-Path $python)) {
    throw "Python virtual environment was not found at $python"
}

# Writable locations come from the single Python resolver (app/paths.py) so the launcher and
# the application always agree, in development and once packaged. In a source checkout this
# resolves to the historical project-relative "outputs" and "work" folders, so nothing about
# the current developer workflow changes. If the resolver cannot be reached the launcher
# falls back to those same folders rather than failing to start.
$outputs = Join-Path $projectRoot "outputs"
$work = Join-Path $projectRoot "work"
try {
    $resolved = & $python -m app.paths --ensure 2>$null | ConvertFrom-Json
    if ($resolved -and $resolved.logs -and $resolved.work) {
        $outputs = $resolved.logs
        $work = $resolved.work
    }
}
catch {
    Write-Host "[startup] Could not resolve runtime paths; using project-relative folders."
}

$apiStdout = Join-Path $outputs "api.stdout.log"
$apiStderr = Join-Path $outputs "api.stderr.log"
$viteStdout = Join-Path $outputs "vite.stdout.log"
$viteStderr = Join-Path $outputs "vite.stderr.log"
$streamlitStdout = Join-Path $outputs "streamlit.stdout.log"
$streamlitStderr = Join-Path $outputs "streamlit.stderr.log"

# Node.js is required ONLY by the Vite dev server, so it is resolved only in development
# mode. Packaged mode never looks for it -- that is the point of serving frontend/dist from
# FastAPI. Resolved now, while the full PATH is still available (it is minimised below so the
# backend's Playwright uses the bundled browsers).
$nodeExe = $null
if (-not $Packaged) {
    $nodeExe = (Get-Command node.exe -ErrorAction SilentlyContinue).Source
    if (-not $nodeExe) { $nodeExe = "C:\Program Files\nodejs\node.exe" }
    if (-not (Test-Path $nodeExe)) {
        throw "Node.js was not found (looked for node.exe on PATH and at $nodeExe). Install Node.js to run the React dev server, or start in packaged mode: .\start_app.ps1 -Packaged"
    }
}
else {
    # Fail fast with an actionable message rather than starting a server that can only
    # answer 503 for every page.
    if (-not (Test-Path (Join-Path $frontendDist "index.html"))) {
        throw "Packaged mode needs the built web interface, but $frontendDist\index.html was not found. Build it once with: cd frontend; npm install; npm run build"
    }
}

New-Item -ItemType Directory -Force -Path $outputs, $work | Out-Null

function Write-Step {
    param([string]$Message)
    Write-Host "[startup] $Message"
}

function Test-PortOpen {
    param([int]$Port)
    try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $client.Connect("127.0.0.1", $Port)
        $client.Close()
        return $true
    }
    catch {
        return $false
    }
}

function Stop-ProjectProcessOnPort {
    param([int]$Port)
    $connections = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
        Where-Object { $_.State -eq "Listen" -or $_.State -eq "Bound" }
    foreach ($connection in $connections) {
        $process = Get-Process -Id $connection.OwningProcess -ErrorAction SilentlyContinue
        if (-not $process) {
            continue
        }
        $path = ""
        try { $path = $process.Path } catch { $path = "" }
        $commandLine = ""
        try {
            $commandLine = (Get-CimInstance Win32_Process -Filter "ProcessId = $($process.Id)").CommandLine
        }
        catch {
            $commandLine = ""
        }
        $isProjectPython = $path -and $path.StartsWith($projectRoot, [System.StringComparison]::OrdinalIgnoreCase)
        $isProjectCommand = $commandLine -and $commandLine.Contains($projectRoot)
        $isPython = $process.ProcessName -match "python|streamlit|uvicorn"
        # Also match this app's own launch signature so stale/mis-launched instances
        # (e.g. started from a different Python interpreter with the venv injected via
        # PYTHONPATH) are cleaned up instead of being left to serve on the port.
        $isAppCommand = $commandLine -and ($commandLine -match "uvicorn\s+app\.main:app" -or $commandLine -match "streamlit\s+run\s+dashboard\.py")
        # The React dev server runs as node.exe (outside the project tree), so match it by
        # a command line that references THIS project's Vite entry — never an unrelated node.
        $isProjectNode = $commandLine -and $commandLine.Contains($projectRoot) -and $process.ProcessName -match "node"
        if ($isProjectPython -or ($isProjectCommand -and $isPython) -or $isAppCommand -or $isProjectNode) {
            Write-Step "Stopping stale process $($process.Id) on port $Port."
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
        else {
            throw "Port $Port is already used by process $($process.ProcessName) ($($process.Id)). Stop that program or change the app port."
        }
    }
}

function Stop-PidFileProcess {
    param([string]$Name)
    $pidFile = Join-Path $work "$Name.pid"
    if (Test-Path $pidFile) {
        $existingPid = [int](Get-Content $pidFile)
        $process = Get-Process -Id $existingPid -ErrorAction SilentlyContinue
        if ($process) {
            Write-Step "Stopping previous $Name process $existingPid."
            Stop-Process -Id $existingPid -Force -ErrorAction SilentlyContinue
        }
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    }
}

function Show-LogTail {
    param(
        [string]$Path,
        [int]$Lines = 40
    )
    if (Test-Path $Path) {
        Write-Host ""
        Write-Host "---- $Path ----"
        Get-Content $Path -Tail $Lines
    }
}

function Start-PostgresIfAvailable {
    if (Test-PortOpen 5432) {
        Write-Step "PostgreSQL is reachable on localhost:5432."
        return
    }

    $service = Get-Service -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match "postgres|pgsql" -or $_.DisplayName -match "PostgreSQL" } |
        Select-Object -First 1
    if ($service) {
        Write-Step "Starting PostgreSQL service $($service.Name)."
        Start-Service -Name $service.Name
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            if (Test-PortOpen 5432) {
                Write-Step "PostgreSQL is reachable on localhost:5432."
                return
            }
            Start-Sleep -Seconds 1
        }
        throw "PostgreSQL service $($service.Name) started, but localhost:5432 did not become reachable."
    }

    throw "PostgreSQL is not reachable on localhost:5432 and no PostgreSQL Windows service was found."
}

function Wait-HttpOk {
    param(
        [string]$Url,
        [string]$Name,
        [string]$StdoutPath,
        [string]$StderrPath,
        # Optional request headers. The packaged web-interface probe needs
        # "Accept: text/html" because FastAPI serves the React shell only to a browser
        # NAVIGATION — a bare Invoke-WebRequest sends "*/*", which correctly falls through
        # to the API and 404s. See app/static_site.py.
        [hashtable]$Headers = @{}
    )
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2 -Headers $Headers
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                Write-Step "$Name is reachable at $Url."
                return
            }
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }
    Show-LogTail $StdoutPath
    Show-LogTail $StderrPath
    throw "$Name did not become reachable at $Url. See logs above."
}

[Environment]::SetEnvironmentVariable("PATH", $null, "Process")
[Environment]::SetEnvironmentVariable("Path", "C:\Windows\System32;C:\Windows", "Process")
[Environment]::SetEnvironmentVariable("PLAYWRIGHT_BROWSERS_PATH", $playwrightBrowsers, "Process")
# Tell FastAPI (inherited by the child process) whether to serve frontend/dist itself. Set
# explicitly in both modes so the running app never has to guess from its surroundings.
[Environment]::SetEnvironmentVariable("SERVE_FRONTEND", $(if ($Packaged) { "true" } else { "false" }), "Process")

Write-Step "Checking ports 8000, 5173 and 8501."
Stop-PidFileProcess "api"
Stop-PidFileProcess "vite"
Stop-PidFileProcess "streamlit"
Stop-ProjectProcessOnPort 8000
Stop-ProjectProcessOnPort 5173
Stop-ProjectProcessOnPort 8501

Write-Step "Checking PostgreSQL."
Start-PostgresIfAvailable

Set-Content -Path $apiStdout -Value ""
Set-Content -Path $apiStderr -Value ""
Set-Content -Path $viteStdout -Value ""
Set-Content -Path $viteStderr -Value ""
Set-Content -Path $streamlitStdout -Value ""
Set-Content -Path $streamlitStderr -Value ""

Write-Step "Starting FastAPI on http://127.0.0.1:8000."
$api = Start-Process `
    -FilePath $python `
    -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000" `
    -WorkingDirectory $projectRoot `
    -RedirectStandardOutput $apiStdout `
    -RedirectStandardError $apiStderr `
    -WindowStyle Hidden `
    -PassThru
Set-Content (Join-Path $work "api.pid") $api.Id
Start-Sleep -Seconds 1
if ($api.HasExited) {
    Show-LogTail $apiStdout
    Show-LogTail $apiStderr
    throw "FastAPI exited during startup with code $($api.ExitCode)."
}
Wait-HttpOk "http://127.0.0.1:8000/health" "FastAPI" $apiStdout $apiStderr

# Streamlit is now the OPTIONAL fallback (not the default). Start it only on request.
if ($WithStreamlit) {
    Write-Step "Starting Streamlit fallback on http://127.0.0.1:8501."
    $dashboard = Start-Process `
        -FilePath $python `
        -ArgumentList "-m", "streamlit", "run", "dashboard.py", "--server.address", "0.0.0.0", "--server.port", "8501", "--server.headless", "true" `
        -WorkingDirectory $projectRoot `
        -RedirectStandardOutput $streamlitStdout `
        -RedirectStandardError $streamlitStderr `
        -WindowStyle Hidden `
        -PassThru
    Set-Content (Join-Path $work "streamlit.pid") $dashboard.Id
    Start-Sleep -Seconds 1
    if ($dashboard.HasExited) {
        Show-LogTail $streamlitStdout
        Show-LogTail $streamlitStderr
        throw "Streamlit exited during startup with code $($dashboard.ExitCode)."
    }
    Wait-HttpOk "http://127.0.0.1:8501" "Streamlit" $streamlitStdout $streamlitStderr
}
else {
    Write-Step "Skipping Streamlit fallback (pass -WithStreamlit to start it on :8501)."
}

# React/Vite dev server — the PRIMARY local dashboard IN DEVELOPMENT ONLY. Launch Vite
# through Node directly so the recorded PID is the actual server (no npm.cmd wrapper to
# orphan). The Vite proxy forwards /api to FastAPI, so no CORS/backend change is needed.
# In packaged mode this whole block is skipped: FastAPI already serves the built assets.
$vite = $null
if (-not $Packaged) {
    $viteEntry = Join-Path $frontend "node_modules\vite\bin\vite.js"
    if (-not (Test-Path $viteEntry)) {
        throw "Vite was not found at $viteEntry. Run 'npm install' in the frontend folder first."
    }
    Write-Step "Starting React (Vite) on $viteUrl."
    $vite = Start-Process `
        -FilePath $nodeExe `
        -ArgumentList $viteEntry `
        -WorkingDirectory $frontend `
        -RedirectStandardOutput $viteStdout `
        -RedirectStandardError $viteStderr `
        -WindowStyle Hidden `
        -PassThru
    Set-Content (Join-Path $work "vite.pid") $vite.Id
    Start-Sleep -Seconds 1
    if ($vite.HasExited) {
        Show-LogTail $viteStdout
        Show-LogTail $viteStderr
        throw "React (Vite) exited during startup with code $($vite.ExitCode)."
    }
    Wait-HttpOk $viteUrl "React" $viteStdout $viteStderr
}
else {
    Write-Step "Packaged mode: FastAPI is serving the built web interface (no Node.js, no Vite)."
    # Prove the shell really is being served before the browser is pointed at it. The probe
    # must identify itself as a browser navigation, exactly as the real browser will.
    Wait-HttpOk $apiUrl "Web interface" $apiStdout $apiStderr -Headers $browserAccept
}

# Open the React dashboard (the primary UI) — not Streamlit. Development opens the Vite dev
# server; packaged opens the FastAPI-served build.
if (-not $NoBrowser) {
    Start-Process $appUrl
}

$streamlitStatus = if ($WithStreamlit) { "running at http://127.0.0.1:8501" } else { "not started  (run: .\start_app.ps1 -WithStreamlit)" }
$modeLabel = if ($Packaged) { "packaged (FastAPI serves frontend\dist; Node.js not required)" } else { "development (Vite dev server)" }

Write-Host ""
Write-Host "Rising Path Recruiters is running:"
Write-Host "  Mode:                      $modeLabel"
Write-Host "  Open this:                 $appUrl"
Write-Host "  FastAPI backend:           $apiUrl"
Write-Host "  FastAPI health:            $apiUrl/health"
Write-Host "  Streamlit fallback:        $streamlitStatus"
Write-Host ""
Write-Host "  Process status:"
Write-Host "    FastAPI  (work\api.pid):   $($api.Id)"
if ($vite) { Write-Host "    React    (work\vite.pid):  $($vite.Id)" }
if ($WithStreamlit) { Write-Host "    Streamlit (work\streamlit.pid): $($dashboard.Id)" }
Write-Host ""
Write-Host "  Logs: $outputs"
Write-Host "  Stop everything with: .\stop_app.ps1"
