@echo off
setlocal

cd /d "%~dp0\.."
echo [SMOKE] compileall...
python -m compileall protocol_handler.pyw core ui engines utils tests
if errorlevel 1 (
  echo [SMOKE] compileall failed
  exit /b 1
)

echo [SMOKE] pytest...
python -m pytest tests -q -p no:cacheprovider
if errorlevel 1 (
  echo [SMOKE] pytest failed
  exit /b 1
)

echo [SMOKE] all passed
exit /b 0

