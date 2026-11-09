; ─────────────────────────────────────────────────────────────
;  WeChat 自动回复 —— 安装包脚本（Inno Setup 6）
;
;  打包内容：便携式开发版（全功能）
;  安装位置：%LOCALAPPDATA%\Programs\WeChatAutoReply
;            用户级安装，不需要管理员权限
;
;  ASCII 说明：这个文件本身可以是 UTF-8（Inno Setup 6 支持），
;  但为保险起见，中文只出现在需要展示给用户的地方。
; ─────────────────────────────────────────────────────────────

#define MyAppName "WeChat 自动回复"
#define MyAppVersion "1.12"
#define MyAppPublisher "DeepSeek 大肥鱼"
#define MyAppExeName "WeChatAutoReply"
#define MyAppId "{{8F3A7C21-4B9E-4D62-A5C8-1E7F9D2B6A43}"

; 源目录（便携式开发版）
#define SrcDir "F:\zhuomian\WeChatAutoReply-Dev"
; 输出目录
#define OutDir "F:\zhuomian"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppComments=微信群里像个人一样聊天的机器人
VersionInfoVersion=1.2.0.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} 安装程序

; 用户级安装 —— 不弹 UAC
DefaultDirName={localappdata}\Programs\WeChatAutoReply
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; 图标
SetupIconFile=app.ico
UninstallDisplayIcon={app}\app.ico
UninstallDisplayName={#MyAppName}

; 压缩
Compression=lzma2/max
SolidCompression=yes

; 界面
WizardStyle=modern
DisableProgramGroupPage=yes
ShowLanguageDialog=no

; 输出
OutputDir={#OutDir}
OutputBaseFilename=WeChatAutoReply-Setup-{#MyAppVersion}
#ifdef EnableSign
SignTool=signtool
#endif

[Languages]
Name: "chs"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: checkedonce
Name: "startupicon"; Description: "开机自动启动（不推荐，会一直占后台）"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
; 整个便携包原样装进去（含 runtime、gui.py、brain.py、prompts、stickers…）
;
; 排除项说明：
;   __pycache__ / *.pyc  Python 字节码，装到别的机器上会被重建，不带
;   .bak-* / *.bak       备份文件
;   runtime\include      C 头文件，运行时不需要
;   runtime\Lib\test     标准库测试套件
;   runtime\Lib\idlelib  IDLE 编辑器
;   runtime\Lib\lib2to3  旧的 2to3 转换器
;   runtime\Lib\ensurepip / venv  建虚拟环境用，不需要
;   runtime\tcl\tix*     Tk 扩展主题，界面没用到
;
; 注意：Inno Setup 的字符串不支持换行，Excludes 必须写成一行。
#define Excl "__pycache__,*.pyc,*.bak-*,*.bak,*.tmp,daemon.log,supervisor.log,heartbeat.txt,decisions.jsonl,state.json,companion-memory\archive.jsonl,companion-memory\records.json,runtime\include,runtime\Lib\test,runtime\Lib\idlelib,runtime\Lib\lib2to3,runtime\Lib\ensurepip,runtime\Lib\venv,runtime\Lib\turtledemo,runtime\tcl\tix8.4.3,runtime\Lib\site-packages\pip,runtime\Lib\site-packages\setuptools,runtime\Lib\site-packages\pkg_resources,runtime\Lib\site-packages\numpy\tests,runtime\Lib\site-packages\numpy\distutils"

Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: {#Excl}

; **单独把人格数据放进去。**
; 上面的 Excludes 里写的是 state.json（通配任意层级），会把
; companion-memory\state.json 一起排掉 —— 那份是人格底色，必须带上。
; 这一条不受上面 Excludes 影响（Excludes 只作用于它自己那条 Source）。
Source: "{#SrcDir}\companion-memory\state.json"; DestDir: "{app}\companion-memory"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
; 开始菜单
Name: "{group}\{#MyAppName}"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\gui.py"""; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"; Comment: "打开控制面板"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
; 桌面
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\gui.py"""; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"; Tasks: desktopicon
; 开机启动
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\gui.py"""; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"; Tasks: startupicon

[Run]
; 安装完成后可选立即启动
Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\gui.py"""; WorkingDir: "{app}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 卸载前先停掉后台
Filename: "{app}\stop-daemon.cmd"; RunOnceId: "StopDaemon"; Flags: runhidden waituntilterminated

[UninstallDelete]
; 清理运行时产生的日志和状态（保留用户配置）
Type: files; Name: "{app}\daemon.log"
Type: files; Name: "{app}\supervisor.log"
Type: files; Name: "{app}\heartbeat.txt"
Type: files; Name: "{app}\stdout.txt"
Type: files; Name: "{app}\stderr.txt"
Type: files; Name: "{app}\stack.txt"

[Code]
// ── 安装前：暂时不做拦截 ──
function InitializeSetup(): Boolean;
begin
  Result := True;
end;

// ── 卸载：清掉运行时生成的字节码缓存 ──
// Inno 只删它自己装进清单的文件。Python 导入模块时会在旁边生成
// __pycache__/*.pyc，那些没在清单里，不主动清就会留下几百个残渣
// （实测 349 个 .pyc 留在了 AppData 里）。
//
// 时机必须是 **usUninstall**（卸载刚开始、文件还在），不能是 usPostUninstall
// —— 那时 {app} 已经被 Inno 处理过，DelTree 扑空。
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    DelTree(ExpandConstant('{app}\runtime'), True, True, True);
    DelTree(ExpandConstant('{app}\__pycache__'), True, True, True);
  end;
end;
