# M3U8D v0.4.1

<p align="leftr">
  <a href="README.md">English</a> | <b>简体中文</b>
</p>

> 面向 Windows 桌面环境的流媒体资源嗅探、解析与下载工具。通过真实浏览器会话捕获媒体资源，分派到多个下载引擎，管理从发现到完成的完整生命周期。

<p align="center">
  <img src="images/download%20center.jpg" width="800" alt="主界面">
</p>

<p align="center">
  <img src="images/brower%20workbench.jpg" width="400" alt="浏览器工作台" style="display: inline-block; margin-right: 10px;">
  <img src="images/resource%20list.jpg" width="400" alt="资源列表" style="display: inline-block;">
</p>

## 项目简介

M3U8D 是一个基于 Python + PyQt6 的 Windows 桌面应用，将以下任务整合到同一个工作流中：

- 在持久化浏览器会话中打开真实网页，保留登录态与 Cookie
- 播放过程中自动发现 m3u8 / mpd / mp4 / webm / 磁力链接等候选资源
- 在资源列表中筛选、选择清晰度、指定引擎
- 用多种下载引擎执行任务，管理队列、重试与历史
- 通过本地 HTTP 服务与 `m3u8dl://` 协议接收外部浏览器扩展或脚本送入的资源

## 主要功能

### 浏览器工作台与资源嗅探

- 通过 Playwright 启动真实持久化 Chrome（不是嵌入式网页控件）
- 跨会话保留登录态、Cookie 与扩展
- 四条发现路径：页面 URL 模式匹配、请求拦截、响应 Content-Type 检测、前端注入脚本回传
- 捕获窗口机制：抓取延迟出现 / 动态注入的媒体链接
- 启动时追加 `--disable-blink-features=AutomationControlled` 降低站点识别概率

### 资源列表与筛选

- 多层去重（URL、视频 ID、itag、标题、变体）
- 按标题 / URL / 来源文本搜索
- 按类型（M3U8 / MPD / MP4 / FLV / MKV / WEBM / TS）、来源域名、清晰度筛选
- M3U8 主播放列表自动解析与变体展开
- 批量下载、批量移除、清空列表

### 多引擎下载

五个下载引擎协同工作：

| 引擎 | 适合场景 |
|------|----------|
| N_m3u8DL-RE | m3u8 / mpd / HLS / DASH，精确选清晰度 |
| yt-dlp | YouTube / B站 / TikTok / Instagram / Twitter / Vimeo 等页面型站点 |
| Streamlink | 直播流（Twitch / 斗鱼 / 虎牙 / B站直播） |
| Aria2 | 直链文件与磁力链接 |
| FFmpeg | 后处理（转封装、合并、字幕提取） |

引擎选择优先级：用户指定 → 扩展名判定 → MIME 探测 → 直播平台名单 → yt-dlp 兜底。规则外置在 `resources/engine_rules.json`，可直接编辑。

### 下载管理

- 幂等入队：重复请求自动合并，不堆积重复任务
- 磁盘空间预检（≥ 预估大小 × 1.2）
- 并发 worker 池，支持动态调整（缩减时软退出）
- HLS 预探测（key URL + 首片 segment 可达性）
- 重试退避、引擎回退链、鉴权优先重试
- 停止响应 2 秒内（500ms 读循环退出 + 1.5s 递归 kill）
- 明确反馈：`queued` / `merged` / `needs_confirmation` / `failed`

### 组件管理

- 管理全部五个引擎：查看本地版本、检查远端更新、一键更新
- 三层 sha256 校验：静态固定值、动态 sidecar（`sha256_url`）、首次信任 TOFU（`~/.m3u8d/component_pins.json`）
- staging 目录隔离 → 安装前 sha256 复核 → 原子替换 + `.bak` 回滚
- 安装后版本复核（宽松前缀匹配）
- 大文件按 2% 粒度回传进度（FFmpeg ~130 MB 不会"看起来卡住"）
- 不会在后台静默安装；启动时只读检查，更新需用户确认

### 外部联动

**CatCatch 本地 HTTP 服务：**
- 强制绑定 `127.0.0.1:9527`（回退 9528–9539）
- 会话令牌认证（`X-Session-Token` + Origin 白名单）
- SSRF 过滤：拒绝私网 / 回环 / 链路本地 / 云元数据地址
- 请求体上限 64 KiB（超限返回 413）
- `GET /download` 已关闭（405）；下载统一走认证的 `POST /download`
- 外部传入的 `_` 前缀 headers 会被整列丢弃

**协议处理器（`m3u8dl://`）：**
- 读取 `~/.m3u8d/session.token`，向已运行实例发起认证 POST 握手
- 仅在无实例响应时才启动新进程
- 日志脱敏：令牌与敏感查询参数不以明文落盘

### 安全与隐私

- 所有引擎命令行使用参数化数组传递（不做字符串拼接）
- 转发到引擎的 headers 走白名单：仅 Referer / User-Agent / Origin / Cookie / Accept-Language
- yt-dlp `format_id` 字符集校验：`[A-Za-z0-9_.+:\-]+`，拒绝 shell 元字符
- 下载历史（`history.json`）写入前剥离 Cookie / Authorization / X-Session-Token 等敏感字段
- 日志脱敏：28 个敏感查询参数模式（OAuth / AWS / GCS / CloudFront / Azure 令牌）
- 调试敏感日志（`SECURITY_DEBUG=1`）独立隔离，默认关闭

