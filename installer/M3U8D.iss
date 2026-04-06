#define AppName "M3U8D"
#define AppVersion "0.2.1"
#define RepoRoot ".."
#define BuildRoot "..\dist\M3U8D"
#define MainExeName "M3U8D.exe"
#define ProtocolDirName "protocol_handler"
#define ProtocolExeName "protocol_handler.exe"

; 安装器当前按真实 one-dir 联调布局接线：
; 1. 先运行 build_pyinstaller.py / build_pyinstaller.bat
; 2. 预期主程序输出到 {#BuildRoot}\{#MainExeName}
; 3. 预期协议处理器输出到 {#BuildRoot}\{#ProtocolDirName}\{#ProtocolExeName}
; 4. 当前安装器直接消费 dist/M3U8D 目录，不再假定协议处理器与主程序共享同一 _internal

[Setup]
AppId={{D6A8DEB7-7D38-4A54-9F39-7C86D9950F5D}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=M3U8D
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MainExeName}
OutputDir={#SourcePath}\output
OutputBaseFilename=M3U8D-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesAssociations=yes
SetupIconFile={#RepoRoot}\resources\icons\mvs.ico

[Dirs]
Name: "{app}\bin"
Name: "{app}\logs"
Name: "{app}\cookies"
Name: "{app}\Temp"
Name: "{app}\scripts"
Name: "{app}\resources"
Name: "{app}\{#ProtocolDirName}"

[Files]
; 主程序 one-dir 产物
Source: "{#BuildRoot}\{#MainExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#BuildRoot}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

; 协议处理器 one-dir 产物
Source: "{#BuildRoot}\{#ProtocolDirName}\{#ProtocolExeName}"; DestDir: "{app}\{#ProtocolDirName}"; Flags: ignoreversion
Source: "{#BuildRoot}\{#ProtocolDirName}\_internal\*"; DestDir: "{app}\{#ProtocolDirName}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

; 打包后随 dist/M3U8D 一起分发的运行时文件
; 其中 core/utils 供安装后 scripts\download_dependencies.py 直接导入
Source: "{#BuildRoot}\resources\*"; DestDir: "{app}\resources"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#BuildRoot}\core\*"; DestDir: "{app}\core"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#BuildRoot}\utils\*"; DestDir: "{app}\utils"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#BuildRoot}\scripts\download_tools.bat"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "{#BuildRoot}\scripts\download_dependencies.py"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "{#BuildRoot}\scripts\register_protocol.bat"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "{#BuildRoot}\scripts\uninstall_protocol.bat"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "{#BuildRoot}\deps.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#BuildRoot}\config.json"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{group}\M3U8D"; Filename: "{app}\{#MainExeName}"
Name: "{group}\卸载 M3U8D"; Filename: "{uninstallexe}"

[Run]
; 依赖下载调用点：改为可见控制台执行，直接展示 CLI 逐项进度输出，并在脚本结束后自动关闭窗口
Filename: "{cmd}"; Parameters: "/C start ""M3U8D Dependency Download"" /wait cmd /C ""{app}\scripts\download_tools.bat"""; StatusMsg: "正在下载必须依赖..."; Flags: waituntilterminated; Check: ShouldRunRequiredDependencyDownload
Filename: "{cmd}"; Parameters: "/C start ""M3U8D Dependency Download"" /wait cmd /C ""{app}\scripts\download_tools.bat"" --include-recommended"; StatusMsg: "正在下载建议依赖..."; Flags: waituntilterminated; Check: ShouldRunRecommendedDependencyDownload

; 协议注册调用点：固定由 {app}\scripts\register_protocol.bat 回指 {app}\protocol_handler\protocol_handler.exe
Filename: "{cmd}"; Parameters: "/C ""{app}\scripts\register_protocol.bat"""; StatusMsg: "正在注册 m3u8dl:// 协议..."; Flags: runhidden waituntilterminated; Check: ShouldRegisterProtocolAfterInstall

; 常规启动入口
Filename: "{app}\{#MainExeName}"; Description: "安装完成后启动 M3U8D"; Flags: nowait postinstall skipifsilent unchecked

[Code]
const
  RequiredDependencyList = '  - yt-dlp'#13#10 +
    '  - N_m3u8DL-RE'#13#10 +
    '  - FFmpeg';
  RecommendedDependencyList = '  - aria2c'#13#10 +
    '  - Streamlink';

var
  WantRequiredDeps: Boolean;
  WantRecommendedDeps: Boolean;
  WantRegisterProtocol: Boolean;
  DependencyOptionsPage: TInputOptionWizardPage;

function ShouldPromptDependencyConfirmation: Boolean;
begin
  { TODO: 后续改为基于 deps.json 与已安装文件的缺失检测。 }
  Result := True;
end;

procedure InitializeWizard;
begin
  WantRequiredDeps := True;
  WantRecommendedDeps := False;
  WantRegisterProtocol := True;

  DependencyOptionsPage := CreateInputOptionPage(
    wpReady,
    '依赖下载与协议注册确认',
    '安装前确认下载内容与协议注册选项',
    '以下组件将在安装后下载到 bin 目录：'#13#10#13#10 +
    '【必须依赖】'#13#10 +
    RequiredDependencyList + #13#10#13#10 +
    '【建议依赖】'#13#10 +
    RecommendedDependencyList + #13#10#13#10 +
    '请在下方勾选需要执行的操作：',
    False,
    False
  );
  DependencyOptionsPage.Add('安装完成后立即下载上述必须依赖');
  DependencyOptionsPage.Add('同时下载上述建议依赖');
  DependencyOptionsPage.Add('安装完成后注册 m3u8dl:// 协议');
  DependencyOptionsPage.Values[0] := True;
  DependencyOptionsPage.Values[1] := False;
  DependencyOptionsPage.Values[2] := True;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;

  if CurPageID = DependencyOptionsPage.ID then
  begin
    WantRequiredDeps := DependencyOptionsPage.Values[0];
    if WantRequiredDeps then
      WantRecommendedDeps := DependencyOptionsPage.Values[1]
    else
      WantRecommendedDeps := False;
    WantRegisterProtocol := DependencyOptionsPage.Values[2];
    exit;
  end;
end;

function ShouldRunRequiredDependencyDownload: Boolean;
begin
  Result := WantRequiredDeps;
end;

function ShouldRunRecommendedDependencyDownload: Boolean;
begin
  Result := WantRecommendedDeps;
end;

function ShouldRegisterProtocolAfterInstall: Boolean;
begin
  Result := WantRegisterProtocol;
end;
