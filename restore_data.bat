@echo off
pushd "%~dp0"
echo === Restore data/ ===
python tools\restore_data.py %*
popd
pause
