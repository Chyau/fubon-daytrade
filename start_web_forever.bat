@echo off
pushd "%~dp0"
echo === Web dashboard auto-restart loop (restarts 5s after exit) ===
python live.py --brief
echo.
echo http://127.0.0.1:9757
:loop
python app.py
echo.
echo [%date% %time%] Server exited. Restarting in 5 seconds...
timeout /t 5 >nul
goto loop
