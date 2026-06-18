@echo off
REM ===========================================================================
REM  First-time setup for the Asana Task Monitor on Windows 11.
REM  Creates a virtualenv, installs dependencies, and downloads Chromium.
REM  Run this ONCE. After that, use run.bat to start the dashboard.
REM ===========================================================================
setlocal
cd /d "%~dp0"

where py >nul 2>&1
if errorlevel 1 (
  echo [ERROR] The Python launcher 'py' was not found.
  echo         Install Python 3.12 from https://www.python.org/downloads/windows/
  echo         and tick "Add python.exe to PATH" during installation.
  pause
  exit /b 1
)

echo === Creating virtual environment (.venv) ===
py -3.12 -m venv .venv
if errorlevel 1 ( echo [ERROR] venv creation failed. & pause & exit /b 1 )

call .venv\Scripts\activate.bat

echo === Upgrading pip ===
python -m pip install --upgrade pip

echo === Installing Python dependencies ===
pip install -r requirements.txt
if errorlevel 1 ( echo [ERROR] pip install failed. & pause & exit /b 1 )

echo === Installing Playwright Chromium ===
playwright install chromium
if errorlevel 1 ( echo [ERROR] playwright install failed. & pause & exit /b 1 )

echo.
echo ===========================================================================
echo  Setup complete.
echo.
echo  Next steps:
echo    1. Copy your .env file into this folder (Asana token, TOTP, URLs).
echo    2. Re-prime each service session on THIS machine (do not copy
echo       .browser_profiles from another OS):
echo           python prime_session.py --service autodesk
echo           python prime_session.py --service plastic-scm
echo           ...one per service, logging in by hand.
echo    3. Start the dashboard:   run.bat
echo ===========================================================================
pause
