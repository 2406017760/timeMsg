@echo off
rem Run in foreground so you can watch the log. Closing this window stops the app.
rem NOTE: keep this file pure ASCII - cmd.exe parses .bat with the OEM codepage.
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m app.main serve
) else (
  python -m app.main serve
)
pause
