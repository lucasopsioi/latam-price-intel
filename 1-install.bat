@echo off
REM ASCII-only (cp936 hazard). Paths via %~dp0.
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
call "%~dp0tools\pyenv.cmd"

echo ============================================
echo   LATAM Competitor Intelligence Hub
echo   First-time setup
echo ============================================
echo.
echo Using interpreter: %HUBPY%
echo.

%HUBPY% --version >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Interpreter not usable: %HUBPY%
  pause
  exit /b 1
)

echo [1/3] Installing Python packages...
%HUBPY% -m pip install -r requirements.txt --quiet --disable-pip-version-check
if errorlevel 1 (
  echo [ERROR] pip install failed. See messages above.
  pause
  exit /b 1
)

echo [2/3] Installing Chromium for Playwright (~150MB, first time only)...
%HUBPY% -m playwright install chromium
if errorlevel 1 echo [WARN] Chromium install failed. Rerun this script.

echo [3/3] Creating database and loading config...
%HUBPY% main.py init
if errorlevel 1 (
  echo [ERROR] DB init failed.
  pause
  exit /b 1
)

echo.
echo Setup complete.
echo Next: run tools\install-service.ps1 to enable autostart + desktop icon.
pause
