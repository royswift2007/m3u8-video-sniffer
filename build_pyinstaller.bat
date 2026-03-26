@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "ROOT_DIR=%~dp0"
for %%I in ("%ROOT_DIR%") do set "ROOT_DIR=%%~fI"
set "BUILD_SCRIPT=%ROOT_DIR%build_pyinstaller.py"

echo ============================================
echo   M3U8D PyInstaller 构建脚本
echo ============================================
echo [INFO] ROOT: %ROOT_DIR%
echo [INFO] OUTPUT: %ROOT_DIR%dist\M3U8D
echo.

if not exist "%BUILD_SCRIPT%" (
    echo [ERROR] 未找到构建入口: "%BUILD_SCRIPT%"
    endlocal & exit /b 1
)

if defined M3U8D_PYTHON if exist "%M3U8D_PYTHON%" (
    set "PYTHON_EXE=%M3U8D_PYTHON%"
    goto :run_with_python_exe
)

if exist "%ROOT_DIR%.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%ROOT_DIR%.venv\Scripts\python.exe"
    goto :run_with_python_exe
)

where py >nul 2>nul
if not errorlevel 1 goto :run_with_py_launcher

where python >nul 2>nul
if not errorlevel 1 goto :run_with_python_path

echo [ERROR] 未找到可用的 Python 解释器。
echo [ERROR] 请先安装 Python / PyInstaller，或设置 M3U8D_PYTHON 环境变量后重试。
endlocal & exit /b 1

:run_with_python_exe
echo [INFO] Python: "%PYTHON_EXE%"
echo [INFO] Command: "%PYTHON_EXE%" "%BUILD_SCRIPT%" %*
echo.
"%PYTHON_EXE%" "%BUILD_SCRIPT%" %*
set "EXIT_CODE=%ERRORLEVEL%"
goto :finish

:run_with_py_launcher
echo [INFO] Python: py -3
echo [INFO] Command: py -3 "%BUILD_SCRIPT%" %*
echo.
py -3 "%BUILD_SCRIPT%" %*
set "EXIT_CODE=%ERRORLEVEL%"
goto :finish

:run_with_python_path
echo [INFO] Python: python
echo [INFO] Command: python "%BUILD_SCRIPT%" %*
echo.
python "%BUILD_SCRIPT%" %*
set "EXIT_CODE=%ERRORLEVEL%"
goto :finish

:finish
echo.
if not "%EXIT_CODE%"=="0" goto :print_error

echo [INFO] PyInstaller build completed.
echo [INFO] Output: "%ROOT_DIR%dist\M3U8D"
endlocal & exit /b %EXIT_CODE%

:print_error
echo [ERROR] PyInstaller build failed. Exit code: %EXIT_CODE%
endlocal & exit /b %EXIT_CODE%
