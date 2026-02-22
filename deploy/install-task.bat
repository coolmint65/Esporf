@echo off
:: Run this script as Administrator to install Esporf as a startup task

set SCRIPT_DIR=%~dp0

echo Installing Esporf as a scheduled task...
schtasks /create /tn "Esporf Bot" /tr "\"%SCRIPT_DIR%run-esporf.bat\"" /sc onlogon /rl highest /f

if %errorlevel% equ 0 (
    echo.
    echo Success! Esporf will start automatically when you log in.
    echo.
    echo To start it right now:
    echo   schtasks /run /tn "Esporf Bot"
    echo.
    echo To stop it:
    echo   schtasks /end /tn "Esporf Bot"
    echo.
    echo To remove it:
    echo   schtasks /delete /tn "Esporf Bot" /f
) else (
    echo.
    echo Failed. Make sure you're running this as Administrator.
    echo Right-click install-task.bat and select "Run as administrator"
)

pause
