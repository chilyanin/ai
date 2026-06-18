@echo off
REM ===========================================================================
REM  Register a Windows Task Scheduler job that runs deactivate-find.bat every
REM  day at 20:00. It finds Asana "Удалить из ..." tasks due TODAY in
REM  --find-only mode (no deactivation) and logs to deactivate-find.log.
REM
REM    Install :  install-deactivate-task.bat
REM    Run now :  schtasks /Run /TN "AsanaDeactivateFindOnly"
REM    Inspect :  schtasks /Query /TN "AsanaDeactivateFindOnly" /V /FO LIST
REM    Remove  :  schtasks /Delete /TN "AsanaDeactivateFindOnly" /F
REM ===========================================================================
setlocal
cd /d "%~dp0"

set "RUNNER=%~dp0deactivate-find.bat"

if not exist "%RUNNER%" (
  echo [ERROR] %RUNNER% not found.
  pause
  exit /b 1
)

REM /RL LIMITED + onlogon-style desktop session: deactivate.py drives a visible
REM Chromium (Playwright) for captcha/2FA, so it needs your interactive session.
schtasks /Create /TN "AsanaDeactivateFindOnly" /SC DAILY /ST 20:00 /RL LIMITED /F ^
  /TR "\"%RUNNER%\""

if errorlevel 1 (
  echo [ERROR] Failed to register the scheduled task.
) else (
  echo.
  echo Task "AsanaDeactivateFindOnly" created - runs daily at 20:00.
  echo Test it now with:   schtasks /Run /TN "AsanaDeactivateFindOnly"
)
pause
