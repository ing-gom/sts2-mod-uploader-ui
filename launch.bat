@echo off
REM ============================================================
REM  STS2 Mod Uploader UI - one-click launcher (Windows)
REM  Double-click this file (or the desktop shortcut created by
REM  "Create Desktop Shortcut.bat") to start the dashboard and
REM  open it in your browser.
REM
REM  Portable: runs from wherever this .bat lives - no hard-coded
REM  paths, so it works after you clone/move the repo.
REM ============================================================
setlocal
title STS2 Mod Uploader UI

REM Always run from this script's own folder.
cd /d "%~dp0"

REM Pick a Python: prefer the "py" launcher, fall back to python on PATH.
set "PYCMD="
where py >nul 2>&1 && set "PYCMD=py -3"
if not defined PYCMD (
    where python >nul 2>&1 && set "PYCMD=python"
)
if not defined PYCMD (
    echo.
    echo  [!] Python was not found.
    echo      Install Python 3.8+ from https://www.python.org/downloads/
    echo      and tick "Add python.exe to PATH" during setup, then try again.
    echo.
    pause
    exit /b 1
)

REM Port the dashboard binds (matches STS2_DASH_PORT / config.json "port").
set "PORT=8791"
if defined STS2_DASH_PORT set "PORT=%STS2_DASH_PORT%"

REM Open the browser a moment after the server binds.
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:%PORT%/"

echo ============================================================
echo  Starting STS2 Mod Uploader UI ...
echo  A browser tab will open at http://127.0.0.1:%PORT%/
echo  (If it did not bind on %PORT%, use the URL printed below.)
echo.
echo  Close this window or press Ctrl+C to stop the server.
echo ============================================================
%PYCMD% "%~dp0workshop_dashboard.py"

echo.
echo Server stopped.
pause
endlocal
