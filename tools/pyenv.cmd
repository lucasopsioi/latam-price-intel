@echo off
REM ============================================================
REM  ASCII-ONLY BY DESIGN (cp936 hazard).
REM  Resolves the interpreter that actually HAS this project's
REM  dependencies, into %HUBPY%.
REM
REM  Why this exists: bare "python" on PATH resolves to an
REM  unrelated venv (hermes-agent) with no apscheduler, so
REM  "python main.py serve" dies with ModuleNotFoundError.
REM  Never rely on PATH here.
REM ============================================================
set "HUBPY="
set "CAND=C:\Python314\python.exe"
if exist "%CAND%" (
  "%CAND%" -c "import apscheduler" >nul 2>&1
  if not errorlevel 1 set "HUBPY=%CAND%"
)
if not defined HUBPY (
  for %%P in (py.exe) do if not "%%~$PATH:P"=="" (
    py -3.14 -c "import apscheduler" >nul 2>&1
    if not errorlevel 1 set "HUBPY=py -3.14"
  )
)
if not defined HUBPY (
  python -c "import apscheduler" >nul 2>&1
  if not errorlevel 1 set "HUBPY=python"
)
if not defined HUBPY (
  REM Nothing has the deps yet. During first-time setup that is
  REM expected, so fall back to the known interpreter and let
  REM 1-install.bat install INTO it.
  if exist "%CAND%" (
    set "HUBPY=%CAND%"
  ) else (
    set "HUBPY=python"
  )
)
exit /b 0
