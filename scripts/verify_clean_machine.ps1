<#
.SYNOPSIS
    Phase 6 clean-machine verification. Self-contained: no Python, no Node, no source checkout.

.DESCRIPTION
    Copy this file and RisingPathRecruiters-Setup-<version>.exe to a clean Windows x64 VM.
    Nothing else is required -- every check uses Windows built-ins and the installed product
    itself. The script is read-only apart from starting and gracefully stopping the
    application; it never installs, uninstalls, kills an unrelated process, disables a
    security feature or writes into the installation directory.

    Run one stage at a time, in order, and send back the .log file each stage writes.

      preflight       BEFORE running setup. Proves the machine is genuinely clean and
                      records the installer's identity, checksum and signature status.
      install-verify  AFTER running setup. Installation, PostgreSQL, provisioning, DPAPI
                      credential ownership, shortcuts, Add/Remove entry.
      runtime         Launch, readiness, binding, no console, second instance, graceful
                      shutdown, Program Files left untouched. Writes a data fingerprint.
      postuninstall   AFTER uninstalling. Proves what was removed and what was preserved.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\verify_clean_machine.ps1 -Stage preflight -Installer .\RisingPathRecruiters-Setup-1.3.0.exe
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('preflight', 'install-verify', 'runtime', 'postuninstall')]
    [string]$Stage,

    [string]$Installer,

    [string]$Label = '',

    [string]$OutFile = ''
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

# ---------------------------------------------------------------------------------------
# Fixed identity of the product under test. Kept here so the script needs no other file.
# ---------------------------------------------------------------------------------------
$AppName      = 'Rising Path Recruiters'
$ExeName      = 'RisingPathRecruiters.exe'
$AppId        = '{7B3D9E42-5C18-4A6F-9E27-1D4A8F0C6B35}'
$Publisher    = 'Rising Path Recruiters'
$DataDirName  = 'AIJobAutomation'
$DbName       = 'linkedin_auto_apply'
$DbRole       = 'ai_job_automation_app'
# Setup refuses to install anything below this. Keep in step with installer\prerequisites.json
# and with RequiredPostgresMajor in the .iss -- there is no import between them.
$RequiredPostgresMajor = 16

# --- 64-bit resolution -------------------------------------------------------------------
# A 32-bit PowerShell host (WOW64) is lied to twice: $env:ProgramFiles reads back as
# "C:\Program Files (x86)", and HKLM\SOFTWARE is redirected to WOW6432Node. On PC10 that
# made the verifier look for the application, and its Add/Remove Programs entry, in two
# places it was never installed to -- four false failures against a correct installation.
# The installer is {#ArchitecturesInstallIn64BitMode} x64compatible, so {autopf} is always
# the 64-bit Program Files: resolve it explicitly and never trust the redirected view.
$ProgramFiles64 = $env:ProgramW6432
if ([string]::IsNullOrWhiteSpace($ProgramFiles64)) { $ProgramFiles64 = $env:ProgramFiles }
$ProgramFiles32 = ${env:ProgramFiles(x86)}

$InstallDir   = Join-Path $ProgramFiles64 $AppName
$InstalledExe = Join-Path $InstallDir $ExeName
# Only ever a mis-installation; checked so it is reported rather than silently missed.
$InstallDir32 = ''
if (-not [string]::IsNullOrWhiteSpace($ProgramFiles32)) {
    $InstallDir32 = Join-Path $ProgramFiles32 $AppName
}

# Add/Remove Programs, read through an explicit registry view rather than the HKLM: drive.
$UninstallPath = "SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\$($AppId)_is1"

function Get-UninstallEntry([string]$view) {
    try {
        $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
            [Microsoft.Win32.RegistryHive]::LocalMachine, $view)
        try {
            $key = $base.OpenSubKey($UninstallPath)
            if ($null -eq $key) { return $null }
            try {
                $entry = @{}
                foreach ($name in $key.GetValueNames()) { $entry[$name] = $key.GetValue($name) }
                return $entry
            } finally { $key.Close() }
        } finally { $base.Close() }
    } catch { return $null }
}

function Get-AllProductEntries() {
    $found = @()
    foreach ($view in @([Microsoft.Win32.RegistryView]::Registry64,
                        [Microsoft.Win32.RegistryView]::Registry32)) {
        try {
            $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
                [Microsoft.Win32.RegistryHive]::LocalMachine, $view)
            try {
                $root = $base.OpenSubKey('SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall')
                if ($null -eq $root) { continue }
                try {
                    foreach ($name in $root.GetSubKeyNames()) {
                        $sub = $root.OpenSubKey($name)
                        if ($null -eq $sub) { continue }
                        try {
                            $display = $sub.GetValue('DisplayName')
                            if ($display -eq $AppName -or $display -like "$AppName *") {
                                $found += "$view\$name"
                            }
                        } finally { $sub.Close() }
                    }
                } finally { $root.Close() }
            } finally { $base.Close() }
        } catch { }
    }
    return $found
}
$DataDir      = Join-Path $env:LOCALAPPDATA $DataDirName
$WorkDir      = Join-Path $DataDir 'work'
$ConfigDir    = Join-Path $DataDir 'config'
$LogFile      = Join-Path (Join-Path $DataDir 'logs') 'launcher.log'
$StateFile    = Join-Path $WorkDir 'runtime.json'
$TokenFile    = Join-Path $WorkDir 'shutdown.token'
$HandoffFile  = Join-Path $WorkDir 'setup-handoff.bin'
$ResultFile   = Join-Path $WorkDir 'installer-result.json'

$Here         = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ManifestFile = Join-Path $Here 'programfiles-manifest.txt'
$FingerFile   = Join-Path $Here 'data-fingerprint.json'

if ([string]::IsNullOrWhiteSpace($OutFile)) {
    $suffix = ''
    if (-not [string]::IsNullOrWhiteSpace($Label)) { $suffix = '-' + $Label }
    $OutFile = Join-Path $Here ("phase6-$Stage$suffix.log")
}

