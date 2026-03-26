# M3U8 Video Sniffer 卸载指南

如果您决定不再使用本程序，请按照以下步骤彻底清除所有相关文件和配置。

## 1. 清除协议关联 (重要)

如果此前运行过 `scripts\register_protocol.bat` 注册了 `m3u8dl://` 协议，请先清除注册表项：

1.  双击运行程序目录下的 **`scripts\uninstall_protocol.bat`**。
2.  看到 `[成功] 协议关联已清除！` 提示即可。
3.  按任意键退出。

> **说明**：这一步会删除注册表中的 `HKEY_CURRENT_USER\Software\Classes\m3u8dl` 项，断开与猫爪扩展的关联。

## 2. 删除程序文件

直接删除整个 `M3U8VideoSniffer` 文件夹即可。

## 3. 清除用户数据 (可选)

程序运行过程中会产生一些缓存和配置文件，您可以手动删除它们以释放空间：

### 浏览器数据
Playwright/Chromium 产生的用户数据（Cookie、缓存等）：
*   **路径**：`%APPDATA%\Roaming\M3U8VideoSniffer`
*   **操作**：按 `Win + R`，输入 `%APPDATA%`，找到并删除 `M3U8VideoSniffer` 文件夹。

### 历史记录备份
程序保存的下载历史记录备份：
*   **路径**：`%USERPROFILE%\.m3u8sniffer`
*   **操作**：进入 `C:\Users\您的用户名`，删除 `.m3u8sniffer` 文件夹（如果是隐藏的，请在查看中开启显示隐藏项目）。

### 临时文件
下载过程中产生的临时分片文件：
*   **路径**：`%TEMP%\M3U8Sniffer`
*   **操作**：按 `Win + R`，输入 `%TEMP%`，找到并删除 `M3U8Sniffer` 文件夹。

---

完成以上步骤后，软件即被彻底卸载。感谢您的使用！
