@echo off
REM ===========================================================================
REM  Nightly UI-drift check: walk every browser-based service, verify the
REM  selectors the deactivate/grant plugins rely on, repair drifted ones into
REM  uicheck\overrides.json. Nothing is removed or invited.
REM  Scheduled by install-uicheck-task.bat to run daily at 03:00.
REM  Output is appended to uicheck.log next to this file; the Markdown report
REM  lands in uicheck\reports\<date>.md (latest.md = most recent).
REM ===========================================================================
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
set "SCRIPT=%~dp0uicheck.py"

if not exist "%PY%" (
  echo [ERROR] %PY% not found. Run setup-windows.bat first.>> "uicheck.log"
  exit /b 1
)

REM --headless so it works in a non-interactive (Session 0) scheduled run.
REM NOTE: this requires every service's saved session in .browser_profiles\
REM to still be valid; an expired login/2FA/captcha will show up as a failed
REM "login" step in the report rather than hang forever (per-step timeouts).
REM Deactivate/grant run at 20:00 and must not share the Chromium profiles
REM with this job, hence the 03:00 slot.
echo ===== %DATE% %TIME% : uicheck --yes --headless =====>> "uicheck.log"
"%PY%" "%SCRIPT%" --yes --headless >> "uicheck.log" 2>&1
