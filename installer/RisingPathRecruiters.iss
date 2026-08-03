; Rising Path Recruiters -- Windows installer
;
; Builds one setup executable from the PyInstaller --onedir output. The person installing it
; needs no Python, no Node.js, no Playwright and no Chromium download, and does no pgAdmin,
; role, database, .env or Alembic step by hand.
;
; ONE prerequisite is theirs: PostgreSQL 16 for Windows x86-64, installed before this setup
; runs, on port 5432, with an administrator password they chose and remember. This setup does
; not download, bundle or install PostgreSQL. It verifies it, asks for that password on its
; own page, and does everything else.
;
; Build it with scripts\build_installer.ps1, which passes the version and identity from
; app\version.py as /D definitions. The defaults below exist only so this file can be opened
; and compiled directly while being worked on.
;
; ---------------------------------------------------------------------------------------
; Two things in here are worth understanding before changing anything.
;
; 1. THE ELEVATION BOUNDARY.
;    This installer runs elevated, because writing to Program Files requires it. But the
;    database credential is protected with DPAPI in CURRENT-USER scope, and "current user"
;    for an elevated installer is whichever account approved the UAC prompt -- frequently not
;    the person who will use the application. A credential stored there would be
;    undecryptable by the real user.
;
;    So every step that touches the credential runs through ExecAsOriginalUser. That is safe
;    because provisioning needs no Windows privilege at all: creating a role and a database
;    needs the POSTGRESQL administrator password, which has nothing to do with UAC.
;
;    The administrator password itself has to cross that boundary, because it is typed into
;    this elevated wizard and used by an unelevated child process. It crosses it as DPAPI
;    CIPHERTEXT: CryptProtectData here, CryptUnprotectData there, with only the blob ever
;    reaching the filesystem -- and that file additionally carries an ACL naming the original
;    user's SID. A user-scope DPAPI key belongs to the ACCOUNT, not to the token's elevation
;    level, which is exactly why the same person elevating themselves can read it back and a
;    different administrator approving the prompt cannot. The second case fails loudly with
;    an instruction rather than quietly storing an unusable credential.
;
; 2. NOTHING PRIVATE IS INSTALLED, AND NOTHING PRIVATE IS REMOVED.
;    [Files] copies the packaged application and nothing else. Uninstall removes the program
;    directory and the shortcuts; it never touches %LOCALAPPDATA%\AIJobAutomation, the
;    PostgreSQL installation, the database, the role or the stored credential. Erasing those
;    is a separate, explicit action -- see docs\WINDOWS_INSTALLER.md.
; ---------------------------------------------------------------------------------------

#ifndef AppVersion
  #define AppVersion "1.1.0"
#endif
#ifndef AppFileVersion
  #define AppFileVersion "1.1.0.0"
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

; Where the packaged application lives, relative to this file.
#ifndef PayloadDir
  #define PayloadDir "..\dist\RisingPathRecruiters"
#endif
#ifndef OutputDir
  #define OutputDir "..\release"
#endif

; PostgreSQL is NOT installed by this setup. It is installed by hand beforehand; setup
; refuses to install anything until it finds a server of at least this major version
; answering on the port below. Keep in step with installer\prerequisites.json.
#ifndef RequiredPostgresMajor
  #define RequiredPostgresMajor "16"
#endif
#ifndef PostgresPort
  #define PostgresPort "5432"
#endif

; Built from the project's own logo by scripts/make_icon.py. The same file is used for the
; setup program, and the executable carries it as a resource for the shortcuts.
#ifndef IconFile
  #define IconFile "RisingPathRecruiters.ico"
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
; nothing else -- in particular, no part of the database setup needs them, so every one of
; those steps runs as the original user.
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

