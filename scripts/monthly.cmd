@echo off
rem ===========================================================================
rem  MyLedger - monthly batch run (double-click entry).
rem
rem  Scans %USERPROFILE%\MyLedgerVault\inbox, imports whatever is there,
rem  prints the monthly report. Safe to run repeatedly: re-importing the same
rem  file is a no-op thanks to same-source + same-transaction-id dedup.
rem
rem  This .cmd is intentionally ASCII-only. All Chinese output comes from the
rem  PowerShell / Python side. Keep it that way to avoid codepage garbling.
rem ===========================================================================
chcp 65001 >nul
setlocal enabledelayedexpansion

set "REPO=%~dp0.."
set "PYEXE="
set "VENV_PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

rem Prefer the isolated venv interpreter. PDF statement support needs PyMuPDF,
rem which is installed there; falling back to a bare python would turn every
rem PDF import into a confusing "no PDF library" error.
if exist "%VENV_PY%" set "PYEXE=%VENV_PY%"
if not defined PYEXE (
  where python >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
  if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe" (
    set "PYEXE=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
  )
)
if not defined PYEXE (
  echo.
  echo [ERROR] Python not found. Install Python 3.10+ and make sure "python" is on PATH.
  echo.
  pause
  exit /b 3
)

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

cd /d "%REPO%"
"%PYEXE%" -m importer monthly %*
set "EXITCODE=%ERRORLEVEL%"

echo.
pause
exit /b %EXITCODE%
