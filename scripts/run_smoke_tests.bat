@echo off
setlocal

cd /d "%~dp0\.."
echo [SMOKE] compileall...
python -m compileall protocol_handler.pyw core ui engines utils tests scripts
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

echo [SMOKE] download success-rate smoke scripts...
python scripts/smoke_engine_select_mime.py
if errorlevel 1 exit /b 1
python scripts/smoke_hls_probe_soft_fail.py
if errorlevel 1 exit /b 1
python scripts/smoke_backoff_retry.py
if errorlevel 1 exit /b 1
python scripts/smoke_catcatch_auth.py
if errorlevel 1 exit /b 1
python scripts/smoke_segment_suppression.py
if errorlevel 1 exit /b 1
python scripts/smoke_header_forwarding.py
if errorlevel 1 exit /b 1
python scripts/smoke_auth_retry_site_rules.py
if errorlevel 1 exit /b 1
python scripts/smoke_rate_limit_strategy.py
if errorlevel 1 exit /b 1
python scripts/smoke_ytdlp_streamlink_m6.py
if errorlevel 1 exit /b 1
python scripts/smoke_progress_consistency.py
if errorlevel 1 exit /b 1

echo [SMOKE] stop-response benchmark (F-09 / R14.4: P95 <=2.0s gate)...
python scripts/smoke_stop_response_benchmark.py
if errorlevel 1 (
  echo [SMOKE] stop-response benchmark failed
  exit /b 1
)

echo [SMOKE] all passed
exit /b 0

