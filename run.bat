@echo off
REM ===========================================================================
REM  Launch the Asana Task Monitor dashboard on Windows 11.
REM  Bootstraps the virtualenv automatically on first run.
REM  Any extra args are passed through to monitor.py, e.g.:
REM      run.bat --port 5050 --interval 600
REM      run.bat --once
REM ===========================================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [INFO] .venv not found - running first-time setup...
  call "%~dp0setup-windows.bat"
)

call .venv\Scripts\activate.bat
echo Starting monitor at http://127.0.0.1:5111   (press Ctrl+C to stop)
python monitor.py %*
