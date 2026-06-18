# Running the Asana Task Monitor on Windows 11

The service is pure Python + Playwright (Chromium), so it runs natively on
Windows. Three batch files make it turnkey.

## First-time setup

1. Install **Python 3.12** from <https://www.python.org/downloads/windows/> —
   tick **"Add python.exe to PATH"**.
2. Copy this project folder onto the machine.
3. Double-click **`setup-windows.bat`** (creates `.venv`, installs deps, downloads
   Chromium).
4. Put your **`.env`** in this folder (Asana token, TOTP secrets, service URLs).
5. **Re-prime the browser sessions on this machine** — do *not* copy
   `.browser_profiles/` from a Mac; Chromium user-data dirs don't migrate across
   operating systems (lock files + OS-bound cookie encryption). For each service:

   ```powershell
   python prime_session.py --service autodesk
   python prime_session.py --service plastic-scm
   # ...one per service, logging in by hand in the window that opens
   ```

   This also re-establishes each session from this machine's IP — relevant for
   the captcha/network constraints (e.g. Autodesk hCaptcha is manual-only).

## Daily use

Double-click **`run.bat`** (or `run.bat --port 5050 --interval 600`).
Dashboard: <http://127.0.0.1:5111>. Press **Ctrl+C** to stop, or use the
**Stop Service** button in the UI.

The **Run now**, **Offboarding**, and **Onboarding** buttons all work; the
browser opens visibly (headful) so you can clear captcha/2FA prompts.

## Keep it running unattended

Double-click **`install-autostart.bat`** once. It registers a Task Scheduler
job (`AsanaMonitor`) that launches the monitor windowless at every logon.

```powershell
schtasks /Run    /TN "AsanaMonitor"      # start it now without re-logging in
schtasks /Delete /TN "AsanaMonitor" /F   # remove autostart
```

> Note: autostart runs the snapshot scheduler in the background. When you click
> Offboarding/Onboarding from the dashboard a visible browser still opens for
> captcha/2FA, so use the machine interactively for those actions.

## If something fails

- `Activate.ps1 cannot be loaded` → run once in PowerShell:
  `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
- `playwright` not found → re-run `setup-windows.bat`; ensure the venv activated.
- Port already in use → `run.bat --port 5050`.
