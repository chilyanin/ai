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

REM Run whether logged on or not: /RU + /RP store the account credentials so the
REM task fires in Session 0 with no desktop. The runner uses --headless to suit
REM that. The machine must be powered on (not asleep) at 20:00, and every
REM service session in .browser_profiles\ must still be valid - an expired
REM login/2FA/captcha has no desktop to interact with and will hang.
REM /RP * prompts for the password (stored encrypted); /RL HIGHEST avoids UAC
REM truncation. Replace %USERDOMAIN%\%USERNAME% if a different account is wanted.
schtasks /Create /TN "AsanaDeactivateFindOnly" /SC DAILY /ST 20:00 ^
  /RU "%USERDOMAIN%\%USERNAME%" /RP * /RL HIGHEST /F ^
  /TR "\"%RUNNER%\""

if errorlevel 1 (
  echo [ERROR] Failed to register the scheduled task.
) else (
  echo.
  echo Task "AsanaDeactivateFindOnly" created - runs daily at 20:00.
  echo Test it now with:   schtasks /Run /TN "AsanaDeactivateFindOnly"
)
pause
