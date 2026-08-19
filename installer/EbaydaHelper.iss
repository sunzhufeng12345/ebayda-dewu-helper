; 注意：本文件含中文路径，必须保存为 UTF-8 (带 BOM)，Inno Setup 6 才能正确解析。
#define MyAppName "Ebayda Helper"
#define MyAppVersion "0.3.4"
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
