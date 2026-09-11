@echo off
REM ============================================================
REM  ASCII-ONLY BY DESIGN (cp936 hazard).
REM  Forwards to the WINDOWLESS launcher. Kept only so that old
REM  shortcuts / muscle memory still work; the desktop icon now
REM  points straight at pythonw.exe tools\launcher.py so that no
REM  console window is ever created.
REM ============================================================
setlocal
cd /d "%~dp0.."
call "%~dp0pyenv.cmd"
for %%I in ("%HUBPY%") do set "HUBPYW=%%~dpIpythonw.exe"
if exist "%HUBPYW%" (
  start "" "%HUBPYW%" "%~dp0launcher.py"
) else (
  start "" "%HUBPY%" "%~dp0launcher.py"
)
exit /b 0
