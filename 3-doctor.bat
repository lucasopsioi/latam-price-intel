@echo off
REM ASCII-only (cp936 hazard). Paths via %~dp0.
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
call "%~dp0tools\pyenv.cmd"

echo Running channel health check (real network, a few minutes)...
echo.
%HUBPY% main.py doctor %*
echo.
pause
