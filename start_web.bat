@echo off
pushd "%~dp0"
python live.py --brief
echo.
echo Starting web dashboard: http://127.0.0.1:9757
python app.py
popd
pause
