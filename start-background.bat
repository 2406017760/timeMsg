@echo off
rem Start TimeMsg in the background (no window). Log: logs\timemsg.log
rem NOTE: keep this file pure ASCII - cmd.exe parses .bat with the OEM codepage.
setlocal
cd /d "%~dp0"
set "PYW="
where pythonw >nul 2>nul && set "PYW=pythonw"
if not defined PYW (
  for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if exist "%%D\pythonw.exe" set "PYW=%%D\pythonw.exe"
  )
)
if not defined PYW (
  echo pythonw.exe not found. Install Python first, or use run.bat in foreground.
  exit /b 1
)
start "" "%PYW%" -m app.main serve
echo Started in background. Status: http://127.0.0.1:8765/setup
