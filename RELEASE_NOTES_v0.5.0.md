# M3U8D v0.5.0 Release Notes

> **Release date:** 2026-07-31
> **Previous version:** v0.4.1
> **Scope:** 下载成功率优化 + 统一下载上下文架构 + 进度实时性修复 + FFmpeg 后处理

---

## 🚀 新功能

### 1. 统一下载上下文架构

新增 `core/download_context.py` 模块，引入 `EngineSelectContext` 结构体，将 `url` / `page_url` / `page_title` / `source` / `resource_type` / `mime` / `headers` / `master_url` / `media_url` / `metadata` 打包为单一对象，从资源嗅探到引擎选择到下载完成全链路贯通。

- 资源类型自动推断：根据 URL 扩展名和 MIME 映射到 `hls` / `dash` / `direct_video` / `segment` / `page` / `unknown`
- 来源标签标准化：`catcatch` / `internal_browser` / `manual` / `unknown`
- 引擎选择优先级链更新为：**用户偏好 → 上下文检测 → HEAD MIME 探测 → 扩展名回退 → 直播平台 → yt-dlp 兜底**
- HLS 检测从 `.m3u8 in url` 字符串匹配升级为 `resource_type` 精确判断

### 2. CDN 签名 / 临时 URL 诊断

新增 `core/media_url_ttl.py` 模块，检测 20+ 种 CDN 签名参数（AWS CloudFront / S3、Google Cloud Storage、Akamai、`expires` / `token` / `signature` / `hdntl` 等），返回风险等级（low / medium / high / expired）和剩余有效时间，辅助用户判断临时链接是否需要在过期前尽快下载。

### 3. 播放列表诊断

新增 `analyze_playlist_diagnostics()` 函数，分析 M3U8 播放列表中的：
- DRM 系统类型（Widevine / FairPlay / PlayReady）
- 跨域密钥 / 分段 URL
- 签名 URL 到期风险

诊断结果在 UI 中以警告提示呈现，帮助用户提前发现 DRM 加密或即将过期的链接。

### 4. FFmpeg 后处理

新增 `ui/main_window_postprocess.py` 模块，在已完成任务的右键菜单中提供 4 种 FFmpeg 后处理操作：

| 操作 | 说明 |
|---|---|
| 封装为 MP4 | 将视频转封装为 MP4 格式（不重编码，速度极快） |
| 压缩视频 | 降低码率压缩文件体积 |
| 提取字幕 | 从视频中提取字幕文件 |
| 合并音频 | 将外部音频文件与视频合并 |

全部在后台线程执行，不阻塞 UI。

### 5. 内置浏览器增强

- 新增 `resource_context_detected` 信号，携带完整资源上下文字典
- 新增 Cookie 域查找：自动从浏览器 Cookie 中按域名匹配资源所需 Cookie
- 新增浏览器导航操作：后退 / 前进 / 刷新 / 新标签页
- 新增 Cookie 导出至文件功能
- 新增临时 URL 签名参数检测与 Header 评分

### 6. 代理统一配置

新增 `utils/proxy_config.py` 模块，统一管理代理配置，支持 `http` / `https` / `socks4` / `socks5` / `socks5h` 协议。所有四个下载引擎和 HTTP 探测器均已接入。

### 7. 可调参数中心化

新增 `utils/tunables.py` 模块，将分散在各模块的 15 个硬编码常量（超时、大小阈值、性能参数）集中管理，支持通过 `config.json → tunables` 运行时覆盖。

### 8. 系统通知重构

新增 `utils/notification_service.py` 模块，用 `QSystemTrayIcon.showMessage` 替代 plyer 库，消除 Windows CMD 窗口闪烁，支持跨线程安全通知。

---

## 🔧 Bug 修复

### 关键修复

| # | 问题 | 修复 |
|---|---|---|
| **Aria2 扩展名丢失** | `-o` 参数收到裸标题（无扩展名），导致下载文件无后缀且无法合并 | 新增 `_resolve_output_filename()`，根据 URL 后缀 / MIME 自动推断并追加正确扩展名 |
| **Aria2 retry_count 误读** | Aria2 引擎错误读取了 N_m3u8DL-RE 的 `retry_count` 配置 | 修正为读取 `engines.aria2.retry_count`（ISS-24） |
| **N_m3u8DL-RE 0.6.x 进度卡 0%** | 进度信息停留在 0% 直到进程退出才一次性刷新 | 引擎输出读取从 `readline()` 重写为分块读取 + Windows `PeekNamedPipe` 无阻塞读取，同时以 `\r` 和 `\n` 分割缓冲区 |
| **进度显示不实时** | N_m3u8DL-RE 0.6.x 通过 `\r` 原地刷新进度而非 `\n` 换行，旧泵只切 `\n` 导致积压 | 新增管道空闲检测 + 无分隔符部分缓冲区强制刷新 + `bufsize=0` 无缓冲管道 I/O |

