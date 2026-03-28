# m3u8 video sniffer v0.2.0

> 面向 Windows 桌面环境的流媒体资源嗅探、解析、下载与安装分发工具。当前版本已形成较完整的“内置浏览器工作台 → 资源捕获 → 下载队列 → 安装包发布 → 协议联动”链路，并已提供带安装界面的安装包方案。

## 项目简介

M3U8D 是一个基于 Python + PyQt6 的 Windows 桌面应用，目标是把“打开页面、保留登录态、捕获媒体请求、选择下载引擎、管理下载任务、通过安装包分发给最终用户”整合到同一个工作流中。

结合当前 0.2.0 代码，项目已经覆盖以下核心能力：

- 通过 [`Playwright`](requirements.txt) 启动真实持久化浏览器工作流，而不是只靠简化网页控件
- 捕获 `m3u8`、`mpd`、`mp4`、`webm` 等多种候选媒体资源
- 通过多下载引擎自动选择 / 手动优先策略完成任务派发
- 提供下载队列、运行日志、历史记录、失败重试与基础指标追踪
- 支持通过本地 HTTP 服务与 `m3u8dl://` 协议接收外部浏览器或扩展送入的资源
- 提供现成的 [`PyInstaller`](build_pyinstaller.py) + [`Inno Setup`](installer/M3U8D.iss) 安装包构建链路

当前仓库中，桌面打包入口使用 [`mvs.pyw`](mvs.pyw:32)，源码入口仍保留 [`main.py`](main.py:36)，主界面核心位于 [`ui/main_window.py`](ui/main_window.py:31)。

## 0.2.0 版本状态

当前版本号已经更新为 `0.2.0`，安装器版本定义位于 [`installer/M3U8D.iss`](installer/M3U8D.iss:2)。

从最新代码看，0.2.0 的实际特征包括：

- 启动时可检查必须依赖是否缺失，并在需要时触发下载确认与安装流程，见 [`mvs.pyw`](mvs.pyw:60)
- 主窗口集成浏览器工作台、资源列表、下载中心三大主标签页，见 [`ui/main_window.py`](ui/main_window.py:146)
- 支持 `yt-dlp`、`N_m3u8DL-RE`、`Streamlink`、`Aria2` 与 `FFmpeg` 组合工作，见 [`ui/main_window.py`](ui/main_window.py:64)
- 本地 CatCatch 服务默认监听 `9527`，若端口被占用会自动尝试 `9528-9539`，见 [`core/catcatch_server.py`](core/catcatch_server.py:181)
- 协议处理器支持 JSON、命令行风格文本与纯链接格式的 `m3u8dl://` 数据，见 [`protocol_handler.pyw`](protocol_handler.pyw:99)
- 安装器在安装完成后可选下载必须依赖、建议依赖，并可注册 `m3u8dl://` 协议，见 [`installer/M3U8D.iss`](installer/M3U8D.iss:68)

## 主要功能

### 1. 浏览器工作台与资源嗅探

M3U8D 当前的浏览器链路主要围绕 [`PlaywrightDriver`](core/playwright_driver.py) 与 [`BrowserView`](ui/browser_view.py:27) 实现：

- 支持启动真实浏览器并保留登录态 / Cookie
- 地址栏输入页面后可直接导航并开始捕获媒体请求，见 [`ui/main_window.py`](ui/main_window.py:195)
- 浏览器未就绪时会缓存待跳转 URL，待浏览器启动后继续导航，见 [`ui/browser_view.py`](ui/browser_view.py:164)
- 捕获到的资源会统一进入嗅探器并同步到资源列表，见 [`ui/browser_view.py`](ui/browser_view.py:185)
- 程序启动时会追加 `--disable-blink-features=AutomationControlled` 以降低站点识别自动化环境的概率，见 [`main.py`](main.py:28) 与 [`mvs.pyw`](mvs.pyw:37)

需要说明的是，当前“新建标签”可触发新页面导航，但 [`BrowserView.back()`](ui/browser_view.py:249)、[`BrowserView.forward()`](ui/browser_view.py:250)、[`BrowserView.reload()`](ui/browser_view.py:251) 仍是占位实现，因此 README 不再把后退 / 前进 / 刷新描述为完整功能。

### 2. 资源列表与筛选

资源列表由 [`ResourcePanel`](ui/resource_panel.py:14) 管理，当前已具备：

