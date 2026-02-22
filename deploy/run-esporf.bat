@echo off
title Esporf Bot
cd /d "%~dp0.."

:loop
echo [%date% %time%] Starting Esporf bot...
esporf run
echo [%date% %time%] Bot exited with code %errorlevel%. Restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto loop