### 其他修复

- **ISS-28**：`site_rules` 和 `features` 配置在嗅探器运行期间修改后不生效 → 新增 `refresh_config()` 热更新
- **ISS-30**：进程终止时可能因 PID 重用杀错进程 → 全引擎 `task.process` / `_pid` / `_expected_engine_name` 原子化 `task.lock` 绑定，`kill_process_tree` 校验进程名
- **ISS-33**：N_m3u8DL-RE 安全模式重建最小命令丢失 Header / 节流参数 → 改为仅移除激进标志，保留所有跨模式选项
- **ISS-45**：协议处理器 `prune_runtime_logs` 每次日志都全目录扫描 → 5 分钟 / 50 次调用节流
- **F-03**：恶意 `m3u8dl://` payload 注入 `_cookie_file` 指向任意本地文件 → 协议处理器和引擎层双重路径白名单验证

---

## ⚡ 下载成功率优化

### Aria2 引擎

- **低连接重试策略**：检测到 429 / 限流 / 超时 / 连接重置等 CDN 摩擦信号后，自动降低 `--max-connection-per-server` 和 `--split` 重试；支持 rate_limit_hint 预判降连接
- **下载统计汇总**：实时采样 CN / 速度 / 峰值，输出结构化日志 `aria2_download_summary`
- **Header 处理增强**：自动剥离浏览器捕获的 `Range`（aria2 自管分段），转发 `Sec-Fetch-*` 等反盗链关键头
- 新增 `--continue=true` / `--allow-overwrite=true` / `--connect-timeout=10` / `--timeout=30` 鲁棒性标志

### yt-dlp 引擎

重试策略从 2 级扩展到 **5 级链式重试**：

1. 默认模式（Cookie 文件 / 嗅探器 Cookie）
2. Bilibili 浏览器 Cookie 重试（输出分类门控，避免无效重试）
3. 通用指纹模拟 `--extractor-args generic:impersonate`（Cloudflare / bot-check / 403 / 空格式）
4. 浏览器 Cookie 链（Playwright Chromium → Chrome → Firefox）
5. 证书错误兜底 `--no-check-certificates`

新增终态失败检测 `_is_terminal_non_retryable_failure()`，对 DRM / 地区限制 / 已删除等不可重试场景立即终止，避免浪费重试次数。

### N_m3u8DL-RE 引擎

- **自适应低并发**：`_rate_limited_hosts` 类级记忆 + 限流检测，失败后用低线程数重试
- **Cookie 支持**：解析 Netscape 格式 cookie 文件，按 domain / path / secure / expiry 过滤
- **Cookie 路径验证**：F-03 路径白名单，防止 `_cookie_file` 注入
- 新增 `--check-certificate` TLS 控制
- 新增临时进度文件监控线程（0.6.x 通过临时文件输出进度）

### Streamlink 引擎

从单次尝试扩展为**质量降级链**：

- 质量降级：`best` → `720p` → `480p`
- 临时故障同质量重试：429 / 超时 / 重置 / 5xx（一次性）
- 终态失败检测：auth / 地区 / DRM / 离线 → 不降级

### HLS 探测器

- 新增 `estimated_bytes`：从播放列表估算下载大小（target_duration × segment_count × 保守码率）
- 新增扩展软失败模式：密钥探测 429 不再硬阻塞
- 新增结构化分阶段诊断（playlist / variant / key / segment）
- 新增 IP 固定会话 `make_pinned_session()`，防止 DNS 重绑定

### HTTP Header 转发增强

可转发 Header 白名单从 **5 个扩展到 12 个**：

```
旧: Referer, User-Agent, Origin, Cookie, Accept-Language
新: + Accept, Range, Sec-Fetch-Site, Sec-Fetch-Mode, Sec-Fetch-Dest,
    Sec-Ch-Ua, Sec-Ch-Ua-Mobile, Sec-Ch-Ua-Platform
```

`Authorization` 作为 opt-in Header，默认不转发，需显式开启。

---

## 🔒 安全加固

### 协议处理器

- 新增 `_strip_internal_keys()`：在 JSON 解析边界剥离 `_` 前缀键，防止 `_cookie_file` 路径注入（F-03）
- 日志清理节流（ISS-45）：5 分钟 / 50 次调用节流，避免性能损耗
- `M3U8D_HANDOFF_LEGACY=1` 启动时输出结构化安全警告（F-07）

### SSRF 防护

- 新增 `make_pinned_session()`：创建 IP 固定的 requests Session，防止 DNS 重绑定攻击
- 新增 `security.allow_private_networks` 配置项（默认 `false`），受信任的私有镜像部署可 opt-in