# ---------------------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------------------
# A genuinely mutable list, and one whose name cannot be collided with by accident.
#
# It used to be called $script:Lines, which broke on PC9: PowerShell variable names are
# CASE-INSENSITIVE, so an innocuous local named $lines in one of the stages below was the
# same variable, and assigning an array to it replaced this collection with a fixed-size
# Object[]. Every Emit after that point failed with "Collection was of a fixed size", and
# the written log was truncated -- while the console output looked complete, because
# Write-Host had already run. The distinctive name is the actual fix; the type is belt and
# braces, and Assert-ReportList turns a future collision into a loud failure rather than a
# silently truncated report.
$script:ReportLines = [System.Collections.Generic.List[string]]::new()
$script:Pass = 0
$script:Fail = 0
$script:Info = 0

function Assert-ReportList {
    if ($script:ReportLines -isnot [System.Collections.Generic.List[string]]) {
        throw "The report collection was replaced by a $($script:ReportLines.GetType().FullName). A variable in this script shares its name."
    }
}

function Emit([string]$text) {
    Assert-ReportList
    $script:ReportLines.Add($text)
    Write-Host $text
}

function Check([string]$name, [bool]$ok, [string]$detail) {
    if ($ok) { $script:Pass++; $tag = '[PASS]' } else { $script:Fail++; $tag = '[FAIL]' }
    if ([string]::IsNullOrWhiteSpace($detail)) { Emit "$tag $name" }
    else { Emit "$tag $name -- $detail" }
}

function Note([string]$name, [string]$detail) {
    $script:Info++
    Emit "[INFO] $name -- $detail"
}

function Section([string]$title) {
    Emit ''
    Emit "=== $title ==="
}

function Absent([string]$path) { return -not (Test-Path -LiteralPath $path) }

function HaveCommand([string]$name) {
    $c = Get-Command $name -ErrorAction SilentlyContinue
    if ($c) { return $c.Source } else { return '' }
}

# PE subsystem: 2 = Windows GUI (no console), 3 = console.
function Get-PeSubsystem([string]$path) {
    try {
        $fs = [System.IO.File]::OpenRead($path)
        try {
            $br = New-Object System.IO.BinaryReader($fs)
            $fs.Position = 0x3C
            $peOffset = $br.ReadInt32()
            $fs.Position = $peOffset
            if ($br.ReadUInt32() -ne 0x00004550) { return -1 }   # 'PE\0\0'
            $fs.Position = $peOffset + 24                        # optional header
            $magic = $br.ReadUInt16()                            # 0x20B = PE32+
            $fs.Position = $peOffset + 24 + 68
            return [int]$br.ReadUInt16()
        } finally { $fs.Dispose() }
    } catch { return -1 }
}

function Get-FileOwner([string]$path) {
    try { return (Get-Acl -LiteralPath $path).Owner } catch { return '<unavailable>' }
}

function Get-AclIdentities([string]$path) {
    try {
        $acl = Get-Acl -LiteralPath $path
        return ($acl.Access | ForEach-Object { $_.IdentityReference.Value } | Sort-Object -Unique) -join '; '
    } catch { return '<unavailable>' }
}

# A stable manifest of Program Files content, used to prove nothing is written at runtime.
function New-Manifest([string]$root) {
    if (-not (Test-Path -LiteralPath $root)) { return @() }
    return Get-ChildItem -LiteralPath $root -Recurse -File -Force -ErrorAction SilentlyContinue |
        ForEach-Object { "{0}|{1}|{2:o}" -f $_.FullName.Substring($root.Length), $_.Length, $_.LastWriteTimeUtc } |
        Sort-Object
}

function Get-RuntimeState() {
    if (-not (Test-Path -LiteralPath $StateFile)) { return $null }
    try { return Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json } catch { return $null }
}

# Listening TCP sockets owned by this application. Pass a pid for one instance, or 0 for
# every process running the installed executable -- never a machine-wide count, which would
# be noisy and would not prove anything about a second instance.
function Get-AppListeners([int]$appPid) {
    # CALLERS MUST WRAP THIS IN @(). PowerShell unrolls a returned one-element array into a
    # bare PSCustomObject, and a bare PSCustomObject has NO .Count at all -- it reads back
    # as $null, so "$listeners.Count -eq 1" was False and the detail printed "count=".
    # ($null.Count is 0, which is why the neighbouring non-loopback check kept passing and
    # this one did not.) A single listener is the NORMAL case, so the check that mattered
    # most was the one guaranteed to fail.
    $ourPids = @()
    if ($appPid -ne 0) {
        $ourPids = @($appPid)
    } else {
        $ourPids = @(Get-Process -Name ([IO.Path]::GetFileNameWithoutExtension($ExeName)) -ErrorAction SilentlyContinue |
            ForEach-Object { $_.Id })
    }
    $rows = @()
    if ($ourPids.Count -eq 0) { return $rows }
    $out = netstat -ano -p TCP | Select-String 'LISTENING'
    foreach ($line in $out) {
        $parts = ($line.Line.Trim() -split '\s+')
        if ($parts.Count -ge 5) {
            $owner = 0
            [void][int]::TryParse($parts[4], [ref]$owner)
            if ($ourPids -contains $owner) {
                $rows += [pscustomobject]@{ Local = $parts[1]; ProcessId = $owner }
            }
        }
    }
    return $rows
}

function Get-Descendants([int]$rootPid) {
    $all = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Select-Object ProcessId, ParentProcessId, Name
    $result = @()
    $frontier = @($rootPid)
    while ($frontier.Count -gt 0) {
        $next = @()
        foreach ($p in $frontier) {
            foreach ($proc in $all) {
                if ($proc.ParentProcessId -eq $p) { $result += $proc; $next += $proc.ProcessId }
            }
        }
        $frontier = $next
    }
    return $result
}