; NOTHING ELSE. There is deliberately no PostgreSQL installer in this payload: the database
; server is installed by the person doing the installation, before setup runs, and setup
; verifies it rather than providing it. Removing the embedded 334 MB prerequisite is what
; makes this setup a fraction of its former size.

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
  EXIT_OK                    = 0;
  EXIT_INVALID_USAGE         = 2;
  EXIT_POSTGRES_MISSING      = 10;
  EXIT_PORT_OCCUPIED         = 11;
  EXIT_ADMIN_AUTH_FAILED     = 12;
  EXIT_PROVISION_FAILED      = 13;
  EXIT_MIGRATION_FAILED      = 14;
  EXIT_VERIFICATION_FAIL     = 15;
  EXIT_CREDENTIAL_FAILED     = 16;
  EXIT_POSTGRES_STOPPED      = 20;
  EXIT_NO_ADMIN_PASSWORD     = 21;
  EXIT_UNEXPECTED            = 22;
  EXIT_POSTGRES_WRONG_VER    = 23;
  EXIT_HANDOFF_FAILED        = 24;

  { How long to wait for a running copy to shut down before asking the user. }
  ShutdownWaitMs = 20000;

  { DPAPI. UI_FORBIDDEN because a wizard must never be blocked by a system prompt. }
  CRYPTPROTECT_UI_FORBIDDEN = $1;
  HandoffDescription = 'Rising Path Recruiters setup';

  CP_UTF8 = 65001;
  GMEM_FIXED = $0000;

  { How many times the person may correct a mistyped password before setup gives up. }
  MaxPasswordAttempts = 5;

type
  TDataBlob = record
    cbData: Cardinal;
    pbData: Cardinal;
  end;

{ ------------------------------------------------------------------- Win32 }

function CryptProtectData(var pDataIn: TDataBlob; szDataDescr: String;
  pOptionalEntropy, pvReserved, pPromptStruct: Cardinal; dwFlags: Cardinal;
  var pDataOut: TDataBlob): Cardinal;
  external 'CryptProtectData@crypt32.dll stdcall';

function CryptUnprotectData(var pDataIn: TDataBlob; ppszDataDescr: Cardinal;
  pOptionalEntropy, pvReserved, pPromptStruct: Cardinal; dwFlags: Cardinal;
  var pDataOut: TDataBlob): Cardinal;
  external 'CryptUnprotectData@crypt32.dll stdcall';

function GlobalAlloc(uFlags, dwBytes: Cardinal): Cardinal;
  external 'GlobalAlloc@kernel32.dll stdcall';
function GlobalFree(hMem: Cardinal): Cardinal;
  external 'GlobalFree@kernel32.dll stdcall';
function LocalFree(hMem: Cardinal): Cardinal;
  external 'LocalFree@kernel32.dll stdcall';

function WideCharToMultiByte(CodePage, dwFlags: Cardinal; lpWideCharStr: String;
  cchWideChar: Integer; lpMultiByteStr: Cardinal; cbMultiByte: Integer;
  lpDefaultChar, lpUsedDefaultChar: Cardinal): Integer;
  external 'WideCharToMultiByte@kernel32.dll stdcall';

{ Two views of RtlMoveMemory. Pascal Script needs one declaration per direction, because the
  buffer is a managed array on one side and a raw address on the other. }
procedure CopyFromAddress(var Dest: Byte; Source: Cardinal; Len: Cardinal);
  external 'RtlMoveMemory@kernel32.dll stdcall';
procedure CopyToAddress(Dest: Cardinal; var Source: Byte; Len: Cardinal);
  external 'RtlMoveMemory@kernel32.dll stdcall';

var
  PasswordPage: TWizardPage;
  PasswordEdit: TPasswordEdit;
  { CIPHERTEXT ONLY. The typed password is read straight out of the edit control into the
    DPAPI call and is never assigned to a script variable, because a Pascal Script string is
    reference-counted and reallocated and cannot be reliably overwritten afterwards. }
  HandoffHex: String;
  HandoffFile: String;
  ProvisioningSummary: String;
  PostgresBinDir: String;
  PostgresMajor: Integer;
  PostgresServiceName: String;
  RolledBack: Boolean;

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

function PostgresInstructions: String;
begin
  Result := 'Install PostgreSQL {#RequiredPostgresMajor} for Windows x86-64 first.' + #13#10#13#10 +
            'Download it from https://www.postgresql.org/download/windows/' + #13#10 +
            'Keep the default port {#PostgresPort}.' + #13#10 +
            'Choose a password for the "postgres" administrator account and remember it -- ' +
            'this setup will ask you for it.' + #13#10#13#10 +
            'Then run this setup again.';
