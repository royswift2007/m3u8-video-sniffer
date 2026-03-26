@echo off
echo 正在启动 Chrome 配置模式...
echo 请在此打开的 Chrome 窗口中安装所需的扩展插件。
echo 安装完成后，请关闭 Chrome 窗口，然后再重新运行 M3U8VideoSniffer 程序。

rem 设置用户数据目录 (于 python 代码中一致)
set "USER_DATA=%APPDATA%\M3U8VideoSniffer\chromium_user_data"

rem 尝试查找 Chrome 安装路径
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    set "CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe"
) else if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" (
    set "CHROME_PATH=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
) else (
    echo [错误] 未能找到 Google Chrome 安装路径，请确认已安装 Chrome。
    pause
    exit /b
)

echo 使用数据目录: "%USER_DATA%"
"%CHROME_PATH%" --user-data-dir="%USER_DATA%" --no-first-run

echo Chrome 已关闭。
pause
