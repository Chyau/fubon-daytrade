@echo off
pushd "%~dp0"
echo === Export read-only snapshot ===
python tools\export_snapshot.py %*
python tools\lock_snapshot.py snapshot\index.html docs\index.html
if errorlevel 1 goto :fail
git add snapshot
git commit -q -m "Update read-only snapshot" --only snapshot
if errorlevel 1 echo (No snapshot changes to commit)
git pull --rebase -q
git push
goto :done
:fail
echo Snapshot export FAILED - nothing committed.
:done
popd
