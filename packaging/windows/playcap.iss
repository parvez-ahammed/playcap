; Inno Setup script for playcap: a per-user installer around the portable build.
; NOT PUBLISHED YET. See packaging/README.md for when this ships (after code
; signing is in place).
;
; Build (Inno Setup 6.3+), after build_portable.py has staged the app:
;   python packaging\windows\build_portable.py
;   iscc /DAppVersion=0.1.0 packaging\windows\playcap.iss
; Output: dist\playcap-<version>-setup.exe
;
; Design:
; - Per-user, no admin prompt: installs to %LOCALAPPDATA%\Programs\playcap.
; - Silent install works with Inno's standard switches:
;     playcap-0.1.0-setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
;   (winget passes these; see packaging/winget/).
; - User data is NOT in the install folder. The installer ships
;   data-location.txt = %USERPROFILE%\playcap, which playcap.bat reads, so
;   config.json, the queue and recordings live in C:\Users\<you>\playcap.
;   That folder is visible and outside OneDrive-synced Documents (recordings
;   are large). Uninstall removes the program only and never touches it;
;   upgrades replace app\ only.

#ifndef AppVersion
  #error Pass the version: iscc /DAppVersion=0.1.0 playcap.iss
#endif
#define Stage "..\..\build\portable\playcap-" + AppVersion + "-windows-x64"

[Setup]
AppId={{347DA824-415A-42D7-852F-F4974A51E050}
AppName=playcap
AppVersion={#AppVersion}
AppVerName=playcap {#AppVersion}
AppPublisher=playcap contributors
AppPublisherURL=https://github.com/parvez-ahammed/playcap
AppSupportURL=https://github.com/parvez-ahammed/playcap/issues
AppUpdatesURL=https://github.com/parvez-ahammed/playcap/releases
LicenseFile={#Stage}\LICENSE.txt
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\playcap
DisableDirPage=auto
DefaultGroupName=playcap
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=playcap-{#AppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=playcap
; The UI's python.exe holds files open; let Restart Manager close it on upgrade.
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Upgrades: start app\ clean so modules removed upstream don't linger.
Type: filesandordirs; Name: "{app}\app"

[Files]
Source: "{#Stage}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "installer-data-location.txt"; DestDir: "{app}"; DestName: "data-location.txt"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\playcap"; Filename: "{app}\playcap.bat"; WorkingDir: "{app}"; Comment: "Open the playcap UI"
Name: "{autoprograms}\playcap data folder"; Filename: "{%USERPROFILE}\playcap"; Comment: "Settings, queue and recordings"
Name: "{autodesktop}\playcap"; Filename: "{app}\playcap.bat"; WorkingDir: "{app}"; Tasks: desktopicon

[Dirs]
; Create the data folder up front so the "data folder" shortcut works before
; first launch. uninsneveruninstall: uninstall must never remove user data.
Name: "{%USERPROFILE}\playcap"; Flags: uninsneveruninstall

[Run]
Filename: "{app}\playcap.bat"; Description: "Open playcap now"; Flags: postinstall nowait skipifsilent shellexec

[UninstallDelete]
; Bytecode caches Python wrote at run time. Only under {app}\app, never data.
Type: filesandordirs; Name: "{app}\app"
