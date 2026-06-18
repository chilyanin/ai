@echo off
REM ===========================================================================
REM  Run deactivate.py in --find-only mode for tasks due TODAY.
REM  Scheduled by install-deactivate-task.bat to run daily at 20:00.
REM  Output is appended to deactivate-find.log next to this file.
REM ===========================================================================
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
set "SCRIPT=%~dp0deactivate.py"

if not exist "%PY%" (
  echo [ERROR] %PY% not found. Run setup-windows.bat first.>> "deactivate-find.log"
  exit /b 1
)

REM Compute today's date as YYYY-MM-DD (locale-independent via PowerShell).
for /f %%d in ('powershell -NoProfile -Command "(Get-Date).ToString('yyyy-MM-dd')"') do set "TODAY=%%d"

echo ===== %DATE% %TIME% : --date %TODAY% --yes --find-only =====>> "deactivate-find.log"
"%PY%" "%SCRIPT%" --date %TODAY% --yes --find-only >> "deactivate-find.log" 2>&1
