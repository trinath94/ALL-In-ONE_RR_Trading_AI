; RR Trading One installer hooks (Tauri NSIS, see tauri.bundle.conf.json).
;
; Before install (upgrade) and before uninstall: stop what the app started, using the uninstall-stop.ps1 that the
; installed copy carries. Nothing is deleted: database and cache volumes, settings and connector data are kept.
; After install: Docker Desktop is required for the engine. If it is missing, the user is ASKED whether to install it;
; it is never installed silently (a silent install of RR Trading One answers "No").

!macro RR_STOP_RUNNING_COPY
  IfFileExists "$INSTDIR\infrastructure\windows\uninstall-stop.ps1" 0 +3
    nsExec::ExecToLog 'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$INSTDIR\infrastructure\windows\uninstall-stop.ps1" -InstallDir "$INSTDIR"'
    Pop $0
!macroend

!macro NSIS_HOOK_PREINSTALL
  !insertmacro RR_STOP_RUNNING_COPY
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro RR_STOP_RUNNING_COPY
!macroend

!macro NSIS_HOOK_POSTINSTALL
  IfFileExists "$PROGRAMFILES64\Docker\Docker\resources\bin\docker.exe" rr_docker_done 0
  nsExec::ExecToStack 'cmd.exe /c where docker'
  Pop $0
  Pop $1
  StrCmp $0 "0" rr_docker_done 0
  MessageBox MB_YESNO|MB_ICONQUESTION "RR Trading One runs its trading engine in Docker Desktop, which is not installed on this PC.$\r$\n$\r$\nInstall Docker Desktop now? It is downloaded from Docker with the Windows Package Manager (winget) and installed under Docker's own licence terms. This can take several minutes, and Windows may ask you to restart.$\r$\n$\r$\nChoose No to install it yourself later from docker.com." /SD IDNO IDNO rr_docker_later
  DetailPrint "Installing Docker Desktop with winget..."
  nsExec::ExecToLog 'winget.exe install --exact --id Docker.DockerDesktop --source winget --accept-package-agreements --accept-source-agreements --silent'
  Pop $0
  StrCmp $0 "0" rr_docker_ok 0
  MessageBox MB_OK|MB_ICONEXCLAMATION "Docker Desktop could not be installed automatically (winget result: $0).$\r$\n$\r$\nThe Docker Desktop download page will open. Install it from there, then start RR Trading One." /SD IDOK
  ExecShell "open" "https://www.docker.com/products/docker-desktop/"
  Goto rr_docker_done
  rr_docker_ok:
  MessageBox MB_OK|MB_ICONINFORMATION "Docker Desktop is installed.$\r$\n$\r$\nRestart Windows if asked, open Docker Desktop once to finish its setup, then start RR Trading One and click Start All." /SD IDOK
  Goto rr_docker_done
  rr_docker_later:
  MessageBox MB_OK|MB_ICONINFORMATION "RR Trading One is installed. Install Docker Desktop from docker.com before clicking Start All." /SD IDOK
  rr_docker_done:
!macroend