end;

{ ------------------------------------------- finding the PostgreSQL server }

function ReadInstallationValue(const SubKey, ValueName: String; var Value: String): Boolean;
begin
  Result := RegQueryStringValue(HKLM64, 'SOFTWARE\PostgreSQL\Installations\' + SubKey,
                                ValueName, Value);
end;

function MajorFromVersion(const Version: String): Integer;
var
  Digits: String;
  Index: Integer;
begin
  { "16.9-1" and "16" both mean 16. Anything unparseable means "not known", never zero-as-ok. }
  Digits := '';
  Index := 1;
  while (Index <= Length(Version)) and (Version[Index] >= '0') and (Version[Index] <= '9') do
  begin
    Digits := Digits + Version[Index];
    Index := Index + 1;
  end;
  if Digits = '' then
    Result := 0
  else
    Result := StrToIntDef(Digits, 0);
end;

procedure FindPostgres;
{ Reads the registry only. Records the newest supported installation found, its bin directory
  and its service name -- everything the checks below need, gathered once. }
var
  Keys: TArrayOfString;
  Index, Major: Integer;
  Version, Base, Service: String;
begin
  PostgresMajor := 0;
  PostgresBinDir := '';
  PostgresServiceName := '';

  if not RegGetSubkeyNames(HKLM64, 'SOFTWARE\PostgreSQL\Installations', Keys) then
    Exit;

  for Index := 0 to GetArrayLength(Keys) - 1 do
  begin
    if not ReadInstallationValue(Keys[Index], 'Version', Version) then
      Continue;
    Major := MajorFromVersion(Version);
    if Major <= PostgresMajor then
      Continue;
    if not ReadInstallationValue(Keys[Index], 'Base Directory', Base) then
      Continue;

    PostgresMajor := Major;
    PostgresBinDir := AddBackslash(Base) + 'bin';
    if ReadInstallationValue(Keys[Index], 'Service ID', Service) then
      PostgresServiceName := Service
    else
      PostgresServiceName := 'postgresql-x64-' + IntToStr(Major);
  end;
  Log('PostgreSQL: registry reports major ' + IntToStr(PostgresMajor) +
      ', service "' + PostgresServiceName + '", bin "' + PostgresBinDir + '"');
end;

function ServiceIsRunning(const Name: String): Boolean;
var
  ResultCode: Integer;
