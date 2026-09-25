; Per-user Explorer integration. Keep other installations' entries on uninstall.
!macro RelayWriteVerb kind
  WriteRegStr HKCU "Software\Classes\${kind}\shell\WinToolboxRelay" "" "发送到文件中转站"
  WriteRegStr HKCU "Software\Classes\${kind}\shell\WinToolboxRelay" "MUIVerb" "发送到文件中转站"
  WriteRegStr HKCU "Software\Classes\${kind}\shell\WinToolboxRelay" "Icon" "$INSTDIR\${MAINBINARYNAME}.exe"
  WriteRegStr HKCU "Software\Classes\${kind}\shell\WinToolboxRelay" "Position" "Top"
  WriteRegStr HKCU "Software\Classes\${kind}\shell\WinToolboxRelay" "MultiSelectModel" "Document"
  WriteRegStr HKCU "Software\Classes\${kind}\shell\WinToolboxRelay\command" "" '$\"$INSTDIR\${MAINBINARYNAME}.exe$\" --relay-upload $\"%1$\"'
!macroend

!macro RelayRemoveVerb kind
  ReadRegStr $0 HKCU "Software\Classes\${kind}\shell\WinToolboxRelay\command" ""
  ${If} $0 == '$\"$INSTDIR\${MAINBINARYNAME}.exe$\" --relay-upload $\"%1$\"'
    DeleteRegKey HKCU "Software\Classes\${kind}\shell\WinToolboxRelay"
  ${EndIf}
!macroend

!macro NSIS_HOOK_POSTINSTALL
  !insertmacro RelayWriteVerb "*"
  !insertmacro RelayWriteVerb "Directory"
  CreateShortCut "$SENDTO\文件中转站.lnk" "$INSTDIR\${MAINBINARYNAME}.exe" "--relay-upload"
  System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0x1000, p 0, p 0)'
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  Push $0
  Push $1
  Push $2
  Push $3
  !insertmacro RelayRemoveVerb "*"
  !insertmacro RelayRemoveVerb "Directory"
  !insertmacro IsShortcutTarget "$SENDTO\文件中转站.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
  Pop $0
  ${If} $0 = 1
    Delete "$SENDTO\文件中转站.lnk"
  ${EndIf}
  System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0x1000, p 0, p 0)'
  Pop $3
  Pop $2
  Pop $1
  Pop $0
!macroend
