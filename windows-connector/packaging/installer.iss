; RR MT5 Connector — Windows installer (Inno Setup 6). Produces RR_MT5_Connector_Setup.exe.
; Build:  iscc /DAppVersion=1.0.0 packaging\installer.iss     (after PyInstaller; see packaging\build.ps1)
;
; Silent deployment:
;   RR_MT5_Connector_Setup.exe /VERYSILENT /ENGINEURL=https://engine.example.com /TOKEN=rrmt5_xxx
;   (/TOKEN is single-use and expires; registration stores the resulting credentials in the
;    Windows Credential Manager of the signed-in user, never in a file.)

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "RR MT5 Connector"
#define AppExe "RR_MT5_Connector.exe"

[Setup]
AppId={{6B0E5C44-1F3A-4E5B-9C7D-2A8E4F1B9D31}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=RR Trading
DefaultDirName={autopf}\RR MT5 Connector
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=RR_MT5_Connector_Setup
SetupIconFile=rr_connector.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
PrivilegesRequired=admin
CloseApplications=yes
RestartApplications=no
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "autostart"; Description: "Start RR MT5 Connector automatically when I sign in to Windows (recommended)"; GroupDescription: "Startup:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\RR_MT5_Connector\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; per-user settings must be written as the signed-in user, not as the elevated installer account
Filename: "{app}\{#AppExe}"; Parameters: "--autostart on"; Flags: runasoriginaluser runhidden waituntilterminated; Tasks: autostart
Filename: "{app}\{#AppExe}"; Parameters: "--autostart off"; Flags: runasoriginaluser runhidden waituntilterminated; Tasks: not autostart
Filename: "{app}\{#AppExe}"; Parameters: "--register ""{code:EngineUrl}"" ""{code:EngineToken}"""; Flags: runasoriginaluser waituntilterminated; Check: HasEngineDetails; StatusMsg: "Linking this PC to the RR AI Engine..."
Filename: "{app}\{#AppExe}"; Description: "Start RR MT5 Connector now"; Flags: runasoriginaluser nowait postinstall skipifsilent
Filename: "{app}\{#AppExe}"; Parameters: "--minimized"; Flags: runasoriginaluser nowait; Check: WizardSilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#AppExe}"; Flags: runhidden; RunOnceId: "StopConnector"

[Code]
var
  EnginePage: TInputQueryWizardPage;
  MT5Found: Boolean;

function ParamValue(const Name: String): String;
var
  I: Integer;
  P: String;
begin
  Result := '';
  for I := 1 to ParamCount do
  begin
    P := ParamStr(I);
    if CompareText(Copy(P, 1, Length(Name) + 2), '/' + Name + '=') = 0 then
      Result := Copy(P, Length(Name) + 3, MaxInt);
  end;
end;

function UninstallHasMT5(RootKey: Integer; const Path: String): Boolean;
var
  Names: TArrayOfString;
  I: Integer;
  DisplayName: String;
begin
  Result := False;
  if RegGetSubkeyNames(RootKey, Path, Names) then
    for I := 0 to GetArrayLength(Names) - 1 do
      if RegQueryStringValue(RootKey, Path + '\' + Names[I], 'DisplayName', DisplayName) then
        if (Pos('METATRADER 5', Uppercase(DisplayName)) > 0) or (Pos('MT5', Uppercase(DisplayName)) > 0) then
        begin
          Result := True;
          Exit;
        end;
end;

function DetectMT5: Boolean;
var
  FindRec: TFindRec;
begin
  Result := UninstallHasMT5(HKLM64, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall')
    or UninstallHasMT5(HKLM32, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall')
    or UninstallHasMT5(HKCU, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall')
    or DirExists(ExpandConstant('{userappdata}\MetaQuotes\Terminal'));
  if not Result then
    if FindFirst(ExpandConstant('{commonpf64}\*'), FindRec) then
    try
      repeat
        if FileExists(ExpandConstant('{commonpf64}\') + FindRec.Name + '\terminal64.exe') then
        begin
          Result := True;
          Exit;
        end;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
end;

procedure InitializeWizard;
begin
  MT5Found := DetectMT5;
  EnginePage := CreateInputQueryPage(wpSelectTasks, 'Link to RR AI Engine',
    'Optional — you can also do this when the connector starts.',
    'In the RR AI Engine open MT5 Terminal > RR MT5 Connector agents > Create registration token. ' +
    'Enter the engine''s HTTPS address and the token below. Credentials are stored in the Windows Credential Manager.');
  EnginePage.Add('RR AI Engine address (https://...):', False);
  EnginePage.Add('Registration token:', True);
  EnginePage.Values[0] := ParamValue('ENGINEURL');
  EnginePage.Values[1] := ParamValue('TOKEN');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = EnginePage.ID) and (Trim(EnginePage.Values[0]) <> '') then
    if (Pos('https://', Lowercase(Trim(EnginePage.Values[0]))) <> 1) then
    begin
      MsgBox('The RR AI Engine address must start with https://', mbError, MB_OK);
      Result := False;
    end
    else if Length(Trim(EnginePage.Values[1])) < 16 then
    begin
      MsgBox('Enter the registration token shown in the RR AI Engine (or clear the address to skip).', mbError, MB_OK);
      Result := False;
    end;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo, MemoComponentsInfo,
  MemoGroupInfo, MemoTasksInfo: String): String;
begin
  Result := MemoDirInfo + NewLine + NewLine + MemoTasksInfo;
  if Trim(EnginePage.Values[0]) <> '' then
    Result := Result + NewLine + NewLine + 'RR AI Engine:' + NewLine + Space + Trim(EnginePage.Values[0]);
  if not MT5Found then
    Result := Result + NewLine + NewLine + 'MetaTrader 5 was NOT found on this PC.' + NewLine + Space +
      'Install it from your broker or https://www.metatrader5.com, log in to your trading account,' + NewLine + Space +
      'then click CONNECT in RR MT5 Connector.';
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpReady) and (not MT5Found) and (not WizardSilent) then
    MsgBox('MetaTrader 5 was not found on this PC.' + #13#10#13#10 +
      'RR MT5 Connector needs the MetaTrader 5 terminal (64-bit). Install it from your broker or from ' +
      'https://www.metatrader5.com, log in to your trading account, then start RR MT5 Connector and click CONNECT.' + #13#10#13#10 +
      'Installation of RR MT5 Connector can continue now.', mbInformation, MB_OK);
end;

function HasEngineDetails: Boolean;
begin
  Result := (Trim(EnginePage.Values[0]) <> '') and (Trim(EnginePage.Values[1]) <> '');
end;

function EngineUrl(Param: String): String;
begin
  Result := Trim(EnginePage.Values[0]);
end;

function EngineToken(Param: String): String;
begin
  Result := Trim(EnginePage.Values[1]);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  RC: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    if UninstallSilent or (MsgBox('Also remove this PC''s RR AI Engine link, saved settings and logs?' + #13#10 +
      '(Choose No to keep them for a later reinstall.)', mbConfirmation, MB_YESNO) = IDYES) then
      Exec(ExpandConstant('{app}\{#AppExe}'), '--forget', '', SW_HIDE, ewWaitUntilTerminated, RC)
    else
      Exec(ExpandConstant('{app}\{#AppExe}'), '--autostart off', '', SW_HIDE, ewWaitUntilTerminated, RC);
  end;
end;
