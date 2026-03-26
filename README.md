# M3U8D

> 面向 Windows 桌面环境的流媒体资源嗅探、解析与下载工具。项目以中文使用体验为主，提供内置浏览器工作台、资源捕获、任务队列、多下载引擎协同、协议联动与安装包分发能力。

## 项目简介

M3U8D 是一个以 Python + PyQt6 构建的桌面应用，核心目标是把“打开网页、播放资源、识别媒体地址、选择下载引擎、查看进度与结果”整合到同一个工作流中。

项目当前主要面向 [`Windows 10/11`](INSTALL.md) 使用场景，围绕以下方向进行设计：

- 基于 [`Playwright`](requirements.txt) 与系统浏览器环境进行页面访问与网络请求捕获
- 对 `m3u8`、`mpd`、直链媒体资源进行统一识别与任务化管理
- 集成多个外部下载引擎，以提升不同站点、不同协议、不同媒体类型下的兼容性
- 提供适合日常使用的 GUI、安装包、协议注册脚本与辅助文档

从仓库结构看，主程序入口为 [`main.py`](main.py)，桌面启动形态还包括 [`mvs.pyw`](mvs.pyw)，下载、嗅探、依赖检查、引擎选择等能力分别拆分在 [`core/`](core) 、[`engines/`](engines) 与 [`ui/`](ui) 目录中。

## 主要功能

### 1. 内置浏览器与资源嗅探

- 内置网页访问工作台，可直接在程序内打开页面并观察资源请求
- 支持在页面播放过程中自动捕获 `m3u8`、`mpd`、`mp4` 等媒体地址
- 支持登录态复用与 Cookie 持久化，适合需要登录后才能获取真实资源地址的场景
- 可结合资源面板进行筛选、去重、批量处理与下载发起

### 2. 多下载引擎协同

项目内置多种下载引擎适配逻辑，典型工具包括：

- [`bin/N_m3u8DL-RE.exe`](bin/N_m3u8DL-RE.exe)：适合 HLS / DASH 资源下载
- [`bin/yt-dlp.exe`](bin/yt-dlp.exe)：适合大量视频网站与页面解析型下载
- [`bin/ffmpeg.exe`](bin/ffmpeg.exe)：用于合并、转封装与后处理
- [`bin/aria2c.exe`](bin/aria2c.exe)：适合普通直链多线程下载
- [`bin/streamlink.exe`](bin/streamlink.exe)：适合直播流类场景

程序会在自动选择与手动选择之间提供平衡：一般场景可交给自动选择，特殊场景也可按资源类型切换引擎。

### 3. 下载队列与历史管理

- 支持多任务并发下载与队列调度
- 支持下载状态展示、失败诊断与日志查看
- 支持历史记录管理，便于回溯已执行任务
- 支持资源列表搜索、过滤、批量下载与状态筛选

### 4. 外部联动与协议调用

- 支持通过自定义协议与浏览器扩展进行联动
- 可与猫爪（CatCatch）等浏览器工作流结合使用
- 支持从外部浏览器将已捕获资源一键发送到程序

## 界面 / 工作流说明

M3U8D 的典型使用流程可以概括为“打开页面 → 捕获资源 → 确认任务 → 下载与查看结果”。

### 工作流 A：内置浏览器嗅探下载

1. 启动程序，进入主界面
2. 在地址栏输入目标页面 URL
3. 使用内置浏览器访问页面并触发视频播放
4. 程序在资源面板中显示捕获到的媒体请求
5. 选中目标资源，确认文件名、引擎或下载参数
6. 将任务加入下载队列并观察进度
7. 下载完成后在历史记录或输出目录中查看结果

这一工作流适合以下场景：

- 真实媒体地址需要在页面运行后才出现
- 资源获取依赖登录态、Cookie 或页面脚本
- 需要从多个候选流中手动挑选清晰度、来源或协议

### 工作流 B：浏览器扩展 / 协议联动

1. 在外部浏览器中使用猫爪等扩展捕获资源
2. 通过 `m3u8dl://` 协议发送到本程序
3. 程序自动唤起或接收任务
4. 进入下载确认或直接加入队列

相关脚本与处理逻辑可参考 [`scripts/register_protocol.bat`](scripts/register_protocol.bat) 与 [`protocol_handler.pyw`](protocol_handler.pyw)。

### 工作流 C：手动粘贴 URL

如果你已经拿到了媒体地址，也可以绕过页面嗅探，直接粘贴链接后选择引擎下载。这种方式适合：

- 已知 `m3u8` / `mpd` / `mp4` 直链
- 从其他工具复制了目标资源地址
- 只希望快速验证某个下载引擎是否可用

## 运行环境

项目当前的推荐环境如下：

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10/11 64 位 |
| Python | 3.9 及以上 |
| GUI | `PyQt6` + `PyQt6-WebEngine` |
| 浏览器环境 | 系统已安装 Chrome，便于内置浏览器工作流使用 |
| 磁盘空间 | 至少 500MB，建议更高 |

