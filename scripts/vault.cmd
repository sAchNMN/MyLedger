@echo off
rem ===========================================================================
rem  MyLedger - encrypted vault for real bill data (7-Zip AES-256).
rem  Usage:  vault.cmd init | open | add | close | status
rem          vault.cmd add -VaultPath "D:\SomeWhere\MyVault"
rem
rem  Double-click with no argument to see the current status.
rem  See scripts\vault.ps1 header (in Chinese) for the full explanation.
rem
rem  This .cmd file is intentionally ASCII-only. All Chinese output comes from
rem  the PowerShell script. Keep it that way to avoid codepage garbling.
rem ===========================================================================
chcp 65001 >nul
setlocal
if "%~1"=="" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0vault.ps1" status
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0vault.ps1" %*
)
set EXITCODE=%ERRORLEVEL%
echo.
pause
exit /b %EXITCODE%
