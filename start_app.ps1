param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$outputs = Join-Path $projectRoot "outputs"
$work = Join-Path $projectRoot "work"
$apiStdout = Join-Path $outputs "api.stdout.log"
$apiStderr = Join-Path $outputs "api.stderr.log"
$streamlitStdout = Join-Path $outputs "streamlit.stdout.log"
$streamlitStderr = Join-Path $outputs "streamlit.stderr.log"
$playwrightBrowsers = Join-Path $projectRoot "storage\ms-playwright"

if (-not (Test-Path $python)) {
    throw "Python virtual environment was not found at $python"
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
        if ($isProjectPython -or ($isProjectCommand -and $isPython) -or $isAppCommand) {
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
        [string]$StderrPath
    )
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
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

Write-Step "Checking ports 8000 and 8501."
Stop-PidFileProcess "api"
Stop-PidFileProcess "streamlit"
Stop-ProjectProcessOnPort 8000
Stop-ProjectProcessOnPort 8501

Write-Step "Checking PostgreSQL."
Start-PostgresIfAvailable

Set-Content -Path $apiStdout -Value ""
Set-Content -Path $apiStderr -Value ""
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

Write-Step "Starting Streamlit on http://127.0.0.1:8501."
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

if (-not $NoBrowser) {
    Start-Process "http://127.0.0.1:8501"
}

Write-Host ""
Write-Host "Job Match Console is running:"
Write-Host "  Dashboard: http://127.0.0.1:8501"
Write-Host "  API health: http://127.0.0.1:8000/health"
Write-Host "  Logs: $outputs"
