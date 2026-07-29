$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

# PID files live wherever the resolver puts them (app/paths.py). In a source checkout this is
# the historical project-relative "work" folder, so existing behaviour is unchanged; once
# packaged it is the per-machine AppData location. Falls back to "work" if unavailable.
$work = Join-Path $projectRoot "work"
if (Test-Path $python) {
    try {
        $resolved = & $python -m app.paths 2>$null | ConvertFrom-Json
        if ($resolved -and $resolved.work) {
            $work = $resolved.work
        }
    }
    catch {
        # Keep the project-relative default.
    }
}

function Stop-AppProcess {
    param([int]$ProcessId)
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($process) {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "Stopped process $ProcessId."
    }
}

foreach ($name in @("api", "vite", "streamlit")) {
    $pidFile = Join-Path $work "$name.pid"
    if (Test-Path $pidFile) {
        $savedProcessId = [int](Get-Content $pidFile)
        Stop-AppProcess $savedProcessId
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    }
}

# Ports: 8000 FastAPI, 5173 React/Vite, 8501 Streamlit fallback. Only processes owned by
# THIS project (by executable path or a command line referencing the project root) are
# stopped, so unrelated Node/Python servers are never touched.
foreach ($port in @(8000, 5173, 8501)) {
    $connections = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
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
        $isProjectPath = $path -and $path.StartsWith($projectRoot, [System.StringComparison]::OrdinalIgnoreCase)
        $isProjectCommand = $commandLine -and $commandLine.Contains($projectRoot)
        if ($isProjectPath -or $isProjectCommand) {
            Stop-AppProcess $process.Id
        }
    }
}

Write-Host "Rising Path Recruiters stopped (FastAPI, React/Vite and Streamlit fallback)."
