@echo off
setlocal
chcp 65001 >nul
title Noode-CG Cloud and Local Selection
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0manual-start.ps1"
set "NOODE_EXIT_CODE=%ERRORLEVEL%"
if not "%NOODE_EXIT_CODE%"=="0" (
    echo.
    echo Noode-CG failed. The window is being kept open so you can read the error.
    pause
)
exit /b %NOODE_EXIT_CODE%
