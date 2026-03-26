@echo off
setlocal EnableExtensions
chcp 65001 >nul

echo ============================================
echo   M3U8D 协议注册工具
echo ============================================
echo.

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%") do set "SCRIPT_DIR=%%~fI"
for %%I in ("%SCRIPT_DIR%..") do set "INSTALL_DIR=%%~fI"
set "HANDLER_EXE="

rem 统一约定：安装器与 dist 产物均使用 {root}\protocol_handler\protocol_handler.exe
call :resolve_handler "%INSTALL_DIR%\protocol_handler\protocol_handler.exe"
if not defined HANDLER_EXE call :resolve_handler "%INSTALL_DIR%\protocol_handler.exe"
if not defined HANDLER_EXE call :resolve_handler "%SCRIPT_DIR%protocol_handler.exe"

if not defined HANDLER_EXE (
    echo [ERROR] 未找到打包后的协议处理器: protocol_handler.exe
    echo [ERROR] 约定优先路径:
    echo         "%INSTALL_DIR%\protocol_handler\protocol_handler.exe"
    echo [ERROR] 兼容回退路径:
    echo         "%INSTALL_DIR%\protocol_handler.exe"
    echo         "%SCRIPT_DIR%protocol_handler.exe"
    endlocal & exit /b 1
)

set "COMMAND_VALUE=\"%HANDLER_EXE%\" \"%%1\""

echo [INFO] Install Root: "%INSTALL_DIR%"
echo [INFO] Handler: "%HANDLER_EXE%"
echo [INFO] 正在注册 m3u8dl:// 协议...
echo.

reg add "HKCU\Software\Classes\m3u8dl" /ve /d "URL:M3U8DL Protocol" /f >nul
if errorlevel 1 goto :register_failed

reg add "HKCU\Software\Classes\m3u8dl" /v "URL Protocol" /d "" /f >nul
if errorlevel 1 goto :register_failed

reg add "HKCU\Software\Classes\m3u8dl\DefaultIcon" /ve /d "\"%HANDLER_EXE%\",0" /f >nul
if errorlevel 1 goto :register_failed

reg add "HKCU\Software\Classes\m3u8dl\shell" /f >nul
if errorlevel 1 goto :register_failed

reg add "HKCU\Software\Classes\m3u8dl\shell\open" /f >nul
if errorlevel 1 goto :register_failed

reg add "HKCU\Software\Classes\m3u8dl\shell\open\command" /ve /d "%COMMAND_VALUE%" /f >nul
if errorlevel 1 goto :register_failed

echo [SUCCESS] m3u8dl:// 协议已注册。
echo [SUCCESS] 目标程序: "%HANDLER_EXE%"
endlocal & exit /b 0

:register_failed
echo [ERROR] m3u8dl:// 协议注册失败。
endlocal & exit /b 1

:resolve_handler
if exist "%~1" (
    for %%I in ("%~1") do set "HANDLER_EXE=%%~fI"
)
exit /b 0
