@echo off
rem Runs AutoTradeBot around the clock: if the app stops unexpectedly, it is started again
rem 30 seconds later. Quitting from the dashboard (or Ctrl+C) ends it for good.
rem
rem   scripts\run_24_7.bat              (from the project folder, or double-click it)
rem
rem To start it when Windows starts, put a shortcut to this file in the Startup folder
rem (Win+R, shell:startup).

cd /d "%~dp0.."

:run
".venv\Scripts\python.exe" run.py --no-browser %*
if %ERRORLEVEL% EQU 0 goto :done
echo.
echo   The app stopped unexpectedly (exit code %ERRORLEVEL%) - starting it again in 30 seconds.
echo   Close this window to stop it.
timeout /t 30 /nobreak >nul
goto run

:done
echo   AutoTradeBot has quit.