### 组件更新保护

- `n_m3u8dl_re` 新增 `allow_prerelease: false`，防止 N_m3u8DL-RE 0.6.0-beta 作为 GitHub "latest" 静默替换稳定版
- FFmpeg 版本正则收紧为纯版本号匹配，过滤构建后缀

---

## 📦 依赖变化

| 新增 | 最低版本 | 用途 |
|---|---|---|
| `psutil` | >= 5.9.0 | 进程树管理（ISS-30） |
| `cryptography` | >= 42.0.0 | TOFU HMAC pin 验证 |

---

## ⚙️ 配置文件变更

### 新增配置项

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `features.download_rate_limit_backoff_multiplier` | 3 | 限流回退乘数 |
| `features.failure_strategy_enabled` | true | 失败策略总开关 |
| `features.rate_limit_low_concurrency_enabled` | true | 限流低并发开关 |
| `features.hls_probe_extended_soft_fail_enabled` | true | HLS 探测扩展软失败 |
| `features.enhanced_header_capture_enabled` | true | 增强 Header 捕获 |
| `features.temporary_cookie_forwarding_enabled` | false | 临时 Cookie 转发 |
| `features.segment_suppression_enabled` | true | 分段压制开关 |
| `features.segment_suppression_threshold` | 3 | 分段压制阈值 |
| `features.forward_cookie_headers` | true | Cookie 转发开关 |
| `features.forward_authorization_headers` | false | Authorization 转发（默认关） |
| `features.exact_request_header_replay_enabled` | true | 精确请求头回放 |
| `features.resource_domain_cookie_lookup_enabled` | true | 资源域 Cookie 查找 |
| `features.ephemeral_m3u8_refresh_enabled` | false | 临时 m3u8 刷新 |
| `features.ephemeral_m3u8_refresh_timeout_ms` | 15000 | 临时 m3u8 刷新超时 |
| `features.playlist_diagnostics_enabled` | true | 播放列表诊断 |
| `features.adaptive_low_concurrency_enabled` | true | 自适应低并发 |
| `security.allow_private_networks` | false | 允许私有网络资源 |
| `tunables` | {} | 可调参数运行时覆盖 |
| `engines.n_m3u8dl_re.low_concurrency_thread_count` | 1 | N_m3u8DL-RE 低并发线程数 |
| `engines.aria2.low_connection_count` | 1 | Aria2 低连接数 |

### 默认值调整

| 配置项 | 旧值 | 新值 | 原因 |
|---|---|---|---|
| `max_concurrent_downloads` | 5 | 3 | 降低并发减少 CDN 限流触发 |
| `speed_limit` | 3 | 20 | 提高速度上限 |
| `engines.n_m3u8dl_re.thread_count` | 5 | 20 | 提高并行分段效率 |
| `engines.aria2.max_connection_per_server` | 16 | 2 | 降低单服务器连接触发限流 |
| `engines.aria2.split` | 16 | 2 | 降低分段连接减少限流 |

> **注意**：`config.json` 向后兼容，缺失的新配置项将使用编译时默认值。

---

## 🧪 测试

- **新增 35 个测试文件**（v0.4.1 无测试），覆盖：Aria2 扩展名修复、统一下载上下文（45 个子测试）、引擎选择器、Header 处理、HLS 探测、协议处理器、SSRF 防护、TOFU HMAC 等
- **新增 8 个冒烟测试脚本**：认证重试、Header 转发、进度一致性、限流策略、分段压制等

---

## 📁 新增文件清单

```
core/
  ├── download_context.py          # 统一下载上下文
  └── media_url_ttl.py             # CDN 签名/临时 URL 诊断
ui/
  └── main_window_postprocess.py   # FFmpeg 后处理
utils/
  ├── tunables.py                  # 可调参数中心化
  ├── proxy_config.py              # 代理配置统一
  └── notification_service.py      # 系统通知重构（Qt 原生）
tests/                             # 35 个测试文件
docs/
  └── error_codes.md               # 错误码文档
```

---

## ⬆️ 升级说明

- 直接从 v0.4.1 升级，`config.json` 向后兼容
- 首次启动时自动使用新配置默认值，无需手动修改
- 若需使用代理，请在 `config.json → proxy` 中配置
- 若需启用 FFmpeg 后处理，确保 `bin/ffmpeg.exe` 可用

---

## ⚠️ 已知问题

- 资源列表中文件名包含方括号（如 `[1080p]`）时，「文件已存在」检查可能误触发（glob 字符类解释），将在下个版本修复
- 内置浏览器（Playwright 驱动）可能无法播放 DRM 加密视频（Widevine CDM 未加载），请使用系统浏览器 + CatCatch 扩展捕获
