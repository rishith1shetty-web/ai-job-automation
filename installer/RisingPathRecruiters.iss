; Rising Path Recruiters -- Windows installer
;
; Builds one setup executable from the PyInstaller --onedir output. The person installing it
; needs nothing else: no Python, no Node.js, no Playwright, no Chromium download, and no
; manual PostgreSQL, pgAdmin, role, database, .env or Alembic step.
;
; Build it with scripts\build_installer.ps1, which passes the version and identity from
; app\version.py as /D definitions. The defaults below exist only so this file can be opened
; and compiled directly while being worked on.
;
; ---------------------------------------------------------------------------------------
; Two things in here are worth understanding before changing anything.
;
; 1. THE ELEVATION BOUNDARY.
;    This installer runs elevated, because writing to Program Files and installing a Windows
;    service both require it. But the database credential is protected with DPAPI in
;    CURRENT-USER scope, and "current user" for an elevated installer is whichever account
;    approved the UAC prompt -- frequently not the person who will use the application. A
;    credential stored there would be undecryptable by the real user.
;
;    So every step that touches the credential runs through ExecAsOriginalUser. That is safe
;    because provisioning needs no Windows privilege at all: creating a role and a database
;    needs the POSTGRESQL administrator password, which has nothing to do with UAC. Only the
;    PostgreSQL installation itself is elevated.
;
; 2. NOTHING PRIVATE IS INSTALLED, AND NOTHING PRIVATE IS REMOVED.
;    [Files] copies the packaged application and nothing else. Uninstall removes the program
;    directory and the shortcuts; it never touches %LOCALAPPDATA%\AIJobAutomation, the
;    PostgreSQL installation, the database, the role or the stored credential. Erasing those
;    is a separate, explicit action -- see docs\WINDOWS_INSTALLER.md.
; ---------------------------------------------------------------------------------------

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef AppFileVersion
  #define AppFileVersion "1.0.0.0"
#endif
#ifndef AppName
  #define AppName "Rising Path Recruiters"
#endif
#ifndef AppPublisher
  #define AppPublisher "Rising Path Recruiters"
#endif
#ifndef ExeName
  #define ExeName "RisingPathRecruiters"
#endif
#ifndef AppIdValue
  ; Doubled leading brace: Inno reads a single "{" as the start of a constant.
  #define AppIdValue "{{7B3D9E42-5C18-4A6F-9E27-1D4A8F0C6B35}"
#endif
#ifndef InstallDirName
  #define InstallDirName "Rising Path Recruiters"
#endif
#ifndef StartMenuGroup
  #define StartMenuGroup "Rising Path Recruiters"
#endif
#ifndef DataDirName
  #define DataDirName "AIJobAutomation"
#endif
#ifndef AppDescription
  #define AppDescription "AI recruitment platform"
#endif
#ifndef AppCopyright
  #define AppCopyright "Copyright (C) Rising Path Recruiters"
#endif

; Where the packaged application and the pinned prerequisite live, relative to this file.
#ifndef PayloadDir
  #define PayloadDir "..\dist\RisingPathRecruiters"
#endif
#ifndef PrerequisiteDir
  #define PrerequisiteDir "prerequisites"
#endif
#ifndef PostgresInstaller
  #define PostgresInstaller "postgresql-16.9-1-windows-x64.exe"
#endif
#ifndef PostgresVersion
  #define PostgresVersion "16.9-1"
#endif
#ifndef OutputDir
  #define OutputDir "..\release"
#endif

; Built from the project's own logo by scripts/make_icon.py. The same file is used for the
; setup program, and the executable carries it as a resource for the shortcuts.
#ifndef IconFile
  #define IconFile "RisingPathRecruiters.ico"
#endif

; Set /DIncludePostgres=0 to build a smaller, online-only installer.
#ifndef IncludePostgres
  #define IncludePostgres "1"
#endif

