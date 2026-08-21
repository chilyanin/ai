@echo off
REM ===========================================================================
REM  Run grant.py for Asana "Заявка на лицензию/доступ ..." tasks due TODAY and
REM  actually perform the access grants. Scheduled by install-grant-task.bat to
REM  run daily at 20:00. Output is appended to grant-run.log next to this file.
REM ===========================================================================
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
set "SCRIPT=%~dp0grant.py"

if not exist "%PY%" (
  echo [ERROR] %PY% not found. Run setup-windows.bat first.>> "grant-run.log"
  exit /b 1
)

REM Compute today's date as YYYY-MM-DD (locale-independent via PowerShell).
for /f %%d in ('powershell -NoProfile -Command "(Get-Date).ToString('yyyy-MM-dd')"') do set "TODAY=%%d"

REM --headless so it works in a non-interactive (Session 0) scheduled run.
REM NOTE: this actually performs grants and requires every service's saved
REM session in .browser_profiles\ to still be valid; an expired login/2FA/captcha
REM will hang with no desktop to interact with.
echo ===== %DATE% %TIME% : --date %TODAY% --yes --headless =====>> "grant-run.log"
"%PY%" "%SCRIPT%" --date %TODAY% --yes --headless >> "grant-run.log" 2>&1
