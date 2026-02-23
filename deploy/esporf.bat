@echo off
:: Esporf management shortcut for Windows
:: Place this file somewhere on your PATH, or run from the project folder

set DIR=%~dp0..

if "%1"=="start"   ( docker compose -f "%DIR%\docker-compose.yml" up -d & goto :eof )
if "%1"=="stop"    ( docker compose -f "%DIR%\docker-compose.yml" down & goto :eof )
if "%1"=="restart" ( docker compose -f "%DIR%\docker-compose.yml" down & docker compose -f "%DIR%\docker-compose.yml" up -d & goto :eof )
if "%1"=="status"  ( docker compose -f "%DIR%\docker-compose.yml" ps & goto :eof )
if "%1"=="logs"    ( docker compose -f "%DIR%\docker-compose.yml" logs -f & goto :eof )
if "%1"=="update"  ( git -C "%DIR%" pull & docker compose -f "%DIR%\docker-compose.yml" down & docker compose -f "%DIR%\docker-compose.yml" up -d --build & goto :eof )
if "%1"=="config"  ( notepad "%DIR%\.env" & goto :eof )

echo Usage: esporf ^<command^>
echo.
echo   start     Start the bot
echo   stop      Stop the bot
echo   restart   Restart the bot
echo   status    Check if the bot is running
echo   logs      Tail live logs
echo   update    Pull latest code and restart
echo   config    Edit .env settings
