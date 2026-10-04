@echo off
rem ===========================================================================
rem  MyLedger - install the pre-commit privacy gate.
rem  Double-click this file, or run it from a terminal.
rem
rem  Why -ExecutionPolicy Bypass: Windows defaults to Restricted and refuses to
rem  run local .ps1 files. Bypass applies to this process only and changes no
rem  system setting, so it is cleaner than Set-ExecutionPolicy.
rem
rem  This .cmd file is intentionally ASCII-only. All Chinese output comes from
rem  the PowerShell script. Keep it that way to avoid codepage garbling.
rem ===========================================================================
chcp 65001 >nul
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-git-hooks.ps1" %*
set EXITCODE=%ERRORLEVEL%
echo.
pause
exit /b %EXITCODE%
