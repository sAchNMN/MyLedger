@echo off
REM ============================================================================
REM  MyLedger launcher
REM
REM  KEEP THIS FILE ASCII-ONLY. Do not add Chinese characters here.
REM
REM  Why: cmd.exe reads a batch file by advancing its file pointer, and under
REM  codepage 65001 it advances by CHARACTERS while the UTF-8 file has multiple
REM  BYTES per character. The pointer drifts, so cmd silently SKIPS whole lines
REM  and CUTS other lines in half, then tries to run the leftover as a command.
REM  Symptom: stray "'xxx' is not recognized as an internal or external command".
REM  Tested on Windows 11 build 26200. All Chinese output is printed by the
REM  Python app instead - Python writes to the console through the wide API,
REM  so Chinese renders correctly no matter what the console codepage is.
REM
REM  Keep CRLF line endings and no BOM as well.
REM ============================================================================

setlocal EnableExtensions
cd /d "%~dp0"
title MyLedger Console
chcp 65001 >nul

set "PYEXE="
set "PYARG="

if not defined PYEXE if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not defined PYEXE if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "PYEXE=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

if not defined PYEXE (
  for /f "delims=" %%i in ('where python.exe 2^>nul') do (
    if not defined PYEXE set "PYEXE=%%i"
  )
)

if not defined PYEXE (
  for /f "delims=" %%i in ('where py.exe 2^>nul') do (
    if not defined PYEXE set "PYEXE=%%i"
  )
  if defined PYEXE set "PYARG=-3"
)

if not defined PYEXE goto :nopython

"%PYEXE%" %PYARG% -m importer web %*
if errorlevel 1 goto :failed
goto :eof

:nopython
echo.
echo [MyLedger] Python interpreter not found.
echo.
echo   Fix it in either way:
echo     1. Install Python 3.11 or newer from python.org and tick
echo        "Add python.exe to PATH" during setup.
echo     2. Or open this file with Notepad and set PYEXE to the full
echo        path of a python.exe already on this machine.
echo.
pause
exit /b 1

:failed
echo.
echo [MyLedger] The console exited with an error. See the messages above.
echo.
pause
exit /b 1
