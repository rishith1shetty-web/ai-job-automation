<#
.SYNOPSIS
    Provision the local PostgreSQL database for the AI Recruitment Platform.

.DESCRIPTION
    Creates the dedicated least-privilege role and the application database, migrates the
    schema to the Alembic head, verifies it, and stores the credential in Windows-protected
    storage. Idempotent: running it again on a provisioned computer changes nothing.

    This script does NOT install PostgreSQL. If PostgreSQL is missing it exits with code 10
    so a future installer step can act on it.

    The administrator password is passed to Python through STDIN, never as a command-line
    argument: on Windows any user can read the full command line of any running process.

.PARAMETER AdminPassword
    A SecureString. Prompted for when omitted. Never echoed, never logged, never written
    to disk.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\provision_postgres.ps1

.EXAMPLE
    .\scripts\provision_postgres.ps1 -Port 5433 -Json

.NOTES
    Exit codes:
      0  SUCCESS                   provisioned or already correct
      2  INVALID_USAGE             no administrator password supplied
     10  POSTGRES_NOT_FOUND        no PostgreSQL reachable (installer should install it)
     11  PORT_OCCUPIED_BY_OTHER    another service holds the port
     12  ADMIN_AUTH_FAILED         administrator password rejected
     13  PROVISION_FAILED          role/database/ownership step failed
     14  MIGRATION_FAILED          alembic upgrade head failed
     15  VERIFICATION_FAILED       resulting schema did not match the baseline
     16  CREDENTIAL_STORE_FAILED   credential could not be stored securely
     20  PYTHON_NOT_FOUND          the application's Python environment is missing
#>
[CmdletBinding()]
param(
    [string]$DbHost = "localhost",
    [int]$Port = 5432,
    [string]$Database = "linkedin_auto_apply",
    [string]$Role = "ai_job_automation_app",
    [string]$AdminUser = "postgres",
    [securestring]$AdminPassword,
    [switch]$Rotate,
    [switch]$DetectOnly,
    [switch]$Status,
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$EXIT_INVALID_USAGE = 2
$EXIT_PYTHON_NOT_FOUND = 20

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Error "The application's Python environment was not found at $python."
    exit $EXIT_PYTHON_NOT_FOUND
}

# Non-secret arguments only. The password goes through stdin below.
$arguments = @(
    "-m", "app.provisioning",
    "--host", $DbHost,
    "--port", $Port,
    "--database", $Database,
    "--role", $Role,
    "--admin-user", $AdminUser
)
if ($Rotate) { $arguments += "--rotate" }
if ($Json) { $arguments += "--json" }

# Read-only modes need no administrator password at all.
if ($DetectOnly -or $Status) {
    if ($DetectOnly) { $arguments += "--detect-only" } else { $arguments += "--status" }
    # The child process inherits this session's PATH; nothing here modifies it, and no
    # [Environment]::SetEnvironmentVariable call is made, so the caller's PATH is untouched.
    & $python @arguments
    exit $LASTEXITCODE
}

if (-not $AdminPassword) {
    $AdminPassword = Read-Host -AsSecureString "PostgreSQL administrator password for '$AdminUser'"
}
if (-not $AdminPassword -or $AdminPassword.Length -eq 0) {
    Write-Error "No administrator password was supplied."
    exit $EXIT_INVALID_USAGE
}

$arguments += "--password-stdin"

# Convert the SecureString only at the moment of use, and clear the unmanaged buffer
# immediately afterwards so the plain text does not linger in process memory.
$bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($AdminPassword)
try {
    $plain = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    # Piped to stdin: it never appears in the command line, in $MyInvocation, in the
    # PowerShell history, or in any process listing.
    $plain | & $python @arguments
    $exitCode = $LASTEXITCODE
}
finally {
    [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    $plain = $null
    Remove-Variable -Name plain -ErrorAction SilentlyContinue
    [System.GC]::Collect()
}

exit $exitCode