补充说明：

- 项目目前明显偏向 Windows 桌面分发，多个脚本、协议注册与安装包流程也是围绕 Windows 构建
- 如缺少 Chrome 或相关下载引擎，程序并非一定无法启动，但内置浏览器、嗅探成功率、登录态复用与下载兼容性会明显下降
- 打包后产物与安装器也都位于 Windows 目录结构中，例如 [`installer/`](installer) 与 [`build/`](build)

## 依赖说明

### Python 依赖

[`requirements.txt`](requirements.txt) 当前列出的核心依赖包括：

- `PyQt6>=6.6.0`
- `PyQt6-WebEngine>=6.6.0`
- `plyer>=2.1.0`
- `requests>=2.31.0`
- `playwright>=1.40.0`

其中：

- `PyQt6` / `PyQt6-WebEngine` 负责桌面界面与 WebEngine 组件
- `playwright` 用于浏览器驱动与页面自动化辅助能力
- `requests` 用于常规网络请求与部分辅助逻辑
- `plyer` 用于桌面通知等跨平台封装能力

### 外部工具依赖

除了 Python 包外，项目还依赖若干外部二进制工具，默认放在 [`bin/`](bin) 目录中：

- [`bin/N_m3u8DL-RE.exe`](bin/N_m3u8DL-RE.exe)
- [`bin/yt-dlp.exe`](bin/yt-dlp.exe)
- [`bin/ffmpeg.exe`](bin/ffmpeg.exe)
- [`bin/aria2c.exe`](bin/aria2c.exe)
- [`bin/streamlink.exe`](bin/streamlink.exe)
- [`bin/deno.exe`](bin/deno.exe)（当前属于辅助/保留工具）

这些工具各自拥有独立的许可证与使用规则，发布、再分发或商用前应逐项核对其官方说明。

## 快速开始

如果你只是希望尽快运行项目，可按以下顺序完成：

### 1. 安装 Python 与依赖

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. 准备外部下载工具

确认 [`bin/`](bin) 目录下存在所需的可执行文件；如果需要，也可参考 [`scripts/download_tools.bat`](scripts/download_tools.bat) 与 [`scripts/download_dependencies.py`](scripts/download_dependencies.py) 的下载逻辑。

### 3. 启动程序

```bash
python main.py
```

首次使用前，建议同时阅读 [`INSTALL.md`](INSTALL.md) 与 [`MANUAL.md`](MANUAL.md)，前者偏安装与部署，后者偏功能与操作说明。

## 源码运行方式

### 方式 1：直接运行主入口

最常见的源码启动方式：

```bash
python main.py
```

对应入口文件为 [`main.py`](main.py)。

### 方式 2：使用窗口化入口

某些桌面使用习惯下，也可以从 [`mvs.pyw`](mvs.pyw) 启动窗口化入口。

### 方式 3：运行测试

仓库中已包含多个测试文件，位于 [`tests/`](tests) 目录，例如：

- [`tests/test_main_entry.py`](tests/test_main_entry.py)
- [`tests/test_protocol_handler.py`](tests/test_protocol_handler.py)
- [`tests/test_m3u8_parser.py`](tests/test_m3u8_parser.py)
- [`tests/test_download_manager_state_machine.py`](tests/test_download_manager_state_machine.py)

如本地已配置测试环境，可使用常见的 Python 测试命令执行对应测试集。

## 打包 / 安装包说明

项目已经包含从源码打包到安装器生成的相关文件，适合在发布 GitHub Release 或本地分发时使用。

### PyInstaller 打包

- 打包脚本：[`build_pyinstaller.py`](build_pyinstaller.py)
- 批处理入口：[`build_pyinstaller.bat`](build_pyinstaller.bat)
- Spec 文件：[`build/pyinstaller/spec/M3U8D.spec`](build/pyinstaller/spec/M3U8D.spec)、[`build/pyinstaller/spec/protocol_handler.spec`](build/pyinstaller/spec/protocol_handler.spec)

从目录结构看，PyInstaller 的输出与中间文件已位于 [`build/pyinstaller/`](build/pyinstaller) 下。

### 安装包构建

- 安装器脚本：[`installer/M3U8D.iss`](installer/M3U8D.iss)
- 已生成安装包目录：[`installer/output/`](installer/output)
- 现有安装包示例：[`installer/output/M3U8D-Setup.exe`](installer/output/M3U8D-Setup.exe)

这说明项目已经具备较完整的 Windows 安装分发链路：源码运行、PyInstaller 打包、Inno Setup 安装包生成。

## 目录结构概览

以下为仓库主要目录与用途概览：