begin
  { "sc query" prints the state; find returns 1 when RUNNING is absent. No output is captured
    and nothing is changed -- this only reads. }
  Result := Exec(ExpandConstant('{cmd}'),
                 '/C sc query "' + Name + '" | find /I "RUNNING" > nul',
                 '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function ServerIsAccepting: Boolean;
var
  ResultCode: Integer;
  Tool: String;
begin
  { pg_isready ships with every PostgreSQL installation and does exactly this one job: open a
    connection to the port and report whether the server answers. It needs no credentials,
    so it can run before the password page. }
  Result := False;
  if PostgresBinDir = '' then
    Exit;
  Tool := AddBackslash(PostgresBinDir) + 'pg_isready.exe';
  if not FileExists(Tool) then
  begin
    Log('PostgreSQL: pg_isready.exe is not at ' + Tool);
    Exit;
  end;
  Result := Exec(Tool, '-h localhost -p {#PostgresPort} -q', '', SW_HIDE,
                 ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
  Log('PostgreSQL: pg_isready exit code ' + IntToStr(ResultCode));
end;

function RequirePostgres: Boolean;
{ The gate. Runs before the wizard appears, so a computer without a usable PostgreSQL is told
  what to do and NOTHING is installed on it. Each failure names one problem and its fix. }
begin
  Result := False;
  FindPostgres;

  if PostgresMajor = 0 then
  begin
    Log('PostgreSQL: no installation found in the registry.');
    MsgBox('PostgreSQL is not installed on this computer.' + #13#10#13#10 +
           PostgresInstructions, mbError, MB_OK);
    Exit;
  end;

  if PostgresMajor < {#RequiredPostgresMajor} then
  begin
    Log('PostgreSQL: found major ' + IntToStr(PostgresMajor) + ', which is too old.');
    MsgBox('This computer has PostgreSQL ' + IntToStr(PostgresMajor) + ', but ' +
           '{#AppName} requires PostgreSQL {#RequiredPostgresMajor} or newer.' + #13#10#13#10 +
           PostgresInstructions, mbError, MB_OK);
    Exit;
  end;

  if not ServiceIsRunning(PostgresServiceName) then
  begin
    Log('PostgreSQL: service "' + PostgresServiceName + '" is not running.');
    MsgBox('PostgreSQL ' + IntToStr(PostgresMajor) + ' is installed on this computer, but ' +
           'its Windows service is not running.' + #13#10#13#10 +
           'Open Services, start "' + PostgresServiceName + '", then run this setup again.',
           mbError, MB_OK);
    Exit;
  end;

  if not ServerIsAccepting then
  begin
    Log('PostgreSQL: nothing is answering on port {#PostgresPort}.');
    MsgBox('PostgreSQL is running, but nothing is answering on localhost:{#PostgresPort}.' + #13#10#13#10 +
           'This setup needs PostgreSQL on port {#PostgresPort}. Change the server back to ' +
           'that port, or free it, then run this setup again.', mbError, MB_OK);
    Exit;
  end;

  Log('PostgreSQL: major ' + IntToStr(PostgresMajor) + ' is running and accepting ' +
      'connections on port {#PostgresPort}.');
  Result := True;
end;

{ ------------------------------------------------------ the secure handoff }

function ProtectToHex(const Secret: String; var Hex: String): Boolean;
{ DPAPI-protect a string for the CURRENT WINDOWS ACCOUNT and return the ciphertext as hex.

  The plain text exists as UTF-8 bytes in one GlobalAlloc'd buffer, which is overwritten and
  freed before this returns. Nothing readable is ever written to disk, a log or a variable. }
var
  Zeros, Blob: array of Byte;
  Needed, Index: Integer;
  Buffer: Cardinal;
  Source, Target: TDataBlob;
  Digits: String;
begin
  Result := False;
  Hex := '';

  Needed := WideCharToMultiByte(CP_UTF8, 0, Secret, Length(Secret), 0, 0, 0, 0);
  if Needed <= 0 then
    Exit;

  Buffer := GlobalAlloc(GMEM_FIXED, Needed);
  if Buffer = 0 then
    Exit;
  try
    if WideCharToMultiByte(CP_UTF8, 0, Secret, Length(Secret), Buffer, Needed, 0, 0) <> Needed then
      Exit;

    Source.cbData := Needed;
    Source.pbData := Buffer;
    Target.cbData := 0;
    Target.pbData := 0;

    if CryptProtectData(Source, HandoffDescription, 0, 0, 0,
                        CRYPTPROTECT_UI_FORBIDDEN, Target) = 0 then
    begin
      Log('Handoff: CryptProtectData failed.');
      Exit;
    end;

    try
      SetArrayLength(Blob, Target.cbData);
      CopyFromAddress(Blob[0], Target.pbData, Target.cbData);
    finally
      LocalFree(Target.pbData);
    end;

    Digits := '0123456789abcdef';
    for Index := 0 to GetArrayLength(Blob) - 1 do
      Hex := Hex + Digits[(Blob[Index] shr 4) + 1] + Digits[(Blob[Index] and $0F) + 1];
    Result := True;
  finally
    { Overwrite the plain-text bytes before the memory goes back to the process heap. }
    SetArrayLength(Zeros, Needed);
    CopyToAddress(Buffer, Zeros[0], Needed);
    GlobalFree(Buffer);
  end;
end;

function HandoffRoundTrips(const Hex: String): Boolean;
{ Can this account read back what it just protected?

  Asked before setup relies on the channel, so that a computer where DPAPI is unavailable
  fails on the password page rather than half-way through provisioning. Only the LENGTH of
  the recovered value is looked at -- the value itself is discarded immediately. }
var
  Blob: array of Byte;
  Index, Count: Integer;
  Buffer: Cardinal;
  Source, Target: TDataBlob;
begin
  Result := False;
  Count := Length(Hex) div 2;
  if Count = 0 then
    Exit;

  SetArrayLength(Blob, Count);
  for Index := 0 to Count - 1 do
    Blob[Index] := StrToIntDef('$' + Copy(Hex, Index * 2 + 1, 2), 0);

  Buffer := GlobalAlloc(GMEM_FIXED, Count);
  if Buffer = 0 then
    Exit;
  try
    CopyToAddress(Buffer, Blob[0], Count);
    Source.cbData := Count;
    Source.pbData := Buffer;
    Target.cbData := 0;
    Target.pbData := 0;
    if CryptUnprotectData(Source, 0, 0, 0, 0, CRYPTPROTECT_UI_FORBIDDEN, Target) = 0 then
    begin
      Log('Handoff: this account cannot read back its own protected value.');
      Exit;
    end;
    Result := Target.cbData > 0;
    LocalFree(Target.pbData);
  finally
    GlobalFree(Buffer);
  end;
end;

function CaptureTypedPassword(Edit: TPasswordEdit): Boolean;
{ Take what was typed, protect it, and keep only the ciphertext. The edit control is cleared
  immediately afterwards so the plain text does not sit in a window handle for the rest of
  the wizard. }
begin
  Result := ProtectToHex(Edit.Text, HandoffHex);
  Edit.Text := '';
  if Result then
    Result := HandoffRoundTrips(HandoffHex);
  if not Result then
    HandoffHex := '';
end;

function WriteHandoffFile: Boolean;
{ Put the ciphertext into the file the original user's process already created and locked
  down. Only hexadecimal ASCII is written -- SaveStringToFile's encoding cannot corrupt it. }
begin
  Result := False;
  if (HandoffFile = '') or (HandoffHex = '') then
    Exit;
  if not SaveStringToFile(HandoffFile, HandoffHex, False) then
  begin
    Log('Handoff: could not write the protected value.');
    Exit;
  end;
  Result := True;
end;

procedure DestroyHandoff;
var
  ResultCode: Integer;
begin
  HandoffHex := '';
  if FileExists(AppExe) then
  begin
    if not RunAppAsUser('--installer cleanup', ResultCode) then
      ResultCode := EXIT_UNEXPECTED;
    Log('Handoff: cleanup exit code ' + IntToStr(ResultCode));
  end;
  { A cleanup that did not run leaves ciphertext, not a password. Deleting it here as well
    costs nothing and makes "nothing remains" true on both paths. }
  if (HandoffFile <> '') and FileExists(HandoffFile) then
    DeleteFile(HandoffFile);
end;

{ --------------------------------------------------------- the wizard page }

procedure CreatePasswordPage;
var
  Intro, Prompt, Fixed: TNewStaticText;
begin
  PasswordPage := CreateCustomPage(wpSelectDir, 'PostgreSQL administrator password',
    'Setup needs to create this application''s own database inside your PostgreSQL server.');

  Intro := TNewStaticText.Create(PasswordPage);
  Intro.Parent := PasswordPage.Surface;
  Intro.Left := 0;
  Intro.Top := 0;
  Intro.Width := PasswordPage.SurfaceWidth;
  Intro.WordWrap := True;
  Intro.AutoSize := True;
  Intro.Caption :=
    'This password is used once, to create a separate low-privilege account for the ' +
    'application. It is never stored and never written to a log.';

  Prompt := TNewStaticText.Create(PasswordPage);
  Prompt.Parent := PasswordPage.Surface;
  Prompt.Left := 0;
  Prompt.Top := Intro.Top + Intro.Height + ScaleY(16);
  Prompt.Width := PasswordPage.SurfaceWidth;
  Prompt.WordWrap := True;
  Prompt.AutoSize := True;
  Prompt.Caption :=
    'Enter the PostgreSQL administrator password created during PostgreSQL installation';

  PasswordEdit := TPasswordEdit.Create(PasswordPage);
  PasswordEdit.Parent := PasswordPage.Surface;
  PasswordEdit.Left := 0;
  PasswordEdit.Top := Prompt.Top + Prompt.Height + ScaleY(6);
  PasswordEdit.Width := PasswordPage.SurfaceWidth;

  Fixed := TNewStaticText.Create(PasswordPage);
  Fixed.Parent := PasswordPage.Surface;
  Fixed.Left := 0;
  Fixed.Top := PasswordEdit.Top + PasswordEdit.Height + ScaleY(16);
  Fixed.Width := PasswordPage.SurfaceWidth;
  Fixed.WordWrap := True;
  Fixed.AutoSize := True;
  Fixed.Caption := 'User name: postgres' + #13#10 +
                   'Server: localhost' + #13#10 +
                   'Port: {#PostgresPort}' + #13#10 +
                   'Database: postgres';
end;

function AskAgain(const Problem: String): Boolean;
{ The retry path. A modal dialog rather than a wizard page, because by the time PostgreSQL
  can be asked whether the password is right the wizard has moved past its pages -- see the
  note in CurStepChanged. It offers exactly what the page offered: one masked field. }
var
  Form: TSetupForm;
  Message, Prompt: TNewStaticText;
  Edit: TPasswordEdit;
  OkButton, CancelButton: TNewButton;
begin
  Result := False;
  { Fixed height: nothing on this form grows vertically. }
  Form := CreateCustomForm(ScaleX(440), ScaleY(210), False, True);
  try
    Form.Caption := '{#AppName} Setup';

    Message := TNewStaticText.Create(Form);
    Message.Parent := Form;
    Message.Left := ScaleX(16);
    Message.Top := ScaleY(16);
    Message.Width := ScaleX(408);
    Message.WordWrap := True;
    Message.AutoSize := True;
    Message.Caption := Problem;

    Prompt := TNewStaticText.Create(Form);
    Prompt.Parent := Form;
    Prompt.Left := ScaleX(16);
    Prompt.Top := Message.Top + Message.Height + ScaleY(16);
    Prompt.Width := ScaleX(408);
    Prompt.WordWrap := True;
    Prompt.AutoSize := True;
    Prompt.Caption :=
      'Enter the PostgreSQL administrator password created during PostgreSQL installation';

    Edit := TPasswordEdit.Create(Form);
    Edit.Parent := Form;
    Edit.Left := ScaleX(16);
    Edit.Top := Prompt.Top + Prompt.Height + ScaleY(6);
    Edit.Width := ScaleX(408);

    OkButton := TNewButton.Create(Form);
    OkButton.Parent := Form;
    OkButton.Width := ScaleX(90);
    OkButton.Height := ScaleY(26);
    OkButton.Left := Form.ClientWidth - ScaleX(212);
    OkButton.Top := Form.ClientHeight - ScaleY(42);
    OkButton.Caption := 'OK';
    OkButton.Default := True;
    OkButton.ModalResult := mrOk;

    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.Width := ScaleX(90);
    CancelButton.Height := ScaleY(26);
    CancelButton.Left := Form.ClientWidth - ScaleX(112);
    CancelButton.Top := OkButton.Top;
    CancelButton.Caption := 'Cancel';
    CancelButton.Cancel := True;
    CancelButton.ModalResult := mrCancel;

    Form.ActiveControl := Edit;
    { Without this it still centres, but on the screen rather than on the wizard. }
    Form.FlipAndCenterIfNeeded(True, WizardForm, False);
    if Form.ShowModal() <> mrOk then
      Exit;
    if Edit.Text = '' then
      Exit;
    Result := CaptureTypedPassword(Edit);
  finally
    Form.Free();
  end;
end;

function ProblemText(Code: Integer): String;
{ One place that turns an exit code into something a person can act on. No path here can
  include a password or a connection URL -- every value is a fixed string. }
begin
  case Code of
    EXIT_PORT_OCCUPIED:
      Result := 'Port {#PostgresPort} is being used by a program that is not PostgreSQL.';
    EXIT_POSTGRES_STOPPED:
      Result := 'PostgreSQL is installed on this computer but is not answering.';
    EXIT_POSTGRES_MISSING:
      Result := 'No PostgreSQL server is reachable on localhost:{#PostgresPort}.';
    EXIT_POSTGRES_WRONG_VER:
      Result := 'The PostgreSQL on this computer is not the version this application requires.';
    EXIT_ADMIN_AUTH_FAILED:
      Result := 'PostgreSQL did not accept the administrator password.';
    EXIT_NO_ADMIN_PASSWORD:
      Result := 'No PostgreSQL administrator password reached the database step.';
    EXIT_HANDOFF_FAILED:
      Result := 'The password could not be passed securely to the database step. This ' +
                'happens when setup is approved with a different administrator account: ' +
                'sign in as the account that will use the application and run setup again.';
    EXIT_MIGRATION_FAILED:
      Result := 'The database could not be brought up to date. No existing data was changed.';
    EXIT_VERIFICATION_FAIL:
      Result := 'The database schema did not match what this version expects. No existing ' +
                'data was changed.';
    EXIT_CREDENTIAL_FAILED:
      Result := 'The database credential could not be stored securely for your Windows account.';
    EXIT_PROVISION_FAILED:
      Result := 'The application database could not be prepared.';
  else
    Result := 'The database setup step did not complete.';
  end;
end;

function SupportLogPath: String;
begin
  Result := 'Support log: %LOCALAPPDATA%\{#DataDirName}\logs' + #13#10 +
            'Setup log: ' + ExpandConstant('{log}');
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
{ The PostgreSQL gate runs HERE, before the wizard is shown and long before any file is
  copied, so a computer that cannot host this application is never partially changed by it. }
begin
  Result := AskUserToCloseApp;
  if not Result then
    Exit;
  Result := RequirePostgres;
end;

function InitializeUninstall: Boolean;
begin
  Result := AskUserToCloseApp;
end;

procedure InitializeWizard;
begin
  CreatePasswordPage;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID <> PasswordPage.ID then
    Exit;

  if PasswordEdit.Text = '' then
  begin
    MsgBox('Enter the PostgreSQL administrator password to continue.', mbError, MB_OK);
    Result := False;
    Exit;
  end;

  { Protect it now, while the wizard can still refuse to go on. Whether PostgreSQL ACCEPTS
    the password is asked after installation, because the only thing on this computer that
    can ask is the application itself. }
  if not CaptureTypedPassword(PasswordEdit) then
  begin
    MsgBox('Windows could not protect the password on this computer, so setup cannot pass ' +
           'it on securely.' + #13#10#13#10 +
           'Sign in as the account that will use the application and run setup again.',
           mbError, MB_OK);
    Result := False;
  end;
end;

procedure RollBackInstallation;
{ Nothing half-installed is left behind: the uninstaller Inno has just registered removes
  exactly what was written, and nothing else. LocalAppData and PostgreSQL are untouched by
  it, as they are by every other path through this script. }
var
  ResultCode: Integer;
  Uninstaller: String;
begin
  Uninstaller := ExpandConstant('{uninstallexe}');
  if not FileExists(Uninstaller) then
  begin
    Log('Rollback: no uninstaller at ' + Uninstaller);
    Exit;
  end;
  Log('Rollback: removing the application again.');
  if Exec(Uninstaller, '/VERYSILENT /NORESTART /SUPPRESSMSGBOXES', '', SW_HIDE,
          ewWaitUntilTerminated, ResultCode) then
  begin
    RolledBack := (ResultCode = 0);
    Log('Rollback: uninstaller exit code ' + IntToStr(ResultCode));
  end
  else
    Log('Rollback: the uninstaller could not be started.');
end;

procedure FailSetup(const Stage, Detail: String);
begin
  ProvisioningSummary := Stage + ' did not complete.';
  DestroyHandoff;
  RollBackInstallation;
  MsgBox('{#AppName} was NOT installed.' + #13#10#13#10 +
         'Stage: ' + Stage + #13#10#13#10 + Detail + #13#10#13#10 + SupportLogPath,
         mbError, MB_OK);
end;

function Authenticate(var Code: Integer): Boolean;
begin
  if not WriteHandoffFile then
  begin
    Code := EXIT_HANDOFF_FAILED;
    Result := False;
    Exit;
  end;
  if not RunAppAsUser('--installer authenticate', Code) then
    Code := EXIT_UNEXPECTED;
  Log('Database: authenticate exit code ' + IntToStr(Code));
  Result := (Code = EXIT_OK);
end;

function AuthenticateWithRetries: Boolean;
var
  Code, Attempt: Integer;
begin
  Attempt := 1;
  Result := Authenticate(Code);
  while (not Result) and (Code = EXIT_ADMIN_AUTH_FAILED) and (Attempt < MaxPasswordAttempts) do
  begin
    Attempt := Attempt + 1;
    if not AskAgain('PostgreSQL did not accept that password. Try again.') then
      Break;
    Result := Authenticate(Code);
  end;

  if not Result then
  begin
    if Code = EXIT_ADMIN_AUTH_FAILED then
      FailSetup('PostgreSQL administrator sign-in',
                'The password for the "postgres" account was not accepted.' + #13#10#13#10 +
                'Run this setup again with the password you chose when you installed ' +
                'PostgreSQL.')
    else
      FailSetup('PostgreSQL administrator sign-in', ProblemText(Code));
  end;
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
    DestroyHandoff;
    Exit;
  end;

  if not AuthenticateWithRetries then
    Exit;

  { As the ORIGINAL user, so the DPAPI credential belongs to the account that will run the
    application. This step reads the protected handoff and destroys it whatever happens. }
  if not RunAppAsUser('--installer provision', ResultCode) then
    ResultCode := EXIT_UNEXPECTED;
  Log('Database: provision exit code ' + IntToStr(ResultCode));

  if ResultCode = EXIT_OK then
  begin
    ProvisioningSummary := 'The application database is ready.';
    DestroyHandoff;
  end
  else
    FailSetup('Database setup', ProblemText(ResultCode));
end;

procedure CurStepChanged(CurStep: TSetupStep);
{ Why the database work happens here, after the files are copied.

  Everything that decides whether this computer is SUITABLE ran in InitializeSetup, in pure
  Pascal, before anything was written -- that is the check that must not come late. What is
  left needs a PostgreSQL client, and the only one this setup has is inside the packaged
  application: setup itself is a 32-bit process and cannot load the 64-bit libpq next to the
  server. So the password is collected and protected on a wizard page, and verified the
  moment the application exists to verify it with. A password that turns out to be wrong is
  retried in a dialog, and giving up rolls the installation back rather than leaving a
  half-configured program behind. }
var
  ResultCode: Integer;
begin
  if CurStep <> ssPostInstall then
    Exit;

  if not RunAppAsUser('--installer begin-handoff', ResultCode) then
    ResultCode := EXIT_UNEXPECTED;
  Log('Handoff: begin-handoff exit code ' + IntToStr(ResultCode));
  if ResultCode <> EXIT_OK then
  begin
    FailSetup('Preparing the secure password handoff', ProblemText(EXIT_HANDOFF_FAILED));
    Exit;
  end;
  HandoffFile := ExpandConstant('{localappdata}\{#DataDirName}\work\setup-handoff.bin');

  ProvisionDatabase;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpFinished then
  begin
    if RolledBack then
      WizardForm.FinishedLabel.Caption :=
        'Setup did not finish. ' + ProvisioningSummary + ' Nothing was left installed.'
    else if ProvisioningSummary <> '' then
      WizardForm.FinishedLabel.Caption :=
        WizardForm.FinishedLabel.Caption + #13#10#13#10 + ProvisioningSummary;
  end;
end;

procedure DeinitializeSetup;
{ Last line of defence: whatever happened -- success, failure or Cancel -- no protected
  material is left in the user's work directory. }
begin
  if (HandoffFile <> '') and FileExists(HandoffFile) then
  begin
    Log('Handoff: removing leftover material at exit.');
    DeleteFile(HandoffFile);
  end;
  HandoffHex := '';
end;
