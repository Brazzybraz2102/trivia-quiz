@echo off
REM Double-click to start the Fall Fest Party Pack on Windows.
cd /d "%~dp0"
where node >nul 2>nul
if errorlevel 1 (
  echo Node.js is not installed. Get the LTS version from https://nodejs.org, then double-click this file again.
  pause
  exit /b 1
)
start "" http://localhost:3000/host
node server.js
pause
