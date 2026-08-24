; Instalador de Windows con el asistente clásico "Siguiente, Siguiente,
; Instalar" (petición explícita del usuario), generado con Inno Setup a
; partir del build "onedir" de PyInstaller (dist/p2p-total).
; Compilar con: ISCC.exe packaging\windows\installer.iss
; (ejecutar desde la raíz del repositorio, para que las rutas relativas
; ..\..\dist y ..\..\packaging apunten bien).

#define MyAppName "P2P Total"
#define MyAppVersion "1.0"
#define MyAppPublisher "AnabasaSoft"
#define MyAppURL "https://github.com/AnabasaSoft/P2P-Total"
#define MyAppExeName "p2p-total.exe"

[Setup]
AppId={{7A5A8350-F979-4D36-91B9-D6135688A4E9}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\P2P Total
DefaultGroupName=P2P Total
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=..\..\dist-installer
OutputBaseFilename=P2P-Total-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=p2p-total.ico
ArchitecturesInstallIn64BitMode=x64compatible
; Necesario para la auto-actualización (core/self_updater.py, punto 34.1
; del backlog): al lanzarse en modo silencioso con /CLOSEAPPLICATIONS
; /RESTARTAPPLICATIONS desde la propia app en marcha, cierra P2P Total
; automáticamente (detecta el proceso que tiene abiertos los ficheros a
; sustituir, vía Windows Restart Manager) y lo vuelve a abrir solo al
; terminar, sin que el usuario tenga que hacer nada.
CloseApplications=yes
RestartApplications=yes

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el escritorio"; GroupDescription: "Accesos directos adicionales:"

[Files]
Source: "..\..\dist\p2p-total\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\P2P Total"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Desinstalar P2P Total"; Filename: "{uninstallexe}"
Name: "{autodesktop}\P2P Total"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Ejecutar P2P Total"; Flags: nowait postinstall skipifsilent
