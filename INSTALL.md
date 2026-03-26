# M3U8 Video Sniffer 安装指南

## 📋 系统要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows 10/11（64位） |
| Python | 3.9 或更高版本 |
| Chrome 浏览器 | 已安装（程序会调用系统 Chrome） |
| 磁盘空间 | 至少 500MB（含工具） |

---

## 🚀 安装步骤

### 步骤 1：安装 Python

1. 访问 https://www.python.org/downloads/
2. 下载 Python 3.9 或更高版本
3. 运行安装程序，**务必勾选** `Add Python to PATH`
4. 完成安装

验证安装：
```bash
python --version
```

---

### 步骤 2：复制程序文件

将整个 `M3U8VideoSniffer` 文件夹复制到目标电脑，推荐位置：
- `C:\Users\用户名\Documents\M3U8VideoSniffer`
- `D:\M3U8VideoSniffer`

> ⚠️ **注意**：不建议放在 `C:\Program Files`（需要管理员权限）

---

### 步骤 3：安装 Python 依赖

打开命令提示符（CMD）或 PowerShell：

```bash
cd 你的程序目录\M3U8VideoSniffer

pip install -r requirements.txt

playwright install chromium
```

---

### 步骤 4：下载外部工具

将以下工具下载到 `bin/` 目录：

| 工具 | 下载地址 |
|------|----------|
| N_m3u8DL-RE.exe | https://github.com/nilaoda/N_m3u8DL-RE/releases |
| yt-dlp.exe | https://github.com/yt-dlp/yt-dlp/releases |
| ffmpeg.exe | https://github.com/BtbN/FFmpeg-Builds/releases |
| aria2c.exe | https://github.com/aria2/aria2/releases |
| streamlink.exe | https://github.com/streamlink/windows-installer/releases |

或运行自动下载脚本：
```bash
scripts\download_tools.bat
```

---

### 步骤 5：运行程序

```bash
python main.py
```

### 步骤 6：配置猫爪浏览器扩展 (可选)

如果您希望使用 Chrome/Edge 浏览器的"猫爪"扩展抓取资源并发送到本程序下载：

1. **注册协议处理程序**
   - 运行项目目录下的 `scripts\register_protocol.bat`
   - 如果提示 `[成功] m3u8dl:// 协议已注册！` 即表示成功

2. **配置猫爪扩展**
   - 在浏览器中打开猫爪扩展设置
   - 找到 "外部调用" 或 "自定义命令" 设置
   - 添加以下配置：
     - **协议名称**: `M3U8 Sniffer`
     - **调用命令**: `m3u8dl://`
     - **参数格式**: N_m3u8DL-RE 格式 (或默认格式)

Settings - URL Protocol m3u8dl
Enable m3u8dl:// Download m3u8 or mpd：N_m3u8DL-RE
Parameter Setting：
"${url}" --save-dir "%USERPROFILE%\Downloads\m3u8dl" --save-name "${title}_${now}" ${referer|exists:'-H "Referer:*"'} ${cookie|exists:'-H "Cookie:*"'} --no-log

3. **使用方法**
   - 在猫爪抓取到资源后，点击"发送到 M3U8 Sniffer"（或您设置的名称）
   - 程序会自动启动并添加下载任务

---

## 🚚 迁移与便携模式 (Portability)

本程序支持"绿色搬家"（直接移动文件夹），但移动后需要注意以下两点：

1.  **删除旧配置文件**：
    *   移动后，请删除程序目录下的 `config.json` 文件。
    *   *原因*：旧的配置文件可能包含旧路径，导致找不到下载内核。删除后重启程序会自动生成新的正确配置。

2.  **重新注册协议**（如果使用插件）：
    *   如果需要配合猫爪插件使用，请在新目录下重新运行 `scripts\register_protocol.bat`。
    *   *原因*：Windows 注册表中的 `m3u8dl://` 协议路径需要更新为新位置。

---

## ⚙️ 配置说明

程序使用**相对路径**，复制到其他电脑无需修改配置。

如需自定义，编辑 `config.json`：

```json
{
    "download_dir": "",           // 留空 = 系统下载文件夹
    "speed_limit": 0,             // 0 = 不限速，单位 MB/s
    "max_concurrent_downloads": 2 // 同时下载任务数
}
```

---

## 🔧 故障排除

| 问题 | 解决方案 |
|------|----------|
| pip 命令不可用 | 确认安装时勾选了 `Add Python to PATH` |
| playwright install 失败 | 设置镜像：`set PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright` |
| 未找到下载引擎 | 检查 `bin/` 目录是否有 exe 文件 |
| 浏览器无法启动 | 确保已安装 Chrome，运行 `playwright install chromium` |

---

## 📁 目录结构

```
M3U8VideoSniffer/
├── bin/                    # 外部工具（相对路径）
├── core/                   # 核心模块
├── engines/                # 下载引擎
├── ui/                     # 界面模块
├── config.json             # 配置文件
├── requirements.txt        # Python 依赖
└── main.py                 # 程序入口
```

---

*最后更新：2026-01-04*
