@echo off
title Esporf Launcher
echo ========================================
echo   Esporf - eSoccer Trend Analyzer
echo ========================================
echo.

:: Start the API server in a new window
echo Starting API server on port 8000...
start "Esporf API" cmd /k "cd /d %~dp0 && python -m uvicorn esporf.api:app --host 127.0.0.1 --port 8000"

:: Give the API a moment to start
timeout /t 3 /nobreak >nul

:: Start the frontend dev server in a new window
echo Starting frontend on port 5173...
start "Esporf Frontend" cmd /k "cd /d %~dp0\frontend && npm run dev"

:: Wait for frontend to start then open browser
timeout /t 5 /nobreak >nul
echo Opening browser...
start http://localhost:5173

echo.
echo ========================================
echo   Everything is running!
echo   Frontend: http://localhost:5173
echo   API:      http://localhost:8000
echo   API Docs: http://localhost:8000/docs
echo ========================================
echo.
echo Close the other two windows to stop.
pause
