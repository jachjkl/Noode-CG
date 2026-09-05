@echo off
setlocal
set "NOODE_ROOT=%~dp0."
set "NOODE_LAUNCHER=%~dp0launch-dashboard.ps1"
set "NOODE_POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

if not exist "%NOODE_POWERSHELL%" (
    echo Windows PowerShell 5.1 was not found.
    pause
    exit /b 1
)

if not exist "%NOODE_LAUNCHER%" (
    echo Noode-CG launcher was not found: %NOODE_LAUNCHER%
    pause
    exit /b 1
)

"%NOODE_POWERSHELL%" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command "Start-Process -FilePath $env:NOODE_POWERSHELL -WindowStyle Hidden -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',([char]34 + $env:NOODE_LAUNCHER + [char]34),'-Root',([char]34 + $env:NOODE_ROOT + [char]34))"
set "NOODE_EXIT_CODE=%ERRORLEVEL%"

if not "%NOODE_EXIT_CODE%"=="0" (
    echo.
    echo Noode-CG failed to start. See logs\dashboard-launch.log.
    pause
)

exit /b %NOODE_EXIT_CODE%
