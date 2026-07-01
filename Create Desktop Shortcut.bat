@echo off
REM Double-click to put a "STS2 Mod Uploader" icon on your Desktop.
REM Then launch the dashboard any time from that icon.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_shortcut.ps1"
echo.
pause
