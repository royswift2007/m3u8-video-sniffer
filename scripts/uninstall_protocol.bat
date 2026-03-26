@echo off
chcp 65001 >nul
echo ============================================
echo   M3U8VideoSniffer 协议卸载工具
echo ============================================
echo.

echo [WARN] This will remove the m3u8dl:// protocol association.
echo [WARN] Browser extensions will no longer launch this app directly.
echo.
pause

:: 创建注册表清理文件
set "REG_FILE=%TEMP%\m3u8dl_uninstall.reg"

echo Windows Registry Editor Version 5.00 > "%REG_FILE%"
echo. >> "%REG_FILE%"
echo [-HKEY_CURRENT_USER\Software\Classes\m3u8dl] >> "%REG_FILE%"

echo [信息] 正在删除 m3u8dl:// 协议关联...
echo.

:: 导入注册表（执行删除）
regedit /s "%REG_FILE%"

if %ERRORLEVEL% EQU 0 (
    echo [成功] 协议关联已清除！
) else (
    echo [错误] 清除失败，请尝试以管理员身份运行
)

:: 清理临时文件
del "%REG_FILE%" 2>nul
echo.
pause