```text
M3U8D/
├── main.py                     # 程序主入口
├── mvs.pyw                     # 窗口化入口
├── config.json                 # 默认配置
├── requirements.txt            # Python 依赖
├── core/                       # 核心逻辑：嗅探、下载、依赖检查、任务模型
├── engines/                    # 下载引擎适配层
├── ui/                         # PyQt6 界面层
├── bin/                        # 外部二进制工具
├── scripts/                    # 辅助脚本、协议注册、工具下载、测试脚本
├── tests/                      # 自动化测试
├── installer/                  # 安装器脚本与输出目录
├── build/                      # 构建中间产物与打包结果
├── cookies/                    # Cookie 文件存放位置
├── logs/                       # 运行日志
├── resources/                  # 图标与界面资源
└── plans/                      # 开发计划、阶段报告与设计记录
```

如果希望快速定位代码：

- 主窗口逻辑可从 [`ui/main_window.py`](ui/main_window.py) 开始
- 下载调度可从 [`core/download_manager.py`](core/download_manager.py) 开始
- 浏览器与嗅探链路可从 [`core/playwright_driver.py`](core/playwright_driver.py) 、[`core/m3u8_sniffer.py`](core/m3u8_sniffer.py) 与 [`ui/browser_view.py`](ui/browser_view.py) 开始
- 引擎策略可从 [`core/engine_selector.py`](core/engine_selector.py) 与 [`engines/`](engines) 开始

## 常见问题

### 1. 程序能启动，但嗅探效果不稳定

优先检查以下项目：

- 是否已安装可正常启动的 Chrome
- 当前站点是否需要登录后才会暴露真实媒体请求
- [`bin/`](bin) 中关键工具是否齐全
- 网络环境是否影响页面脚本、播放器或外部工具访问

### 2. 提示缺少下载引擎

请检查 [`bin/N_m3u8DL-RE.exe`](bin/N_m3u8DL-RE.exe)、[`bin/yt-dlp.exe`](bin/yt-dlp.exe)、[`bin/ffmpeg.exe`](bin/ffmpeg.exe) 等文件是否存在；缺失时很多下载流程会被降级或直接失败。

### 3. 为什么安装了 `playwright` 还建议安装 Chrome

根据现有文档与项目说明，内置浏览器工作流更依赖系统 Chrome 环境。只安装 Playwright 不一定能完整替代实际浏览器使用场景，尤其在登录态复用、页面兼容性与交互稳定性方面。

### 4. 更换目录后为什么需要重新检查配置

项目支持便携使用，但移动目录后，旧的 [`config.json`](config.json) 可能保留过时路径；如果你还使用协议联动，也需要重新运行 [`scripts/register_protocol.bat`](scripts/register_protocol.bat) 完成协议路径更新。

### 5. 项目是否只适合源码运行

不是。仓库已经包含打包与安装器相关产物，既可以源码运行，也可以通过安装包分发给最终用户。

## 开源与合规提示

本项目已新增 [`OPEN_SOURCE_NOTICE.md`](OPEN_SOURCE_NOTICE.md)，在公开发布到 GitHub 前后，建议将其与许可证、第三方依赖说明一起阅读。

请特别注意以下几点：

- 本项目的开源目的主要是技术学习、研究验证、开发参考与个人合法合规使用
- 项目开源不代表作者对目标网站内容、平台资源或第三方服务拥有任何授权
- 使用者需要自行确认目标网站条款、内容版权、当地法律与个人使用权限
- 仓库中涉及的第三方二进制工具具有各自独立的许可证与使用限制
- 严禁将本项目用于侵权、绕过限制、批量滥用接口或其他违法违规用途

如果准备在 GitHub 上公开发布，建议在仓库首页、Release、安装包说明与二进制分发页面保持一致的合规表述。

## 相关文档链接

仓库中已存在以下配套文档：

- [`INSTALL.md`](INSTALL.md)：安装、依赖准备、协议注册、环境排查
- [`MANUAL.md`](MANUAL.md)：功能使用教程、操作说明、工作流示例
- [`UNINSTALL.md`](UNINSTALL.md)：卸载与清理说明
- [`OPEN_SOURCE_NOTICE.md`](OPEN_SOURCE_NOTICE.md)：开源边界、第三方依赖、免责与合规说明

## 适合 GitHub 首页的阅读建议

如果你是首次访问仓库，建议按以下顺序阅读：

1. 先看本页，了解项目定位、能力边界与整体结构
2. 再看 [`INSTALL.md`](INSTALL.md)，确认运行环境与安装步骤
3. 然后看 [`MANUAL.md`](MANUAL.md)，熟悉界面与具体使用流程
4. 最后阅读 [`OPEN_SOURCE_NOTICE.md`](OPEN_SOURCE_NOTICE.md)，明确合规边界与责任分配

---

## License

本仓库当前在 [`README.md`](README.md) 里保留 MIT License 提示；若仓库后续新增正式许可证文件，建议在 GitHub 根目录补充标准许可证文本，并与 [`OPEN_SOURCE_NOTICE.md`](OPEN_SOURCE_NOTICE.md) 的说明保持一致。