# Start-Process -Wait -PassThru does NOT reliably populate ExitCode: the returned object is
# a snapshot, and with -Wait the caller often reads back $null (which is what the PC10 run
# reported as "exit="). Waiting on the object we hold, then reading it, always works.
function Start-AndWait([string]$exe, [string[]]$arguments, [int]$timeoutSec = 600) {
    $proc = $null
    if ($arguments -and $arguments.Count -gt 0) {
        $proc = Start-Process -FilePath $exe -ArgumentList $arguments -PassThru
    } else {
        $proc = Start-Process -FilePath $exe -PassThru
    }
    if (-not $proc.WaitForExit($timeoutSec * 1000)) {
        return [pscustomobject]@{ ExitCode = $null; TimedOut = $true; Process = $proc }
    }
    $proc.WaitForExit()   # ensures the exit code is available, not just the main window
    return [pscustomobject]@{ ExitCode = $proc.ExitCode; TimedOut = $false; Process = $proc }
}

# What a browser sends when it NAVIGATES to a page. Invoke-WebRequest's default is
# "Accept: */*", which the application deliberately treats as an API call -- see
# accepts_html() in app/static_site.py, the same history-fallback rule a dev server uses.
# Asking for the dashboard with the wrong header tested the wrong thing and reported the
# React build as missing when it was present and correct.
$BrowserAccept = 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'

function Invoke-Local([string]$url, [int]$timeoutSec = 15, [string]$accept = '') {
    try {
        if ([string]::IsNullOrWhiteSpace($accept)) {
            return Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec $timeoutSec
        }
        return Invoke-WebRequest -Uri $url -Headers @{ Accept = $accept } -UseBasicParsing -TimeoutSec $timeoutSec
    } catch { return $null }
}

function Describe-Response($response) {
    <#  A failed HTTP check that reports nothing cannot be diagnosed remotely -- which is
        exactly what happened here: the detail was blank, so the body that would have shown
        a 404 was never seen. #>
    if ($null -eq $response) { return 'no response (request failed or timed out)' }
    $body = [string]$response.Content
    $head = $body.Substring(0, [Math]::Min(160, $body.Length)) -replace '\s+', ' '
    return "status=$($response.StatusCode) length=$($body.Length) body[0..160]=$head"
}

# Patterns that must never appear in a log the user might send to support.
$SecretPatterns = @(
    'superpassword',
    'password\s*[=:]\s*[^\s*]',
    'postgres(ql)?(\+\w+)?://[^\s:/@]+:[^\s@*]+@',
    'PGPASSWORD'
)

function Test-LogForSecrets([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return @('<log missing>') }
    $hits = @()
    foreach ($pattern in $SecretPatterns) {
        $found = Select-String -LiteralPath $path -Pattern $pattern -AllMatches -ErrorAction SilentlyContinue
        foreach ($f in $found) {
            # A redacted value is the expected, correct outcome; only unredacted text is a hit.
            if ($f.Line -notmatch '\*\*\*') { $hits += ("line {0}: {1}" -f $f.LineNumber, $f.Line.Trim()) }
        }
    }
    return $hits
}

# ---------------------------------------------------------------------------------------
Emit "Phase 6 clean-machine verification -- stage: $Stage"
Emit ("Run at        : " + (Get-Date).ToString('u'))
Emit ("Computer      : " + $env:COMPUTERNAME)
Emit ("User          : " + "$env:USERDOMAIN\$env:USERNAME")
$osInfo = Get-CimInstance Win32_OperatingSystem
Emit ("Windows       : {0} build {1} {2}" -f $osInfo.Caption, $osInfo.BuildNumber, $osInfo.OSArchitecture)
Emit ("PowerShell    : " + $PSVersionTable.PSVersion)
$elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Emit ("Elevated      : " + $elevated)
Emit ("64-bit host   : " + [Environment]::Is64BitProcess + " (OS 64-bit: " + [Environment]::Is64BitOperatingSystem + ")")
Emit ("Install dir   : " + $InstallDir)
Emit ("User SID      : " + ([Security.Principal.WindowsIdentity]::GetCurrent()).User.Value)