- 自动去重与候选资源收集，见 [`ui/resource_panel.py`](ui/resource_panel.py:249)
- 搜索、类型筛选、来源筛选、清晰度筛选，见 [`ui/resource_panel.py`](ui/resource_panel.py:111)
- 批量下载、批量移除、清空列表等操作，见 [`ui/resource_panel.py`](ui/resource_panel.py:87)
- 资源表格展示文件名、类型、清晰度、来源、建议引擎、检测时间与下载动作，见 [`ui/resource_panel.py`](ui/resource_panel.py:141)

这意味着当前程序已适合“网页播放后从多个候选流中手动筛选可下载条目”的使用场景。

### 3. 多下载引擎协同

主窗口在初始化时按配置检查并加载外部引擎，见 [`ui/main_window.py`](ui/main_window.py:64)。

当前引擎组合包括：

- `N_m3u8DL-RE`
- `yt-dlp`
- `Streamlink`
- `Aria2`
- `FFmpeg`（后处理）

依赖默认声明位于 [`deps.json`](deps.json:1)，其中：

- 必须依赖：`yt-dlp`、`N_m3u8DL-RE`、`FFmpeg`
- 建议依赖：`aria2c`、`Streamlink`
- 可选依赖：`Deno`

任务进入下载队列后，下载管理器会基于 URL 与用户偏好选择引擎，见 [`DownloadManager.add_task()`](core/download_manager.py:48) 与 [`EngineSelector`](core/engine_selector.py)。

### 4. 下载队列、日志与历史

下载中心核心由 [`DownloadQueuePanel`](ui/download_queue.py:20) 与 [`DownloadManager`](core/download_manager.py:24) 组成，当前提供：

- 等待 / 下载中 / 暂停 / 失败 / 完成等状态视图
- 暂停、恢复、停止、删除、重试、打开目录等任务操作，见 [`ui/download_queue.py`](ui/download_queue.py:90)
- 并发 worker 下载模型，见 [`DownloadManager._start_workers()`](core/download_manager.py:171)
- 错误分类、失败阶段粗分、重试与状态更新能力，见 [`core/download_manager.py`](core/download_manager.py:201)
- 运行日志、下载历史、任务队列联动展示，主界面装配见 [`ui/main_window.py`](ui/main_window.py:244)

### 5. 外部联动：CatCatch + 协议处理器

项目当前支持两类外部接入：

1. 本地 HTTP 服务
2. `m3u8dl://` 协议处理器

其中：

- 本地 API 服务实现见 [`CatCatchServer`](core/catcatch_server.py:162)
- HTTP 入口包括 `/download` 与 `/status`，见 [`core/catcatch_server.py`](core/catcatch_server.py:48)
- 协议处理器实现见 [`protocol_handler.pyw`](protocol_handler.pyw:1)
- 协议脚本注册入口见 [`scripts/register_protocol.bat`](scripts/register_protocol.bat)
- 卸载协议注册见 [`scripts/uninstall_protocol.bat`](scripts/uninstall_protocol.bat)

这使得浏览器扩展、外部脚本或其他工具可以把已解析链接直接投递到 M3U8D。

### 6. 安装包分发链路

0.2.0 版本已经沿用现有安装分发方案：

- PyInstaller 构建入口：[`build_pyinstaller.py`](build_pyinstaller.py)
- 批处理入口：[`build_pyinstaller.bat`](build_pyinstaller.bat)
- 安装器脚本：[`installer/M3U8D.iss`](installer/M3U8D.iss)
- 最终安装包输出：[`installer/output/M3U8D-Setup.exe`](installer/output/M3U8D-Setup.exe)

当前安装器不是“4MB 左右的裸 exe”，而是带安装界面的完整安装包方案，支持：

- 安装主程序 one-dir 产物
- 安装协议处理器 one-dir 产物
- 安装后按需下载必须依赖 / 建议依赖
- 安装后按需注册 `m3u8dl://` 协议

## 典型工作流

### 工作流 A：在程序内打开网页并捕获资源

1. 启动程序
2. 进入浏览器工作台
3. 点击启动浏览器
4. 在地址栏输入页面地址
5. 触发视频播放、切换清晰度或完成登录
6. 在资源列表中筛选捕获到的候选媒体
7. 选择下载条目并加入下载队列
8. 在下载中心观察进度、日志与结果

适合：

- 真实媒体地址只有在页面运行后才出现
- 资源依赖登录态 / Cookie
- 需要手动挑选清晰度、码率、来源或协议

