; ─────────────────────────────────────────────────────────────
;  WeChat 自动回复 便携版 —— 安装包脚本（Inno Setup 6）
;
;  和 installer.iss（开发版）的区别：
;    · SrcDir 指向 F:\zhuomian\WeChatAutoReply（阉割版目录）
;    · AppId 不同 —— 两个版本可以**并存安装**，不会互相覆盖
;    · 输出文件名不同：WeChatAutoReply-Lite-Setup-x.y.exe
;
;  阉割版能力（代码级硬禁用，见 brain.py 的 is_portable()）：
;    有：群聊回复、私聊回复、收红包/转账、拍一拍、表情包、看图、guardian
;    无：朋友圈、好友申请、转述
; ─────────────────────────────────────────────────────────────

#define MyAppName "WeChat 自动回复（便携版）"
#define MyAppVersion "3.1"
#define MyAppPublisher "DeepSeek 大肥鱼"
#define MyAppExeName "WeChatAutoReply"
; **AppId 必须和开发版不同**，否则装了一个会覆盖另一个
#define MyAppId "{{B7C4E9D2-5F81-4A3E-9D26-8C41A7B5E903}"

#define SrcDir "F:\zhuomian\WeChatAutoReply"
#define OutDir "F:\zhuomian"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppComments=微信群聊自动回复（便携版：只回群聊和私聊）
VersionInfoVersion=2.9.0.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} 安装程序

; 装到单独的目录，和开发版并存
DefaultDirName={localappdata}\Programs\WeChatAutoReply-Lite
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

SetupIconFile=app.ico
UninstallDisplayIcon={app}\app.ico
UninstallDisplayName={#MyAppName}

Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
WizardStyle=modern
DisableProgramGroupPage=yes
OutputDir={#OutDir}
OutputBaseFilename=WeChatAutoReply-Lite-Setup-{#MyAppVersion}
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible

[Languages]
Name: "chs"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; \
    Excludes: "daemon.log,daemon.log.*,supervisor.log,guardian.log,ALERT.txt,heartbeat.txt,decisions.jsonl,vision-cache.json,state.json,emotion.json,companion-memory\*,member-profiles.json,style.json,topics.json,.git\*,.gitignore,__pycache__\*,*.pyc,_*.py,publish_api.py,publish.py,sync_publish.py,installer.iss,installer-lite.iss,*.bak*,manifest.json,README.md,HANDOVER.md,CONTINUITY.md,这段对话.md,letter_from_mama*.md"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\gui.py"""; WorkingDir: "{app}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\gui.py"""; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\gui.py"""; WorkingDir: "{app}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\companion-memory"
