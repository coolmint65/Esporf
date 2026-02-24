@echo off
:: Esporf management shortcut for Windows
:: Edit ESPORF_DIR below if your repo is in a different location.

set ESPORF_DIR=%USERPROFILE%\Esporf

if "%1"=="start"   ( docker compose -f "%ESPORF_DIR%\docker-compose.yml" up -d & goto :eof )
if "%1"=="stop"    ( docker compose -f "%ESPORF_DIR%\docker-compose.yml" down & goto :eof )
if "%1"=="restart" ( docker compose -f "%ESPORF_DIR%\docker-compose.yml" down & docker compose -f "%ESPORF_DIR%\docker-compose.yml" up -d & goto :eof )
if "%1"=="status"  ( docker compose -f "%ESPORF_DIR%\docker-compose.yml" ps & goto :eof )
if "%1"=="logs"    ( docker compose -f "%ESPORF_DIR%\docker-compose.yml" logs -f & goto :eof )
if "%1"=="update"  ( git -C "%ESPORF_DIR%" pull & echo Updated. Run 'esporf restart' to apply. & goto :eof )
if "%1"=="config"  ( notepad "%ESPORF_DIR%\.env" & goto :eof )

echo Usage: esporf ^<command^>
echo.
echo   start     Start the bot
echo   stop      Stop the bot
echo   restart   Restart the bot
echo   status    Check if the bot is running
echo   logs      Tail live logs
echo   update    Pull latest code (does NOT restart)
echo   config    Edit .env settings