### 工作流 B：通过 CatCatch 或外部脚本送入链接

1. 外部工具将 URL、headers、filename 发送给本地 HTTP 服务
2. 或通过 `m3u8dl://` 协议唤起 M3U8D
3. 程序将资源加入资源列表
4. 用户继续确认并下载

命令行 / 协议传入参数处理可参考 [`mvs.pyw`](mvs.pyw:97)、[`main.py`](main.py:63) 与 [`protocol_handler.pyw`](protocol_handler.pyw:175)。

### 工作流 C：直接粘贴已知媒体地址

如果已经获得 `m3u8` / `mpd` / `mp4` 直链，也可以直接送入程序并依靠引擎选择逻辑完成下载。

## 运行环境

当前项目的推荐环境如下：

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10/11 64 位 |
| Python | 3.9+ |
| GUI | `PyQt6` + `PyQt6-WebEngine` |
| 浏览器 | 建议系统已安装 Google Chrome |
| 网络 | 建议可访问 GitHub 与常见资源站点 |
| 磁盘空间 | 至少 500MB，建议 2GB 以上 |

特别说明：

- 内置浏览器工作流明显依赖系统 Chrome，详细说明可见 [`resources/manual_zh.md`](resources/manual_zh.md)
- 仅安装 `playwright install chromium` 并不等同于具备完整的系统 Chrome 工作环境
- 若缺少关键下载引擎，程序可能仍能启动，但下载能力会明显下降

## 依赖说明

### Python 依赖

核心 Python 依赖见 [`requirements.txt`](requirements.txt)：

- `PyQt6>=6.6.0`
- `PyQt6-WebEngine>=6.6.0`
- `plyer>=2.1.0`
- `requests>=2.31.0`
- `playwright>=1.40.0`

### 外部二进制依赖

当前依赖清单由 [`deps.json`](deps.json:1) 驱动，并可由 [`scripts/download_dependencies.py`](scripts/download_dependencies.py) 与 [`scripts/download_tools.bat`](scripts/download_tools.bat:1) 下载。

默认分类如下：

- 必须依赖：`yt-dlp`、`N_m3u8DL-RE`、`FFmpeg`
- 建议依赖：`aria2c`、`Streamlink`
- 可选依赖：`Deno`

在安装包场景下，安装器会调用 [`download_tools.bat`](scripts/download_tools.bat:1) 触发依赖下载，见 [`installer/M3U8D.iss`](installer/M3U8D.iss:68)。

## 快速开始

### 1. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

### 2. 准备浏览器与外部工具

- 确认系统已安装 Google Chrome
- 确认 [`bin/`](bin) 目录中存在必须依赖，或运行 [`scripts/download_tools.bat`](scripts/download_tools.bat) 下载

### 3. 启动程序

推荐使用当前打包入口一致的源码启动方式：

```bash
python mvs.pyw
```

如果需要使用较轻量的源码入口，也可执行：

```bash
python main.py
```

其中：

- [`mvs.pyw`](mvs.pyw:32) 包含运行目录初始化、缺失必须依赖检查与安装提示
- [`main.py`](main.py:36) 是当前仍保留的源码入口，适合开发或调试

## 命令行参数

当前入口支持以下参数：

- `--url`
- `--headers`
- `--filename`

参数解析可见 [`mvs.pyw`](mvs.pyw:23) 与 [`main.py`](main.py:19)。典型用途：

- 协议处理器回传链接
- 外部脚本注入资源
- 调试时直接向 GUI 塞入目标媒体地址

## 安装与打包

### 源码运行

- 启动入口：[`mvs.pyw`](mvs.pyw)
- 备用入口：[`main.py`](main.py)

### PyInstaller 打包

- 构建脚本：[`build_pyinstaller.py`](build_pyinstaller.py)
- 批处理入口：[`build_pyinstaller.bat`](build_pyinstaller.bat)
- spec 输出目录：[`build/pyinstaller/spec/`](build/pyinstaller/spec)

### Inno Setup 安装包

- 安装器脚本：[`installer/M3U8D.iss`](installer/M3U8D.iss)
- 安装包输出目录：[`installer/output/`](installer/output)
- 当前安装包：[`installer/output/M3U8D-Setup.exe`](installer/output/M3U8D-Setup.exe)

## 目录结构概览

