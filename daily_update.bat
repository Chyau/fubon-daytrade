@echo off
pushd "%~dp0"
python tools\daily_update.py %*
popd
