@echo off
REM ===========================================================================
REM  Register a Windows Task Scheduler job that runs uicheck-run.bat every
REM  night at 03:00 (after the 20:00 deactivate/grant jobs have released the
REM  Chromium profiles). It checks every service UI against the plugins'
REM  selectors and repairs drift into uicheck\overrides.json.
REM
REM    Install :  install-uicheck-task.bat
REM    Run now :  schtasks /Run /TN "AsanaUICheck"
REM    Inspect :  schtasks /Query /TN "AsanaUICheck" /V /FO LIST
REM    Remove  :  schtasks /Delete /TN "AsanaUICheck" /F
REM ===========================================================================
setlocal
cd /d "%~dp0"

set "RUNNER=%~dp0uicheck-run.bat"

if not exist "%RUNNER%" (
  echo [ERROR] %RUNNER% not found.
  pause
  exit /b 1
)

REM First run: make sure uicheck\overrides.json exists with the code defaults
REM so the plugins and the checker agree on the selector set from day one.
if not exist "%~dp0uicheck\overrides.json" (
  echo [INFO] creating uicheck\overrides.json from code defaults...
  "%~dp0.venv\Scripts\python.exe" "%~dp0uicheck.py" --init-overrides
)

REM Run whether logged on or not: /RU + /RP store the account credentials so the
REM task fires in Session 0 with no desktop. The runner uses --headless to suit
REM that. The machine must be powered on (not asleep) at 03:00.
REM /RP * prompts for the password (stored encrypted); /RL HIGHEST avoids UAC
REM truncation. Replace %USERDOMAIN%\%USERNAME% if a different account is wanted.
schtasks /Create /TN "AsanaUICheck" /SC DAILY /ST 03:00 ^
  /RU "%USERDOMAIN%\%USERNAME%" /RP * /RL HIGHEST /F ^
  /TR "\"%RUNNER%\""

if errorlevel 1 (
  echo [ERROR] Failed to register the scheduled task.
) else (
  echo.
  echo Task "AsanaUICheck" created - runs daily at 03:00.
  echo Test it now with:   schtasks /Run /TN "AsanaUICheck"
  echo Report:             uicheck\reports\latest.md
)
pause