## 运行环境

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows 10/11 64 位 |
| Python | 3.9+ |
| GUI | PyQt6 + PyQt6-WebEngine |
| 浏览器 | 系统已安装 Google Chrome |
| 网络 | 可访问 GitHub 与常见资源站点 |
| 磁盘空间 | 至少 500 MB，建议 2 GB 以上 |

**重要提示：** 内置浏览器依赖系统安装的 Chrome。仅安装 `playwright install chromium` 不能替代。

## 快速开始

### 1. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

### 2. 准备浏览器与外部工具

- 确认系统已安装 Google Chrome
- 确认 `bin/` 目录中存在必须引擎，或运行 `scripts/download_tools.bat` 下载

### 3. 启动程序

```bash
python mvs.pyw
```

或使用较轻量的开发入口：

```bash
python main.py
```

### 4.（可选）注册协议处理器

```bash
scripts\register_protocol.bat
```

注册后，浏览器扩展（如猫爪 CatCatch）发送的 `m3u8dl://` 链接可被 M3U8D 接收。

## 命令行参数

| 参数 | 说明 |
|------|------|
| `--url` | 视频或页面 URL（仅 http/https，最长 4096 字符，经 SSRF 过滤） |
| `--headers` | JSON 格式请求头（仅白名单字段生效） |
| `--filename` | 默认文件名（自动清洗 Windows 保留名，路径上限 240 字节） |

## 依赖说明

### Python 依赖

见 `requirements.txt`：
- PyQt6 ≥ 6.6.0
- PyQt6-WebEngine ≥ 6.6.0
- plyer ≥ 2.1.0
- requests ≥ 2.31.0
- playwright ≥ 1.40.0

### 外部引擎

声明在 `deps.json`：
- **必须：** yt-dlp、N_m3u8DL-RE、FFmpeg
- **建议：** aria2c、Streamlink
- **可选：** Deno

## 安装与打包

| 步骤 | 工具 | 入口 |
|------|------|------|
| 构建 | PyInstaller | `build_pyinstaller.py` / `build_pyinstaller.bat` |
| 安装器 | Inno Setup | `installer/M3U8D.iss` |
| 输出 | | `installer/output/M3U8D-Setup v0.4.1.exe` |

安装器支持：
- 安装主程序与协议处理器
- 安装后按需下载必须 / 建议引擎
- 安装后按需注册 `m3u8dl://` 协议

## 目录结构

```text
M3U8D/
├── mvs.pyw                    # 推荐入口（与打包对齐）
├── main.py                    # 开发入口
├── protocol_handler.pyw       # m3u8dl:// 协议处理器
├── config.json                # 配置文件
├── deps.json                  # 外部依赖清单
├── core/                      # 嗅探、下载管理、组件更新
│   └── download/              # 模块化下载管理器（队列、worker、分类器）
├── engines/                   # 下载引擎适配层
├── ui/                        # PyQt6 图形界面
├── utils/                     # 日志、国际化、配置、脱敏、路径清洗
├── resources/                 # 图标、手册、engine_rules.json
├── scripts/                   # 协议注册、依赖下载
├── tests/                     # 自动化测试
├── installer/                 # Inno Setup 安装器
├── bin/                       # 外部引擎二进制
├── logs/                      # 运行日志（自动轮转）
└── cookies/                   # 按域名存储的 Cookie 文件
```

## 常见问题

### 浏览器启动了但部分视频无法播放

Playwright 驱动的 Chrome 与日常浏览器的启动参数不同，常见原因：
- Widevine DRM CDM 未加载（在内置浏览器地址栏输入 `chrome://components` 查看）
- GPU / 硬件解码在自动化模式下受限
- 部分反爬系统识别到自动化环境

**建议：** 用系统浏览器 + 猫爪插件捕获资源 URL，再通过协议处理器或 HTTP API 让 M3U8D 下载。

### 协议处理器每次都启动新实例

- 确认已执行 `scripts\register_protocol.bat`
- 检查 `~/.m3u8d/session.token` 是否存在且可读
- 确认端口 9527–9539 未被防火墙拦截

### 组件更新看起来卡住了

大组件（FFmpeg ~130 MB）需要几分钟。界面会按 2% 粒度更新进度。单次 HTTPS 请求超时为 10 分钟。如果确实长时间无进度（网速为 0），可在组件管理里点"重试"。

## 合规与开源提示

请阅读 [`OPEN_SOURCE_NOTICE.md`](OPEN_SOURCE_NOTICE.md)。

- 本项目用于技术研究、学习与合法合规的个人使用
- 项目开源不代表对目标网站内容拥有授权
- 使用者应自行确认目标网站条款、版权边界与当地法律要求
- 第三方下载引擎具有各自独立许可证
- 严禁用于侵权、批量滥用、绕过限制或其他违法用途

## 相关文档

- [INSTALL.md](INSTALL.md) — 安装与环境准备
- [CHANGELOG_v0.4.1.md](CHANGELOG_v0.4.1.md) — 从 v0.3.1 起的详细更新日志
- [resources/manual_zh.md](resources/manual_zh.md) — 详细中文使用手册
- [resources/manual_en.md](resources/manual_en.md) — 详细英文使用手册

## License

见 [LICENSE](LICENSE)。分发二进制或安装包前，请核对所有捆绑的第三方工具的许可证。
