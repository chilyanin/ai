@echo off
REM ===========================================================================
REM  Register a Windows Task Scheduler job that starts the monitor in the
REM  background at every logon (no console window). The monitor's own 15-min
REM  scheduler then keeps taking snapshots; the dashboard stays reachable at
REM  http://127.0.0.1:5111.
REM
REM    Install :  install-autostart.bat
REM    Run now :  schtasks /Run /TN "AsanaMonitor"
REM    Remove  :  schtasks /Delete /TN "AsanaMonitor" /F
REM ===========================================================================
setlocal
cd /d "%~dp0"

set "PYW=%~dp0.venv\Scripts\pythonw.exe"
set "SCRIPT=%~dp0monitor.py"

if not exist "%PYW%" (
  echo [ERROR] %PYW% not found.
  echo         Run setup-windows.bat first.
  pause
  exit /b 1
)

REM pythonw.exe runs windowless. The app is working-directory independent
REM (it resolves paths from __file__), so no /SD start-in is required.
schtasks /Create /TN "AsanaMonitor" /SC ONLOGON /RL LIMITED /F ^
  /TR "\"%PYW%\" \"%SCRIPT%\""

if errorlevel 1 (
  echo [ERROR] Failed to register the scheduled task.
) else (
  echo.
  echo Task "AsanaMonitor" created - it will start at your next logon.
  echo Start it right now with:   schtasks /Run /TN "AsanaMonitor"
)
pause
