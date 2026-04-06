@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

echo ============================================
echo   M3U8D 协议注册工具
echo ============================================
echo.

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%") do set "SCRIPT_DIR=%%~fI"
for %%I in ("%SCRIPT_DIR%..") do set "INSTALL_DIR=%%~fI"
set "HANDLER_EXE="
set "HANDLER_SCRIPT="
set "PYTHON_EXE="
set "COMMAND_VALUE="
set "DEFAULT_ICON_VALUE="

rem 优先使用打包后的协议处理器。
call :resolve_handler_exe "%INSTALL_DIR%\protocol_handler\protocol_handler.exe"
if not defined HANDLER_EXE call :resolve_handler_exe "%INSTALL_DIR%\protocol_handler.exe"
if not defined HANDLER_EXE call :resolve_handler_exe "%SCRIPT_DIR%protocol_handler.exe"

rem 开发源码环境回退到 protocol_handler.pyw。
if not defined HANDLER_EXE call :resolve_handler_script "%INSTALL_DIR%\protocol_handler.pyw"

if defined HANDLER_EXE (
    set "COMMAND_VALUE=\"!HANDLER_EXE!\" \"%%1\""
    set "DEFAULT_ICON_VALUE=\"!HANDLER_EXE!\",0"
)

if not defined COMMAND_VALUE if defined HANDLER_SCRIPT (
    call :resolve_python_executable
    if not defined PYTHON_EXE (
        echo [ERROR] 找到了源码协议处理器，但未找到可用的 Python 解释器。
        echo [ERROR] 请先安装 Python，或设置 M3U8D_PYTHON 后重试。
        endlocal & exit /b 1
    )
    set "COMMAND_VALUE=\"!PYTHON_EXE!\" \"!HANDLER_SCRIPT!\" \"%%1\""
    call :resolve_icon_value
)

if not defined COMMAND_VALUE (
    echo [ERROR] 未找到可用于注册协议的处理器入口。
    echo [ERROR] 优先路径:
    echo         "%INSTALL_DIR%\protocol_handler\protocol_handler.exe"
    echo [ERROR] 兼容回退路径:
    echo         "%INSTALL_DIR%\protocol_handler.exe"
    echo         "%SCRIPT_DIR%protocol_handler.exe"
    echo         "%INSTALL_DIR%\protocol_handler.pyw"
    endlocal & exit /b 1
)

echo [INFO] Install Root: "%INSTALL_DIR%"
if defined HANDLER_EXE echo [INFO] Handler EXE: "!HANDLER_EXE!"
if defined HANDLER_SCRIPT echo [INFO] Handler Script: "!HANDLER_SCRIPT!"
if defined PYTHON_EXE echo [INFO] Python: "!PYTHON_EXE!"
echo [INFO] Command: !COMMAND_VALUE!
echo [INFO] 正在注册 m3u8dl:// 协议...
echo.

reg add "HKCU\Software\Classes\m3u8dl" /ve /d "URL:M3U8DL Protocol" /f >nul
if errorlevel 1 goto :register_failed

reg add "HKCU\Software\Classes\m3u8dl" /v "URL Protocol" /d "" /f >nul
if errorlevel 1 goto :register_failed

if defined DEFAULT_ICON_VALUE (
    reg add "HKCU\Software\Classes\m3u8dl\DefaultIcon" /ve /d "!DEFAULT_ICON_VALUE!" /f >nul
    if errorlevel 1 goto :register_failed
)

reg add "HKCU\Software\Classes\m3u8dl\shell" /f >nul
if errorlevel 1 goto :register_failed

reg add "HKCU\Software\Classes\m3u8dl\shell\open" /f >nul
if errorlevel 1 goto :register_failed

reg add "HKCU\Software\Classes\m3u8dl\shell\open\command" /ve /d "!COMMAND_VALUE!" /f >nul
if errorlevel 1 goto :register_failed

echo [SUCCESS] m3u8dl:// 协议已注册。
if defined HANDLER_EXE echo [SUCCESS] 目标程序: "!HANDLER_EXE!"
if defined HANDLER_SCRIPT echo [SUCCESS] 目标脚本: "!HANDLER_SCRIPT!"
endlocal & exit /b 0

:register_failed
echo [ERROR] m3u8dl:// 协议注册失败。
endlocal & exit /b 1

:resolve_handler_exe
if exist "%~1" (
    for %%I in ("%~1") do set "HANDLER_EXE=%%~fI"
)
exit /b 0

:resolve_handler_script
if exist "%~1" (
    for %%I in ("%~1") do set "HANDLER_SCRIPT=%%~fI"
)
exit /b 0

:resolve_icon_value
call :set_icon_if_exists "%INSTALL_DIR%\resources\icons\mvs.ico"
if not defined DEFAULT_ICON_VALUE call :set_icon_if_exists "%INSTALL_DIR%\resources\mvs.ico"
if not defined DEFAULT_ICON_VALUE if defined HANDLER_SCRIPT set "DEFAULT_ICON_VALUE=\"%HANDLER_SCRIPT%\",0"
exit /b 0

:set_icon_if_exists
if exist "%~1" (
    for %%I in ("%~1") do set "DEFAULT_ICON_VALUE=\"%%~fI\""
)
exit /b 0

:resolve_python_executable
if defined M3U8D_PYTHON if exist "%M3U8D_PYTHON%" (
    set "PYTHON_EXE=%M3U8D_PYTHON%"
    exit /b 0
)

call :set_python_candidate "%INSTALL_DIR%\.venv\Scripts\pythonw.exe"
if defined PYTHON_EXE exit /b 0
call :set_python_candidate "%INSTALL_DIR%\.venv\Scripts\python.exe"
if defined PYTHON_EXE exit /b 0

for /f "delims=" %%I in ('where pythonw 2^>nul') do (
    set "PYTHON_EXE=%%~fI"
    exit /b 0
)

for /f "delims=" %%I in ('where python 2^>nul') do (
    set "PYTHON_EXE=%%~fI"
    exit /b 0
)

for /f "delims=" %%I in ('where py 2^>nul') do (
    set "PYTHON_EXE=%%~fI"
    exit /b 0
)

exit /b 0

:set_python_candidate
if exist "%~1" (
    for %%I in ("%~1") do set "PYTHON_EXE=%%~fI"
)
exit /b 0
