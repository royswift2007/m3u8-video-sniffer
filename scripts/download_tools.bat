@echo off
setlocal EnableExtensions
chcp 65001 >nul

for %%I in ("%~dp0..") do set "ROOT_DIR=%%~fI"
set "CLI_SCRIPT=%ROOT_DIR%\scripts\download_dependencies.py"
set "EXIT_CODE=1"
set "COUNTDOWN_SECONDS=10"

echo ============================================
echo   M3U8D 依赖下载脚本
echo ============================================
echo [INFO] ROOT: %ROOT_DIR%
echo [INFO] 默认仅下载必须依赖。
echo [INFO] 如需同时下载建议依赖，可追加参数: --include-recommended
echo [INFO] 将显示依赖清单、逐项状态与下载进度。
echo.

if not exist "%CLI_SCRIPT%" (
    echo [ERROR] 未找到依赖下载入口: "%CLI_SCRIPT%"
    endlocal & exit /b 1
)

if defined M3U8D_PYTHON if exist "%M3U8D_PYTHON%" (
    set "PYTHON_EXE=%M3U8D_PYTHON%"
    goto :run_with_python_exe
)

if exist "%ROOT_DIR%\.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%ROOT_DIR%\.venv\Scripts\python.exe"
    goto :run_with_python_exe
)

where py >nul 2>nul
if not errorlevel 1 goto :run_with_py_launcher

where python >nul 2>nul
if not errorlevel 1 goto :run_with_python_path

echo [ERROR] 未找到可用的 Python 解释器。
echo [ERROR] 请先安装 Python，或设置 M3U8D_PYTHON 环境变量后重试。
endlocal & exit /b 1

:run_with_python_exe
echo [INFO] Python: "%PYTHON_EXE%"
echo [INFO] Command: "%PYTHON_EXE%" "%CLI_SCRIPT%" %*
echo.
"%PYTHON_EXE%" "%CLI_SCRIPT%" %*
set "EXIT_CODE=%ERRORLEVEL%"
goto :finish

:run_with_py_launcher
echo [INFO] Python: py -3
echo [INFO] Command: py -3 "%CLI_SCRIPT%" %*
echo.
py -3 "%CLI_SCRIPT%" %*
set "EXIT_CODE=%ERRORLEVEL%"
goto :finish

:run_with_python_path
echo [INFO] Python: python
echo [INFO] Command: python "%CLI_SCRIPT%" %*
echo.
python "%CLI_SCRIPT%" %*
set "EXIT_CODE=%ERRORLEVEL%"
goto :finish

:finish
echo.
if "%EXIT_CODE%"=="0" (
    echo [INFO] 依赖下载完成。
) else (
    echo [ERROR] 依赖下载失败，退出码: %EXIT_CODE%
)
echo [INFO] 此窗口将在 %COUNTDOWN_SECONDS% 秒后自动关闭...
timeout /t %COUNTDOWN_SECONDS% /nobreak >nul

endlocal & exit /b %EXIT_CODE%