[Setup]
AppId={#AppIdValue}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
VersionInfoVersion={#AppFileVersion}
VersionInfoProductVersion={#AppFileVersion}
VersionInfoProductName={#AppName}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} Setup
VersionInfoCopyright={#AppCopyright}
AppPublisher={#AppPublisher}
AppComments={#AppDescription}

DefaultDirName={autopf}\{#InstallDirName}
DefaultGroupName={#StartMenuGroup}
; The install location is a deliberate, supported choice; the data location is not, so the
; directory page stays and the user keeps control of where the program goes.
DisableDirPage=no
DisableProgramGroupPage=yes
UninstallDisplayName={#AppName} {#AppVersion}
; The executable carries the icon as a resource, so Add/Remove Programs and the shortcuts
; all show the same artwork without a second copy of it being installed.
UninstallDisplayIcon={app}\{#ExeName}.exe
; The setup program's own icon, and the icon shown in the wizard.
SetupIconFile={#IconFile}

; Per-machine install into Program Files. Administrator rights are needed for that and for
; installing PostgreSQL as a service -- and for nothing else; every step that does not
; require them runs as the original user.
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

WizardStyle=modern
WizardSizePercent=110
ShowLanguageDialog=no
AllowNoIcons=yes

OutputDir={#OutputDir}
OutputBaseFilename={#ExeName}-Setup-{#AppVersion}
; LZMA2 at maximum, but NOT solid. The payload is dominated by Chromium and by compiled
; Python extensions, which are already-compressed binaries: solid compression buys very
; little on them while pushing build time and decompression memory up sharply on a ~1 GB
; payload. LZMANumBlockThreads keeps the build parallel.
Compression=lzma2/max
SolidCompression=no
LZMANumBlockThreads=4
; The payload is far larger than the 2 GB single-disk default assumption.
DiskSpanning=no
UsePreviousAppDir=yes
UsePreviousGroup=yes
; Inno's own detection of files in use, plus the explicit shutdown in [Code] below.
CloseApplications=yes
CloseApplicationsFilter=*.exe
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Off by default: a desktop icon is a preference, not a default.
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
; The packaged application, exactly as scripts\build_windows.ps1 produced it. Nothing from
; the source checkout, .venv, node_modules, tests, outputs or the user's data is referenced
; anywhere in this section -- scripts\build_installer.ps1 fails the build if any appears.
Source: "{#PayloadDir}\{#ExeName}.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

#if IncludePostgres == "1"
; The pinned official PostgreSQL installer, embedded but NOT copied anywhere by default.
;
; "dontcopy" rather than "DestDir + Check:" deliberately. A Check function is evaluated while
; Inno is processing file entries -- which happens BEFORE {app}\RisingPathRecruiters.exe is on
; disk, so a Check that asks the application whether PostgreSQL is present cannot work: it
; would fail on exactly the machines that need the prerequisite most. Instead the file is
; carried inside the setup and pulled out by ExtractTemporaryFile in InstallPostgres, which
; runs after installation, when detection has a working executable to ask.
Source: "{#PrerequisiteDir}\{#PostgresInstaller}"; Flags: dontcopy noencryption
#endif

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#ExeName}.exe"; Comment: "{#AppDescription}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#ExeName}.exe"; Comment: "{#AppDescription}"; Tasks: desktopicon

[Run]
; Offered, never automatic. The application itself opens the browser only once its own
; interface answers, so there is no race between the shortcut and the server.
Filename: "{app}\{#ExeName}.exe"; Description: "Start {#AppName}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallDelete]
; The program directory only. PyInstaller writes nothing here at runtime, but an
; interrupted upgrade can leave an empty tree behind.
Type: dirifempty; Name: "{app}\_internal"
Type: dirifempty; Name: "{app}"

[Messages]
; Nothing in this installer prints a password; these strings are also secret-free.
SetupAppTitle={#AppName} Setup
SetupWindowTitle={#AppName} {#AppVersion} Setup

[Code]
const
  { Exit codes from the packaged application's --installer actions (app/installer_bridge.py). }
  EXIT_OK                 = 0;
  EXIT_INVALID_USAGE      = 2;
  EXIT_POSTGRES_MISSING   = 10;
  EXIT_PORT_OCCUPIED      = 11;
  EXIT_ADMIN_AUTH_FAILED  = 12;
  EXIT_PROVISION_FAILED   = 13;
  EXIT_MIGRATION_FAILED   = 14;
  EXIT_VERIFICATION_FAIL  = 15;
  EXIT_CREDENTIAL_FAILED  = 16;
  EXIT_POSTGRES_STOPPED   = 20;
  EXIT_NO_ADMIN_PASSWORD  = 21;
  EXIT_UNEXPECTED         = 22;

  { How long to wait for a running copy to shut down before asking the user. }
  ShutdownWaitMs = 20000;

var
  PostgresState: Integer;      { one of the EXIT_* values from the detect action }
  PostgresChecked: Boolean;
  ProvisioningSummary: String;

{ ------------------------------------------------------------------ helpers }

function AppExe: String;
begin
  Result := ExpandConstant('{app}\{#ExeName}.exe');
end;

function RunAppAsUser(const Args: String; var ResultCode: Integer): Boolean;
{ Runs a packaged --installer action as the ORIGINAL interactive user, so anything it stores
  with DPAPI belongs to the account that will actually start the application. }
begin
  Result := ExecAsOriginalUser(AppExe, Args, ExpandConstant('{app}'),
                               SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function DetectPostgres: Integer;
var
  ResultCode: Integer;
begin
  if PostgresChecked then
  begin
    Result := PostgresState;
    Exit;
  end;
  { Detection is read-only and needs no privilege, so it runs as the original user too. }
  if not RunAppAsUser('--installer detect', ResultCode) then
    ResultCode := EXIT_UNEXPECTED;
  PostgresState := ResultCode;
  PostgresChecked := True;
  Result := ResultCode;
end;

function NeedsPostgresInstall: Boolean;
{ The single decision point: the embedded prerequisite is only ever unpacked and run when
  this computer genuinely has no usable PostgreSQL. An existing compatible server -- whatever
  its port -- is never reinstalled, never upgraded and never even extracted over. }
begin
  Result := (DetectPostgres = EXIT_POSTGRES_MISSING);
end;

function ProblemText(Code: Integer): String;
{ One place that turns an exit code into something a person can act on. No code path here
  can include a password or a connection URL -- the values are all fixed strings. }
begin
  case Code of
    EXIT_PORT_OCCUPIED:
      Result := 'Port 5432 is being used by a program that is not PostgreSQL.' + #13#10#13#10 +
                'Stop that program, or install PostgreSQL on a different port, and run this ' +
                'setup again.';
    EXIT_POSTGRES_STOPPED:
      Result := 'PostgreSQL is installed on this computer but is not running.' + #13#10#13#10 +
                'Start the PostgreSQL service in Windows Services, then run this setup again.';
    EXIT_ADMIN_AUTH_FAILED:
      Result := 'The PostgreSQL administrator password was not accepted.' + #13#10#13#10 +
                'The application has been installed. Start it and complete the database ' +
                'setup, or run setup again with the correct password.';
    EXIT_NO_ADMIN_PASSWORD:
      Result := 'The database could not be set up because no PostgreSQL administrator ' +
                'password was available.' + #13#10#13#10 +
                'The application has been installed. Start it to finish the database setup.';
    EXIT_MIGRATION_FAILED:
      Result := 'The database could not be brought up to date.' + #13#10#13#10 +
                'No existing data has been changed. See the log file in ' +
                '%LOCALAPPDATA%\{#DataDirName}\logs.';
    EXIT_VERIFICATION_FAIL:
      Result := 'The database schema did not match what this version expects.' + #13#10#13#10 +
                'No existing data has been changed. See the log file in ' +
                '%LOCALAPPDATA%\{#DataDirName}\logs.';
    EXIT_CREDENTIAL_FAILED:
      Result := 'The database credential could not be stored securely for your Windows ' +
                'account.' + #13#10#13#10 +
                'The application has been installed. Start it to finish the setup.';
    EXIT_PROVISION_FAILED:
      Result := 'The application database could not be prepared.' + #13#10#13#10 +
                'See the log file in %LOCALAPPDATA%\{#DataDirName}\logs.';
  else
    Result := 'The database setup step did not complete.' + #13#10#13#10 +
              'The application has been installed. Start it to finish the setup.';
  end;
end;

{ ------------------------------------------------ running-process handling }

function AppIsRunning: Boolean;
var
  ResultCode: Integer;
begin
  { tasklist filtered to the exact image name. Deliberately narrow: unrelated python.exe,
    chrome.exe and postgres.exe processes must never be affected. }
  Result := Exec(ExpandConstant('{cmd}'),
                 '/C tasklist /FI "IMAGENAME eq {#ExeName}.exe" | find /I "{#ExeName}.exe"',
                 '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function InstalledExePath: String;
{ The already-installed executable, which is the one that can be asked to stop. During an
  upgrade or uninstall it is still on disk from the previous installation. }
begin
  Result := ExpandConstant('{app}\{#ExeName}.exe');
end;

function RequestGracefulShutdown: Boolean;
{ Ask the running application to stop ITSELF.

  The application exposes a loopback-only, token-authenticated shutdown action; the token is
  a per-run random value stored in the user's own data directory. "--shutdown" reads the
  published runtime state, sends the request to the exact recorded process, and waits for it
  to exit. It returns 0 when the application stopped or was not running.

  This is why the installer never needs taskkill. A killed process would abandon its lock,
  leave its runtime state behind, and could be interrupted mid-way through a portal session
  -- leaving a half-written LinkedIn or Dice session file and an orphaned browser. }
var
  ResultCode: Integer;
  Exe: String;
begin
  Result := False;
  Exe := InstalledExePath;
  if not FileExists(Exe) then
    Exit;

  { As the original user: the runtime state and the shutdown token live in THAT account's
    LocalAppData, and an elevated installer would look in the wrong profile. }
  if ExecAsOriginalUser(Exe, '--shutdown', ExpandConstant('{app}'), SW_HIDE,
                        ewWaitUntilTerminated, ResultCode) then
    Result := (ResultCode = 0);
end;

function AskUserToCloseApp: Boolean;
var
  Waited: Integer;
begin
  Result := True;
  if not AppIsRunning then
    Exit;

  { 1. Ask the application to close itself, cleanly. Nothing is forced. }
  if RequestGracefulShutdown then
  begin
    Waited := 0;
    while AppIsRunning and (Waited < ShutdownWaitMs) do
    begin
      Sleep(500);
      Waited := Waited + 500;
    end;
    if not AppIsRunning then
      Exit;
  end;

  { 2. It did not stop -- perhaps it is mid-task, or the graceful path is unavailable
       because this is an upgrade from a version that predates it. Ask the person. Still
       nothing is forced: no taskkill, and no process belonging to Python, Chromium or
       PostgreSQL is touched at any point. }
  if MsgBox('{#AppName} is currently running.' + #13#10#13#10 +
            'Please close it, then click OK to continue.',
            mbConfirmation, MB_OKCANCEL) <> IDOK then
  begin
    Result := False;
    Exit;
  end;

  Waited := 0;
  while AppIsRunning and (Waited < ShutdownWaitMs) do
  begin
    Sleep(500);
    Waited := Waited + 500;
  end;

  if AppIsRunning then
  begin
    MsgBox('{#AppName} is still running. Close it and start this setup again.',
           mbError, MB_OK);
    Result := False;
  end;
end;

{ ------------------------------------------------------------ wizard hooks }

function InitializeSetup: Boolean;
begin
  Result := AskUserToCloseApp;
end;

function InitializeUninstall: Boolean;
begin
  Result := AskUserToCloseApp;
end;

procedure InstallPostgres;
var
  ResultCode: Integer;
  OptionFile: String;
  Installer: String;
begin
  { 1. As the ORIGINAL user: generate a temporary administrator password and write it into a
       protected option file inside that user's own data directory. The password is never
       returned to this script, never printed, and never placed on a command line. }
  if not RunAppAsUser('--installer prepare-postgres', ResultCode) or (ResultCode <> EXIT_OK) then
  begin
    MsgBox('The PostgreSQL installation could not be prepared.', mbError, MB_OK);
    Exit;
  end;

  OptionFile := ExpandConstant('{localappdata}\{#DataDirName}\work\postgres-setup.options');

  { 2. Unpack the embedded prerequisite, now that it is known to be needed. Setup removes its
       temporary directory when it finishes, so nothing is left behind. }
#if IncludePostgres == "1"
  ExtractTemporaryFile('{#PostgresInstaller}');
#endif
  Installer := ExpandConstant('{tmp}\{#PostgresInstaller}');
  if not FileExists(Installer) then
  begin
    MsgBox('This setup does not include the PostgreSQL installer.' + #13#10#13#10 +
           'Install PostgreSQL yourself, then start the application to finish the database ' +
           'setup.', mbError, MB_OK);
    Exit;
  end;

  { 3. ELEVATED: the only step that genuinely needs administrator rights. The command line
       carries the path to the option file and no secret whatsoever. }
  if not Exec(Installer, '--optionfile "' + OptionFile + '"', '',
              SW_SHOW, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
    MsgBox('PostgreSQL {#PostgresVersion} could not be installed.' + #13#10#13#10 +
           'The application has been installed. Install PostgreSQL yourself and then start ' +
           'the application to finish the database setup.', mbError, MB_OK);
end;

procedure ProvisionDatabase;
var
  ResultCode: Integer;
begin
  { Already provisioned -- the normal case for an upgrade. Nothing is recreated, no password
    is rotated, and the existing database is left exactly as it is. }
  if RunAppAsUser('--installer status', ResultCode) and (ResultCode = EXIT_OK) then
  begin
    ProvisioningSummary := 'The existing database was kept.';
    Exit;
  end;

  { As the ORIGINAL user, so the DPAPI credential belongs to the account that will run the
    application. This step reads the administrator password from the protected option file
    and destroys that file whether it succeeds or fails. }
  if not RunAppAsUser('--installer provision', ResultCode) then
    ResultCode := EXIT_UNEXPECTED;

  if ResultCode = EXIT_OK then
    ProvisioningSummary := 'The application database is ready.'
  else
  begin
    ProvisioningSummary := 'The database setup did not finish.';
    MsgBox(ProblemText(ResultCode), mbError, MB_OK);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep <> ssPostInstall then
    Exit;

  { Order matters: PostgreSQL must exist before the application database can be prepared.
    Detection runs here, after installation, because it asks the application itself -- which
    is only on disk from this point onwards. }
  if NeedsPostgresInstall then
    InstallPostgres
  else if DetectPostgres <> EXIT_OK then
  begin
    MsgBox(ProblemText(DetectPostgres), mbError, MB_OK);
    Exit;
  end;

  ProvisionDatabase;
end;
