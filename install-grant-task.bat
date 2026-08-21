@echo off
REM ===========================================================================
REM  Register a Windows Task Scheduler job that runs grant-run.bat every day at
REM  20:00. It processes Asana "Заявка на лицензию/доступ ..." tasks due TODAY
REM  and performs the grants (headless), logging to grant-run.log.
REM
REM    Install :  install-grant-task.bat
REM    Run now :  schtasks /Run /TN "AsanaGrant"
REM    Inspect :  schtasks /Query /TN "AsanaGrant" /V /FO LIST
REM    Remove  :  schtasks /Delete /TN "AsanaGrant" /F
REM ===========================================================================
setlocal
cd /d "%~dp0"

set "RUNNER=%~dp0grant-run.bat"

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
schtasks /Create /TN "AsanaGrant" /SC DAILY /ST 20:00 ^
  /RU "%USERDOMAIN%\%USERNAME%" /RP * /RL HIGHEST /F ^
  /TR "\"%RUNNER%\""

if errorlevel 1 (
  echo [ERROR] Failed to register the scheduled task.
) else (
  echo.
  echo Task "AsanaGrant" created - runs daily at 20:00.
  echo Test it now with:   schtasks /Run /TN "AsanaGrant"
)
pause