switch ($Stage) {

# =======================================================================================
'preflight' {
    Section 'Installer identity'
    if ([string]::IsNullOrWhiteSpace($Installer)) {
        $candidate = Get-ChildItem -LiteralPath $Here -Filter 'RisingPathRecruiters-Setup-*.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($candidate) { $Installer = $candidate.FullName }
    }
    if ([string]::IsNullOrWhiteSpace($Installer) -or -not (Test-Path -LiteralPath $Installer)) {
        Check 'Installer file is present' $false 'pass -Installer <path to the setup .exe>'
    } else {
        $item = Get-Item -LiteralPath $Installer
        Note 'Filename' $item.Name
        Note 'Size' ("{0:N0} bytes ({1:N1} MB)" -f $item.Length, ($item.Length / 1MB))
        Note 'SHA256' (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash
        $vi = $item.VersionInfo
        Note 'ProductName/Version' ("{0} / {1}" -f $vi.ProductName.Trim(), $vi.ProductVersion.Trim())
        Note 'CompanyName' $vi.CompanyName.Trim()
        $sig = Get-AuthenticodeSignature -LiteralPath $Installer
        $signer = '<none>'
        if ($sig.SignerCertificate) { $signer = $sig.SignerCertificate.Subject }
        Note 'Authenticode' ("{0} / {1}" -f $sig.Status, $signer)
        Check 'Unsigned status is recorded honestly' ($sig.Status -ne 'Valid') "expected NotSigned in Phase 6; got $($sig.Status)"
        $zone = Get-Item -LiteralPath "$Installer`:Zone.Identifier" -ErrorAction SilentlyContinue
        if ($zone) { Note 'Mark of the Web' 'present (SmartScreen will warn -- expected)' }
        else { Note 'Mark of the Web' 'absent (copied by a channel that does not tag; SmartScreen may not warn)' }
    }

    Section 'The machine must be clean'
    # PostgreSQL is deliberately NOT in this list any more. The deployment model changed:
    # PostgreSQL 16 is installed by hand BEFORE setup runs, so a clean machine for this
    # test is one with PostgreSQL present and everything else absent.
    foreach ($tool in @('python', 'python3', 'py', 'node', 'npm', 'npx', 'vite')) {
        $where = HaveCommand $tool
        Check "$tool is absent" ([string]::IsNullOrEmpty($where)) $where
    }

    Section 'PostgreSQL must already be installed (you install it, not setup)'
    $pgServices = @(Get-Service -Name 'postgresql*' -ErrorAction SilentlyContinue)
    Check 'A PostgreSQL service exists' ($pgServices.Count -gt 0) (($pgServices | ForEach-Object { "$($_.Name)=$($_.Status)" }) -join ', ')
    Check 'A PostgreSQL service is Running' (($pgServices | Where-Object { $_.Status -eq 'Running' }).Count -gt 0) ''

    # The registry is what setup itself reads, in the 64-bit view -- so this asks the same
    # question the installer's gate will ask, rather than a similar one.
    $pgKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey('LocalMachine', 'Registry64').OpenSubKey('SOFTWARE\PostgreSQL\Installations')
    $majors = @()
    if ($pgKey) {
        foreach ($name in $pgKey.GetSubKeyNames()) {
            $v = $pgKey.OpenSubKey($name).GetValue('Version')
            if ($v -match '^(\d+)') { $majors += [int]$Matches[1] }
        }
    }
    Note 'PostgreSQL majors in the registry' ($majors -join ', ')
    Check "PostgreSQL major $RequiredPostgresMajor or newer is installed" (($majors | Where-Object { $_ -ge $RequiredPostgresMajor }).Count -gt 0) "found: $($majors -join ', ')"

    $listener = @(netstat -ano -p TCP | Select-String 'LISTENING' | Select-String ':5432\s')
    Check 'Something is listening on port 5432' ($listener.Count -gt 0) (($listener | ForEach-Object { $_.Line.Trim() }) -join ' | ')

    $isready = Get-ChildItem -LiteralPath 'C:\Program Files\PostgreSQL' -Recurse -Filter 'pg_isready.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($isready) {
        $r = Start-AndWait $isready.FullName @('-h', 'localhost', '-p', '5432', '-q') 30
        Check 'PostgreSQL accepts connections (pg_isready)' ($r.ExitCode -eq 0) "exit=$($r.ExitCode)"
    } else {
        Note 'pg_isready' 'not found; port check above is the only reachability evidence'
    }

    Section 'Nothing of this application may be present yet'
    Check "No %LOCALAPPDATA%\$DataDirName" (Absent $DataDir) $DataDir
    Check 'No Playwright browser cache' (Absent (Join-Path $env:LOCALAPPDATA 'ms-playwright')) ''
    $installedAnywhere = (Test-Path -LiteralPath $InstallDir)
    if (-not [string]::IsNullOrWhiteSpace($InstallDir32)) {
        $installedAnywhere = $installedAnywhere -or (Test-Path -LiteralPath $InstallDir32)
    }
    Check 'No existing installation (both Program Files locations)' (-not $installedAnywhere) "$InstallDir | $InstallDir32"
    Check 'No Add/Remove Programs entry (both registry views)' ((Get-AllProductEntries).Count -eq 0) ''
    Check 'No .env in the profile' (Absent (Join-Path $env:USERPROFILE '.env')) ''
    Check 'No project source checkout on C:' ((Get-ChildItem -LiteralPath 'C:\' -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -match 'build-a-python-application' }).Count -eq 0) ''

    Section 'Security posture (read only -- nothing is disabled)'
    try {
        $mp = Get-MpComputerStatus -ErrorAction Stop
        Note 'Defender real-time protection' $mp.RealTimeProtectionEnabled
        Note 'Defender antivirus enabled' $mp.AntivirusEnabled
        Note 'Defender signature version' $mp.AntivirusSignatureVersion
    } catch { Note 'Defender status' 'Get-MpComputerStatus unavailable' }
    try {
        $smartScreen = Get-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer' -Name 'SmartScreenEnabled' -ErrorAction Stop
        Note 'SmartScreen policy' $smartScreen.SmartScreenEnabled
    } catch { Note 'SmartScreen policy' 'default (no explicit policy value)' }
}

# =======================================================================================
'install-verify' {
    Section 'Installation location'
    Check 'Installed under 64-bit Program Files' (Test-Path -LiteralPath $InstalledExe) $InstalledExe
    if (-not [string]::IsNullOrWhiteSpace($InstallDir32)) {
        Check 'Not installed under Program Files (x86)' (Absent $InstallDir32) $InstallDir32
    }
    if (Test-Path -LiteralPath $InstalledExe) {
        $subsystem = Get-PeSubsystem $InstalledExe
        Check 'Executable is a GUI build (subsystem 2)' ($subsystem -eq 2) "subsystem=$subsystem"
        $vi = (Get-Item -LiteralPath $InstalledExe).VersionInfo
        Note 'Installed version' ("{0} / product {1} / file {2}" -f $vi.ProductName.Trim(), $vi.ProductVersion.Trim(), $vi.FileVersion.Trim())
        $files = Get-ChildItem -LiteralPath $InstallDir -Recurse -File -Force -ErrorAction SilentlyContinue
        Note 'Installed payload' ("{0:N0} files, {1:N1} MB" -f $files.Count, (($files | Measure-Object Length -Sum).Sum / 1MB))
        Check 'Bundled Chromium is present' ((Get-ChildItem -LiteralPath $InstallDir -Recurse -Filter 'chrome.exe' -ErrorAction SilentlyContinue).Count -gt 0) ''
        # PyInstaller --onedir puts bundled data under _internal\, which is what sys._MEIPASS
        # resolves to and therefore where app/paths.py looks. Checking only the top level
        # reported a correctly installed React build as missing.
        $shell = @(@('_internal\frontend\dist\index.html', 'frontend\dist\index.html') |
            ForEach-Object { Join-Path $InstallDir $_ } |
            Where-Object { Test-Path -LiteralPath $_ })
        Check 'Bundled React build is present' ($shell.Count -gt 0) ($shell -join ', ')
        New-Manifest $InstallDir | Set-Content -LiteralPath $ManifestFile -Encoding UTF8
        Note 'Program Files manifest saved' $ManifestFile
    }

    Section 'No developer toolchain was required'
    foreach ($tool in @('python', 'node', 'npm', 'npx', 'vite')) {
        $where = HaveCommand $tool
        Check "$tool still absent after installation" ([string]::IsNullOrEmpty($where)) $where
    }
    Check 'No external Playwright cache was created' (Absent (Join-Path $env:LOCALAPPDATA 'ms-playwright')) ''

    Section 'PostgreSQL (installed by hand; setup must not have changed it)'
    $pgServices = Get-Service -Name 'postgresql*' -ErrorAction SilentlyContinue
    Check 'A PostgreSQL service exists' ($null -ne $pgServices) ''
    foreach ($svc in $pgServices) {
        $wmi = Get-CimInstance Win32_Service -Filter "Name='$($svc.Name)'" -ErrorAction SilentlyContinue
        Check "Service $($svc.Name) is Running" ($svc.Status -eq 'Running') "StartType=$($wmi.StartMode); Account=$($wmi.StartName)"
        Note "Service $($svc.Name) binary" $wmi.PathName
    }
    $pgRoots = Get-ChildItem -LiteralPath 'C:\Program Files\PostgreSQL' -Directory -ErrorAction SilentlyContinue
    Note 'PostgreSQL versions present' (($pgRoots | ForEach-Object { $_.Name }) -join ', ')
    $listener = (netstat -ano -p TCP | Select-String 'LISTENING' | Select-String ':5432\s')
    Note 'Port 5432 listeners' (($listener | ForEach-Object { $_.Line.Trim() }) -join ' | ')

    Section 'Provisioning, migrations and the DPAPI credential'
    Check 'DPAPI credential exists' (Test-Path -LiteralPath (Join-Path $ConfigDir 'database_credential.dat')) ''
    $credPath = Join-Path $ConfigDir 'database_credential.dat'
    if (Test-Path -LiteralPath $credPath) {
        $owner = Get-FileOwner $credPath
        $expected = "$env:USERDOMAIN\$env:USERNAME"
        Check 'Credential belongs to the interactive user' ($owner -eq $expected) "owner=$owner expected=$expected"
        Note 'Credential ACL identities' (Get-AclIdentities $credPath)
        Note 'Credential size' ((Get-Item -LiteralPath $credPath).Length)
    }
    Check 'Secure password handoff was destroyed' (Absent $HandoffFile) $HandoffFile
    if (Test-Path -LiteralPath $HandoffFile) {
        # It should not be here. Its ACL is the evidence of why provisioning failed, so
        # report the permissions -- never the contents deliberately not read.
        Emit '    --- handoff ACL (contents deliberately not read) ---'
        (icacls $HandoffFile) | ForEach-Object { Emit "    $_" }
    }

    # The whole work directory, so a file nobody expected cannot hide in it.
    $leftovers = @(Get-ChildItem -LiteralPath $WorkDir -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne 'installer-result.json' })
    Check 'No other setup material remains in the work directory' ($leftovers.Count -eq 0) (($leftovers | ForEach-Object { $_.Name }) -join ', ')
    if (Test-Path -LiteralPath $ResultFile) {
        Note 'installer-result.json' ((Get-Content -LiteralPath $ResultFile -Raw).Trim())
    }
    Check 'No roaming AppData directory' (Absent (Join-Path $env:APPDATA $DataDirName)) ''
    Check 'Writable data is under LocalAppData' (Test-Path -LiteralPath $DataDir) $DataDir
    if (Test-Path -LiteralPath $DataDir) {
        Note 'LocalAppData subdirectories' ((Get-ChildItem -LiteralPath $DataDir -Directory | ForEach-Object { $_.Name }) -join ', ')
    }

    Emit ''
    Emit "Running $ExeName --check (proves the role, the database, Alembic head and the schema)..."
    $proc = Start-AndWait $InstalledExe @('--check')
    Check '--check succeeded (role + database + Alembic head + schema)' ($proc.ExitCode -eq 0) "exit=$($proc.ExitCode) timedOut=$($proc.TimedOut)"
    if (Test-Path -LiteralPath $LogFile) {
        Emit '--- last 25 log lines ---'
        Get-Content -LiteralPath $LogFile -Tail 25 | ForEach-Object { Emit "    $_" }
        Emit '--- end ---'
    }

    Section 'What the installer itself recorded'
    # SetupLogging=yes, so Inno writes a log per run. Its "PostgreSQL:", "Handoff:" and
    # "Database:" lines say what the gate found, whether the secure handoff worked and what
    # each database step exited with -- the parts of an install nobody can watch happen.
    $setupLogs = @(Get-ChildItem -LiteralPath $env:TEMP -Filter 'Setup Log*.txt' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending)
    if ($setupLogs.Count -eq 0) {
        Note 'Inno Setup log' "none found in $env:TEMP"
    } else {
        $newest = $setupLogs[0]
        Note 'Inno Setup log' $newest.FullName
        $setupLogHits = @(Select-String -LiteralPath $newest.FullName -Pattern 'PostgreSQL:|Handoff:|Database:|Rollback:|Exception|Runtime Error|Setup aborted' -ErrorAction SilentlyContinue)
        if ($setupLogHits.Count -eq 0) {
            Note 'Setup decision lines' 'none (this build predates the gate and handoff logging)'
        } else {
            foreach ($hit in $setupLogHits) { Emit ("    " + $hit.Line.Trim()) }
        }
    }

    Section 'Shortcuts and Add/Remove Programs'
    $startMenu = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\$AppName\$AppName.lnk"
    $desktop = Join-Path ([Environment]::GetFolderPath('CommonDesktopDirectory')) "$AppName.lnk"
    $userDesktop = Join-Path ([Environment]::GetFolderPath('Desktop')) "$AppName.lnk"
    Check 'Start Menu shortcut exists' (Test-Path -LiteralPath $startMenu) $startMenu
    $desktopPresent = (Test-Path -LiteralPath $desktop) -or (Test-Path -LiteralPath $userDesktop)
    Note 'Desktop shortcut' ("present=$desktopPresent (optional -- unticked by default)")
    $arp = Get-UninstallEntry ([Microsoft.Win32.RegistryView]::Registry64)
    if ($null -ne $arp) {
        Check 'Add/Remove Programs entry exists (64-bit view)' $true ''
        Note 'ARP DisplayName' $arp['DisplayName']
        Note 'ARP DisplayVersion' $arp['DisplayVersion']
        Note 'ARP Publisher' $arp['Publisher']
        Note 'ARP UninstallString' $arp['UninstallString']
    } else {
        Check 'Add/Remove Programs entry exists (64-bit view)' $false "HKLM\$UninstallPath"
    }
    $entries = Get-AllProductEntries
    Check 'Exactly one Add/Remove Programs entry' ($entries.Count -eq 1) (($entries -join ', ') + " count=" + $entries.Count)

    Section 'Secrets must never reach the log'
    $hits = Test-LogForSecrets $LogFile
    Check 'No unredacted secret in launcher.log' ($hits.Count -eq 0) ($hits -join ' || ')
}

# =======================================================================================
'runtime' {
    if (-not (Test-Path -LiteralPath $InstalledExe)) {
        Check 'Application is installed' $false $InstalledExe
        break
    }

    Section 'Program Files baseline'
    $before = New-Manifest $InstallDir
    Note 'Files in the installation' $before.Count

    Section 'Launch'
    Remove-Variable -Name app -ErrorAction SilentlyContinue
    $app = Start-Process -FilePath $InstalledExe -PassThru
    Note 'Launched pid' $app.Id
    $ready = $null
    $deadline = (Get-Date).AddSeconds(180)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 1500
        $state = Get-RuntimeState
        if ($state -and $state.ready -eq $true) { $ready = $state; break }
        if ($app.HasExited) { break }
    }
    Check 'Application reached ready state' ($null -ne $ready) ''
    if ($null -eq $ready) {
        if (Test-Path -LiteralPath $LogFile) {
            Emit '--- last 40 log lines ---'
            Get-Content -LiteralPath $LogFile -Tail 40 | ForEach-Object { Emit "    $_" }
        }
        break
    }

    Note 'runtime.json' (Get-Content -LiteralPath $StateFile -Raw).Trim()
    $port = [int]$ready.port
    Check 'Runtime state records this process' ($ready.pid -eq $app.Id) "state pid=$($ready.pid) actual=$($app.Id)"
    Check 'Host is loopback only' ($ready.host -eq '127.0.0.1') "host=$($ready.host)"
    $stateText = (Get-Content -LiteralPath $StateFile -Raw)
    Check 'Runtime state carries no secret' (($stateText -notmatch 'password') -and ($stateText -notmatch '://') -and ($stateText -notmatch 'token')) ''

    Section 'No console window'
    $subsystem = Get-PeSubsystem $InstalledExe
    Check 'Executable subsystem is GUI (2)' ($subsystem -eq 2) "subsystem=$subsystem"
    $conhost = @(Get-Descendants $app.Id | Where-Object { $_.Name -eq 'conhost.exe' })
    Check 'No console host child process' ($conhost.Count -eq 0) ("count=" + $conhost.Count)

    Section 'Network binding'
    $listeners = @(Get-AppListeners $app.Id)
    Note 'Listeners owned by the application' (($listeners | ForEach-Object { $_.Local }) -join ', ')
    $nonLoopback = @($listeners | Where-Object { $_.Local -notlike '127.0.0.1:*' })
    Check 'Every listener is 127.0.0.1' ($nonLoopback.Count -eq 0) (($nonLoopback | ForEach-Object { $_.Local }) -join ', ')
    Check 'Exactly one application listener' ($listeners.Count -eq 1) ("count=" + $listeners.Count)

    Section 'The interface actually serves'
    $health = Invoke-Local "http://127.0.0.1:$port/health"
    Check '/health answers 200' ($null -ne $health -and $health.StatusCode -eq 200) ''
    $root = Invoke-Local "http://127.0.0.1:$port/" 15 $BrowserAccept
    $shellOk = ($null -ne $root) -and ($root.Content -match '<div id="root">')
    $shellDetail = ''
    if (-not $shellOk) { $shellDetail = Describe-Response $root }
    Check 'Dashboard shell is served (React root present)' $shellOk $shellDetail

    # A deep route must return the shell too, or a refresh inside the application 404s.
    $deep = Invoke-Local "http://127.0.0.1:$port/matched-jobs" 15 $BrowserAccept
    $deepOk = ($null -ne $deep) -and ($deep.Content -match '<div id="root">')
    $deepDetail = ''
    if (-not $deepOk) { $deepDetail = Describe-Response $deep }
    Check 'Deep-route refresh returns the shell' $deepOk $deepDetail

    # And the API must NOT be given the shell: same middleware, opposite direction.
    $apiProbe = Invoke-Local "http://127.0.0.1:$port/dashboard/summary"
    Check 'API responses are not replaced by the shell' (($null -ne $apiProbe) -and ($apiProbe.Content -notmatch '<div id="root">')) ''
    $summary = Invoke-Local "http://127.0.0.1:$port/dashboard/summary"
    if ($summary) { Note 'dashboard/summary' $summary.Content }

    Section 'Data fingerprint (for the upgrade and uninstall comparisons)'
    $finger = [ordered]@{
        capturedAt   = (Get-Date).ToString('o')
        version      = $ready.version
        dashboard    = ''
        jobs         = 0
        applications = 0
        resumeFiles  = 0
        credential   = ''
        sessions     = @()
    }
    if ($summary) { $finger.dashboard = $summary.Content }
    $jobs = Invoke-Local "http://127.0.0.1:$port/jobs"
    if ($jobs) { try { $finger.jobs = (@($jobs.Content | ConvertFrom-Json)).Count } catch { } }
    $apps = Invoke-Local "http://127.0.0.1:$port/applications"
    if ($apps) { try { $finger.applications = (@($apps.Content | ConvertFrom-Json)).Count } catch { } }
    $uploads = Join-Path $DataDir 'uploads'
    if (Test-Path -LiteralPath $uploads) {
        $finger.resumeFiles = (Get-ChildItem -LiteralPath $uploads -Recurse -File -ErrorAction SilentlyContinue).Count
    }
    $credPath = Join-Path $ConfigDir 'database_credential.dat'
    if (Test-Path -LiteralPath $credPath) { $finger.credential = (Get-FileHash -LiteralPath $credPath -Algorithm SHA256).Hash }
    $sessionDir = Join-Path $DataDir 'browser_sessions'
    if (Test-Path -LiteralPath $sessionDir) {
        $finger.sessions = @(Get-ChildItem -LiteralPath $sessionDir -File -ErrorAction SilentlyContinue |
            ForEach-Object { "$($_.Name)=$((Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash)" })
    }
    ($finger | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $FingerFile -Encoding UTF8
    Note 'Fingerprint written' $FingerFile
    Note 'Fingerprint' (($finger.GetEnumerator() | Where-Object { $_.Key -ne 'dashboard' } | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join '; ')

    Section 'Second instance'
    $processesBefore = @(Get-Process -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
    $listenersBefore = @(Get-AppListeners 0).Count
    $second = Start-AndWait $InstalledExe @('--no-browser') 120
    Check 'Second instance refused with exit code 3' ($second.ExitCode -eq 3) "exit=$($second.ExitCode) timedOut=$($second.TimedOut)"
    Start-Sleep -Seconds 2
    $listenersAfter = @(Get-AppListeners 0).Count
    Check 'No additional listener was created' ($listenersAfter -eq $listenersBefore) "$listenersBefore -> $listenersAfter"
    $running = @(Get-Process -Name ([IO.Path]::GetFileNameWithoutExtension($ExeName)) -ErrorAction SilentlyContinue)
    Check 'Exactly one application process remains' ($running.Count -eq 1) ("count=" + $running.Count)
    Check 'The original instance is still alive' (-not $app.HasExited) ''

    Section 'Graceful shutdown'
    $allBefore = @(Get-CimInstance Win32_Process | Select-Object -Property ProcessId, Name)
    # The postmaster is the process whose death would actually mean PostgreSQL was killed.
    # Its per-connection backends are a different matter entirely -- see below.
    $pgServiceBefore = @(Get-CimInstance Win32_Service -Filter "Name LIKE 'postgresql%'" |
        Select-Object -Property Name, ProcessId, State)
    $shutdown = Start-AndWait $InstalledExe @('--shutdown') 120
    Check '--shutdown reported success' ($shutdown.ExitCode -eq 0) "exit=$($shutdown.ExitCode) timedOut=$($shutdown.TimedOut)"
    $gone = $false
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        if ($app.HasExited) { $gone = $true; break }
        Start-Sleep -Milliseconds 1000
    }
    Check 'The application process exited' $gone ''
    if ($gone) { Note 'Application exit code' $app.ExitCode }
    Check 'runtime.json was removed' (Absent $StateFile) $StateFile
    Check 'shutdown.token was removed' (Absent $TokenFile) $TokenFile
    Start-Sleep -Seconds 2
    $allAfter = @(Get-CimInstance Win32_Process | Select-Object -Property ProcessId, Name)
    $beforeIds = $allBefore | ForEach-Object { $_.ProcessId }
    $afterIds = $allAfter | ForEach-Object { $_.ProcessId }
    $terminated = @($allBefore | Where-Object { $afterIds -notcontains $_.ProcessId })

    # PostgreSQL forks ONE backend process per connection. When the application closes its
    # connection pool during shutdown, those backends exit BY THEMSELVES -- the server did
    # not stop and nothing killed them. Counting that as "the application terminated an
    # unrelated process" was a false positive: it reported normal, required behaviour as
    # interference. (PC9 reported postgres.exe(5536) here while the service stayed Running.)
    #
    # The exclusion does not weaken the check, because what it was really protecting against
    # -- PostgreSQL being killed -- is now asserted directly and more precisely, against the
    # postmaster PID recorded before shutdown.
    $postgresChurn = @($terminated | Where-Object { $_.Name -eq 'postgres.exe' })
    $unrelated = @($terminated | Where-Object {
        $_.Name -ne $ExeName -and $_.Name -ne 'chrome.exe' -and $_.Name -ne 'conhost.exe' -and
        $_.Name -ne 'WmiPrvSE.exe' -and $_.Name -ne 'postgres.exe'
    })
    if ($postgresChurn.Count -gt 0) {
        Note 'PostgreSQL backends that ended with their connection' (($postgresChurn | ForEach-Object { "$($_.Name)($($_.ProcessId))" }) -join ', ')
    }
    Check 'No unrelated process was terminated' ($unrelated.Count -eq 0) (($unrelated | ForEach-Object { "$($_.Name)($($_.ProcessId))" }) -join ', ')

    $pgAfter = @(Get-Service -Name 'postgresql*' -ErrorAction SilentlyContinue)
    Check 'PostgreSQL is still running after shutdown' (@($pgAfter | Where-Object { $_.Status -eq 'Running' }).Count -gt 0) ''

    # The real question the excluded check was asking, asked properly.
    $pgServiceAfter = @(Get-CimInstance Win32_Service -Filter "Name LIKE 'postgresql%'" |
        Select-Object -Property Name, ProcessId, State)
    $postmasterKilled = @()
    foreach ($svc in $pgServiceBefore) {
        $now = $pgServiceAfter | Where-Object { $_.Name -eq $svc.Name }
        if ($null -eq $now -or $now.ProcessId -ne $svc.ProcessId -or $now.State -ne $svc.State) {
            $postmasterKilled += "$($svc.Name): pid $($svc.ProcessId)/$($svc.State) -> pid $($now.ProcessId)/$($now.State)"
        }
    }
    Check 'The PostgreSQL server process is the same one as before shutdown' ($postmasterKilled.Count -eq 0) ($postmasterKilled -join '; ')

    Section 'Program Files was not modified at runtime'
    $after = New-Manifest $InstallDir
    $diff = Compare-Object -ReferenceObject $before -DifferenceObject $after
    $diffCount = 0
    if ($diff) { $diffCount = @($diff).Count }
    Check 'Installation directory is byte-for-byte unchanged' ($diffCount -eq 0) ("changes=" + $diffCount)
    if ($diffCount -gt 0) { $diff | ForEach-Object { Emit ("    {0} {1}" -f $_.SideIndicator, $_.InputObject) } }

    Section 'Restart preserves the credential and the data'
    $credAfter = ''
    $credPath = Join-Path $ConfigDir 'database_credential.dat'
    if (Test-Path -LiteralPath $credPath) { $credAfter = (Get-FileHash -LiteralPath $credPath -Algorithm SHA256).Hash }
    Check 'Credential survived the run unchanged' ($credAfter -eq $finger.credential -and $credAfter -ne '') ''
    $recheck = Start-AndWait $InstalledExe @('--check')
    Check 'Restart check still succeeds' ($recheck.ExitCode -eq 0) "exit=$($recheck.ExitCode) timedOut=$($recheck.TimedOut)"

    Section 'Secrets must never reach the log'
    $hits = Test-LogForSecrets $LogFile
    Check 'No unredacted secret in launcher.log' ($hits.Count -eq 0) ($hits -join ' || ')
}

# =======================================================================================
'postuninstall' {
    Section 'Removed'
    Check 'Program Files directory is gone' (Absent $InstallDir) $InstallDir
    $startMenu = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\$AppName"
    Check 'Start Menu group is gone' (Absent $startMenu) $startMenu
    Check 'Desktop shortcut is gone' ((Absent (Join-Path ([Environment]::GetFolderPath('CommonDesktopDirectory')) "$AppName.lnk")) -and (Absent (Join-Path ([Environment]::GetFolderPath('Desktop')) "$AppName.lnk"))) ''
    Check 'Add/Remove Programs entry is gone (both views)' ((Get-AllProductEntries).Count -eq 0) ''

    Section 'Preserved'
    Check "%LOCALAPPDATA%\$DataDirName still exists" (Test-Path -LiteralPath $DataDir) $DataDir
    $credPath = Join-Path $ConfigDir 'database_credential.dat'
    Check 'DPAPI credential is preserved' (Test-Path -LiteralPath $credPath) ''
    $pg = Get-Service -Name 'postgresql*' -ErrorAction SilentlyContinue
    Check 'PostgreSQL is still installed and running' (($pg | Where-Object { $_.Status -eq 'Running' }).Count -gt 0) (($pg | ForEach-Object { "$($_.Name)=$($_.Status)" }) -join ', ')
    Check 'PostgreSQL program files remain' (Test-Path -LiteralPath 'C:\Program Files\PostgreSQL') ''
    $uploads = Join-Path $DataDir 'uploads'
    $sessions = Join-Path $DataDir 'browser_sessions'
    Check 'Resume uploads are preserved' (Test-Path -LiteralPath $uploads) ''
    Check 'Portal sessions are preserved' (Test-Path -LiteralPath $sessions) ''

    if (Test-Path -LiteralPath $FingerFile) {
        Section 'Comparison with the pre-uninstall fingerprint'
        $finger = Get-Content -LiteralPath $FingerFile -Raw | ConvertFrom-Json
        $credNow = ''
        if (Test-Path -LiteralPath $credPath) { $credNow = (Get-FileHash -LiteralPath $credPath -Algorithm SHA256).Hash }
        Check 'Credential is byte-for-byte identical' ($credNow -eq $finger.credential -and $credNow -ne '') ''
        $resumeNow = 0
        if (Test-Path -LiteralPath $uploads) { $resumeNow = (Get-ChildItem -LiteralPath $uploads -Recurse -File -ErrorAction SilentlyContinue).Count }
        Check 'Resume file count unchanged' ($resumeNow -eq $finger.resumeFiles) ("before=$($finger.resumeFiles) after=$resumeNow")
        $sessionNow = @()
        if (Test-Path -LiteralPath $sessions) {
            $sessionNow = @(Get-ChildItem -LiteralPath $sessions -File -ErrorAction SilentlyContinue |
                ForEach-Object { "$($_.Name)=$((Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash)" })
        }
        $sessionsSame = ((@($finger.sessions) -join '|') -eq (($sessionNow) -join '|'))
        Check 'Portal session files are byte-for-byte identical' $sessionsSame ''
        Note 'Recorded job/application counts before uninstall' ("jobs=$($finger.jobs) applications=$($finger.applications)")
        Emit 'Database rows are re-verified by reinstalling and comparing /dashboard/summary against the recorded value below.'
        Note 'Recorded dashboard summary' $finger.dashboard
    } else {
        Note 'Fingerprint file' 'not found -- run the runtime stage before uninstalling to enable comparison'
    }

    Section 'Full cleanup remains a separate, explicit procedure'
    Note 'Not performed by uninstall' "database $DbName, role $DbRole, LocalAppData, PostgreSQL itself"
}

} # end switch

Emit ''
Emit "SUMMARY  stage=$Stage  pass=$($script:Pass)  fail=$($script:Fail)  info=$($script:Info)"
if ($script:Fail -eq 0) { Emit 'RESULT: PASS' } else { Emit 'RESULT: FAIL' }

Assert-ReportList
$script:ReportLines | Set-Content -LiteralPath $OutFile -Encoding UTF8
Write-Host ''
Write-Host "Log written to: $OutFile"
Write-Host 'Send this file back.'

if ($script:Fail -eq 0) { exit 0 } else { exit 1 }
