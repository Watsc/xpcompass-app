; XPCompass's Windows installer, for Inno Setup 6. tools/pack_companion.py compiles it with ISCC,
; passing the PyInstaller one-folder build (AppSource), the build (AppBuild), a number that only
; grows (AppNumber) and the icon (AppIcon); with a code-signing certificate set up it also defines
; Sign and the sign tool "xpc".
;
; Per user: %LOCALAPPDATA%\Programs\XPCompass, no administrator rights and no
; UAC prompt, a Start menu entry and an entry in Settings > Apps. Settings stay in
; %LOCALAPPDATA%\ForeverCoach and the addons in the game folder; uninstalling leaves both.
;
; The app updates itself by running a newer copy of this setup with /VERYSILENT /SUPPRESSMSGBOXES
; /NORESTART /LOG=<file> /RELAUNCH=<its own exe>, then quitting (selfupdate.py). The setup waits for
; it to close, installs and starts the new app; if the install fails it starts the old one again.

#ifndef AppSource
  #error AppSource must name the PyInstaller one-folder build; tools/pack_companion.py passes it
#endif
#ifndef AppBuild
  #define AppBuild "dev"
#endif
#ifndef AppNumber
  #define AppNumber "0"
#endif
#ifndef AppIcon
  #define AppIcon "..\assets\branding\XPCompass.ico"
#endif
#define AppName "XPCompass"
#define AppExe "XPCompass.exe"
; The app holds this mutex while it runs (system.single_instance).
#define AppMutex "ForeverCoachCompanion"

[Setup]
; Never change AppId: it is how a new setup finds the installed app, and how Windows lists it.
AppId={{DD689CA4-8609-497F-8563-382DAB3095D2}
AppName={#AppName}
AppVersion={#AppBuild}
AppVerName={#AppName} {#AppBuild}
VersionInfoVersion=1.0.{#AppNumber}.0
AppPublisher={#AppName}
AppPublisherURL=https://xpcompass.com
AppSupportURL=https://xpcompass.com/download
AppUpdatesURL=https://xpcompass.com/download
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\{#AppName}
DisableDirPage=yes
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename={#AppName}-Setup
SetupIconFile={#AppIcon}
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
SetupMutex=XPCompassSetup
; The app has normally quit before the files are replaced (InitializeSetup waits for it). If it
; hasn't, a silent update closes it rather than fail on files in use; the setup starts it itself.
CloseApplications=force
RestartApplications=no
#ifdef Sign
SignTool=xpc
SignedUninstaller=yes
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"

[Run]
Filename: "{app}\{#AppExe}"; Parameters: "--after-update"; Flags: nowait; Check: WizardSilent
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Installers the app downloaded. The settings beside this folder stay.
Type: filesandordirs; Name: "{localappdata}\ForeverCoach\updates"

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';
  RunName = 'ForeverCoachCompanion';

var
  Installed: Boolean;

{ Wait for the running app to quit. A silent update waits on its own, since the app quits right
  after starting it; after a minute it carries on, and CloseApplications closes the app if it still
  holds files. A player running the setup or the uninstaller is asked to quit the app. }
function AppClosed(Silent: Boolean): Boolean;
var
  Waited: Integer;
begin
  Result := True;
  Waited := 0;
  while CheckForMutexes('{#AppMutex}') do begin
    if Silent then begin
      if Waited >= 60000 then
        Exit;
      Sleep(500);
      Waited := Waited + 500;
    end else if MsgBox('XPCompass is running. Quit it from its tray icon (right-click it, then Quit), ' +
                       'then click OK.', mbInformation, MB_OKCANCEL) = IDCANCEL then begin
      Result := False;
      Exit;
    end;
  end;
end;

function InitializeSetup(): Boolean;
begin
  Result := AppClosed(WizardSilent);
end;

function Locked(const FileName: String): Boolean;
var
  Stream: TFileStream;
begin
  Result := False;
  if not FileExists(FileName) then
    Exit;
  try
    Stream := TFileStream.Create(FileName, fmOpenReadWrite or fmShareExclusive);
    Stream.Free;
  except
    Result := True;
  end;
end;

{ The one-file app from before the installer is two processes, and the outer one keeps its .exe
  locked for a moment after the app has let go of its mutex. Players who kept that .exe here have it
  replaced by this install, so wait up to half a minute for it to come free. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Waited: Integer;
begin
  Result := '';
  Waited := 0;
  while Locked(ExpandConstant('{app}\{#AppExe}')) and (Waited < 30000) do begin
    Sleep(500);
    Waited := Waited + 500;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    Installed := True;
end;

{ A silent update that failed starts the app it came from again, so the player isn't left without
  it. That app leaves the build alone for an hour (selfupdate.RETRY_AFTER). }
procedure DeinitializeSetup();
var
  Previous: String;
  Code: Integer;
begin
  Previous := ExpandConstant('{param:RELAUNCH|}');
  if WizardSilent and not Installed and (Previous <> '') and FileExists(Previous) then
    Exec(Previous, '--after-update', '', SW_SHOWNORMAL, ewNoWait, Code);
end;

function InitializeUninstall(): Boolean;
begin
  Result := AppClosed(UninstallSilent);
end;

{ Start with Windows is the app's own setting, so the setup never adds it; it goes with the app. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Command: String;
begin
  if (CurUninstallStep = usPostUninstall) and RegQueryStringValue(HKCU, RunKey, RunName, Command)
     and (Pos(Lowercase(ExpandConstant('{app}')), Lowercase(Command)) > 0) then
    RegDeleteValue(HKCU, RunKey, RunName);
end;
