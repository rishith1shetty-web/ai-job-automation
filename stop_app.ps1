$projectRoot = $PSScriptRoot
$work = Join-Path $projectRoot "work"

function Stop-AppProcess {
    param([int]$ProcessId)
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($process) {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "Stopped process $ProcessId."
    }
}

foreach ($name in @("api", "streamlit")) {
    $pidFile = Join-Path $work "$name.pid"
    if (Test-Path $pidFile) {
        $savedProcessId = [int](Get-Content $pidFile)
        Stop-AppProcess $savedProcessId
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    }
}

foreach ($port in @(8000, 8501)) {
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

Write-Host "Job Match Console stopped."
