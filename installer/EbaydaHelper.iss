; 注意：本文件含中文路径，必须保存为 UTF-8 (带 BOM)，Inno Setup 6 才能正确解析。
#define MyAppName "Ebayda Helper"
#define MyAppVersion "0.3.6"
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
  // 升级安装时回显上次填写的地址；Inno 内置跨安装记忆，无需读文件
  ApiOriginPage.Values[0] := GetPreviousData('ApiOrigin', '');
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'ApiOrigin', Trim(ApiOriginPage.Values[0]));
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  ApiValue: String;
  HostPart: String;
begin
  Result := True;
  if CurPageID = ApiOriginPage.ID then
  begin
    ApiValue := Trim(ApiOriginPage.Values[0]);
    if ApiValue <> '' then
    begin
      if (Pos('http://', ApiValue) = 1) or (Pos('https://', ApiValue) = 1) then
      begin
        HostPart := Copy(ApiValue, Pos('://', ApiValue) + 3, Length(ApiValue));
        if (Pos('/', HostPart) > 0) or (Pos('?', HostPart) > 0) or (Pos(' ', ApiValue) > 0) then
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
  OriginLines: TArrayOfString;
begin
  // 静默安装不显示向导页，跳过写入以保留现有配置文件
  if (CurStep = ssPostInstall) and (not WizardSilent) then
  begin
    if Trim(ApiOriginPage.Values[0]) = '' then
      DeleteFile(ExpandConstant('{app}\api-origin.txt'))
    else
    begin
      SetArrayLength(OriginLines, 1);
      OriginLines[0] := Trim(ApiOriginPage.Values[0]);
      // UTF-8（带 BOM）写入；程序侧以 utf-8-sig 读取
      SaveStringsToUTF8File(ExpandConstant('{app}\api-origin.txt'), OriginLines, False);
    end;
  end;
end;