```text
M3U8D/
├── mvs.pyw                    # 当前打包入口 / 推荐源码入口
├── main.py                    # 备用源码入口
├── protocol_handler.pyw       # m3u8dl:// 协议处理器
├── config.json                # 配置文件
├── deps.json                  # 外部依赖清单
├── core/                      # 嗅探、依赖检查、下载管理、任务模型
├── engines/                   # 下载引擎适配层
├── ui/                        # PyQt6 图形界面
├── utils/                     # 日志、国际化、配置等基础模块
├── resources/                 # 图标、内置手册、界面资源
├── scripts/                   # 协议注册、依赖下载、测试脚本
├── tests/                     # 自动化测试
├── installer/                 # Inno Setup 安装器与输出目录
├── build/                     # PyInstaller 中间产物
├── logs/                      # 运行日志
├── cookies/                   # Cookie 文件
└── plans/                     # 计划、报告与设计记录
```

如果希望快速定位代码，可从以下位置开始：

- 主窗口：[`MainWindow`](ui/main_window.py:31)
- 浏览器控制：[`BrowserView`](ui/browser_view.py:27)
- 下载管理：[`DownloadManager`](core/download_manager.py:24)
- 协议联动：[`protocol_handler.pyw`](protocol_handler.pyw)
- 本地 HTTP 接入：[`CatCatchServer`](core/catcatch_server.py:162)

## 常见问题

### 1. 程序能启动，但浏览器工作台效果不稳定

优先检查：

- 是否已安装可正常启动的 Chrome
- 当前站点是否需要登录后才能暴露真实媒体地址
- 是否实际触发了播放、切换清晰度或其他网络请求
- 关键下载引擎是否已正确安装

### 2. 安装后为什么还会提示下载依赖

因为安装包当前分发的是主程序与运行时结构，外部下载引擎依赖通过安装完成后的下载步骤补齐，逻辑定义见 [`installer/M3U8D.iss`](installer/M3U8D.iss:68) 与 [`scripts/download_tools.bat`](scripts/download_tools.bat:10)。

### 3. 为什么推荐用 [`mvs.pyw`](mvs.pyw) 而不是只运行 [`main.py`](main.py)

因为最新打包入口与协议处理回传优先链路都更贴近 [`mvs.pyw`](mvs.pyw:32)，它还包含必须依赖检查、运行目录初始化与依赖安装提示。

### 4. 协议联动失败怎么办

优先检查：

- 是否执行过 [`scripts/register_protocol.bat`](scripts/register_protocol.bat)
- `protocol_handler` 是否已随安装正确部署
- 本地 CatCatch 服务是否已启动
- 端口 `9527-9539` 是否被防火墙或其他程序拦截

### 5. 当前是否已经支持完整安装包分发

支持。当前仓库已经包含完整的 [`PyInstaller`](build_pyinstaller.py) + [`Inno Setup`](installer/M3U8D.iss) 链路，并已生成带安装界面的安装包 [`installer/output/M3U8D-Setup.exe`](installer/output/M3U8D-Setup.exe)。

## 合规与开源提示

请务必阅读 [`OPEN_SOURCE_NOTICE.md`](OPEN_SOURCE_NOTICE.md)。

尤其需要注意：

- 本项目用于技术研究、学习、开发验证与合法合规的个人使用
- 项目开源不代表作者对目标网站内容拥有授权
- 使用者应自行确认目标网站条款、版权边界与当地法律要求
- 第三方下载引擎具有各自独立许可证与使用限制
- 严禁将项目用于侵权、批量滥用接口、绕过限制或其他违法违规用途

## 相关文档

- [`INSTALL.md`](INSTALL.md)：安装、环境准备与排查
- [`MANUAL.md`](MANUAL.md)：中文使用手册
- [`resources/manual_zh.md`](resources/manual_zh.md)：更细的中文版内置手册源文档
- [`resources/manual_en.md`](resources/manual_en.md)：更细的英文版内置手册源文档
- [`UNINSTALL.md`](UNINSTALL.md)：卸载说明
- [`OPEN_SOURCE_NOTICE.md`](OPEN_SOURCE_NOTICE.md)：开源边界与合规说明

---

## License

项目仓库当前附带 [`LICENSE`](LICENSE) 与 [`OPEN_SOURCE_NOTICE.md`](OPEN_SOURCE_NOTICE.md)。在公开分发二进制、安装包或 Release 时，请同时核对第三方依赖许可证与再分发要求。
