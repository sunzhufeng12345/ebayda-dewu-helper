; 注意：本文件含中文路径，必须保存为 UTF-8 (带 BOM)，Inno Setup 6 才能正确解析。
#define MyAppName "Ebayda Helper"
#define MyAppVersion "0.3.5"
#define MyAppExeName "EbaydaHelper.exe"

[Setup]
AppId={{EAA11134-2364-4B14-A6C8-6F85FC868A61}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\EbaydaHelper
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
OutputDir=..\dist-installer
OutputBaseFilename=EbaydaHelperSetup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
; 配置目录随安装包落到安装目录，程序优先读取这里（main.py 中"配置文件"优先于内嵌 _ebayda_config）。
; onlyifdoesntexist + uninsneveruninstall：升级不覆盖、卸载不删除运营改过的 Excel。
Source: "..\配置文件\*"; DestDir: "{app}\配置文件"; Excludes: ".DS_Store,*.pyc"; Flags: recursesubdirs createallsubdirs onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

[Registry]
Root: HKCU; Subkey: "Software\Classes\ebayda"; ValueType: string; ValueName: ""; ValueData: "URL:Ebayda Helper Protocol"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\ebayda"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\ebayda\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#MyAppExeName},0"
Root: HKCU; Subkey: "Software\Classes\ebayda\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "EbaydaHelper"; ValueData: """{app}\{#MyAppExeName}"" --resident"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--resident"; Flags: nowait skipifsilent

[UninstallDelete]
; 卸载时清理安装向导生成的自建服务器地址配置
Type: files; Name: "{app}\api-origin.txt"

[Code]
var
  ApiOriginPage: TInputQueryWizardPage;

procedure InitializeWizard;
begin
  ApiOriginPage := CreateInputQueryPage(wpSelectDir,
    'API 服务器地址',
    '指定助手连接的任务下发网站',
    '自建服务器请填写完整地址（例如 http://101.34.90.101:10112），' + #13#10 +
    '留空表示使用官方地址 www.ebayda.com。');
  ApiOriginPage.Add('API 地址：', False);
end;

procedure CurPageChanged(CurPageID: Integer);
var
  Existing: String;
begin
  // 升级安装时回显现有配置，便于修改或清空（清空 = 回到官方地址）
  if (CurPageID = ApiOriginPage.ID) and (ApiOriginPage.Values[0] = '') then
  begin
    if LoadStringFromFile(ExpandConstant('{app}\api-origin.txt'), Existing) then
      ApiOriginPage.Values[0] := Trim(Existing);
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Value: String;
  Rest: String;
begin
  Result := True;
  if CurPageID = ApiOriginPage.ID then
  begin
    Value := Trim(ApiOriginPage.Values[0]);
    if Value <> '' then
    begin
      if (Pos('http://', Value) = 1) or (Pos('https://', Value) = 1) then
      begin
        Rest := Copy(Value, Pos('://', Value) + 3, MaxInt);
        if (Pos('/', Rest) > 0) or (Pos('?', Rest) > 0) or (Pos(' ', Value) > 0) then
        begin
          MsgBox('API 地址格式错误：只能是 http(s)://主机[:端口]，不能包含路径。', mbError, MB_OK);
          Result := False;
        end;
      end
      else
      begin
        MsgBox('API 地址必须以 http:// 或 https:// 开头。', mbError, MB_OK);
        Result := False;
      end;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Origin: String;
begin
  if CurStep = ssPostInstall then
  begin
    // 静默安装（/SILENT）不显示向导页，此时保留现有配置不动
    if WizardSilent() then
      exit;
    Origin := Trim(ApiOriginPage.Values[0]);
    if Origin <> '' then
      SaveStringToFile(ExpandConstant('{app}\api-origin.txt'), Origin, False)
    else
      DeleteFile(ExpandConstant('{app}\api-origin.txt'));
  end;
end;
