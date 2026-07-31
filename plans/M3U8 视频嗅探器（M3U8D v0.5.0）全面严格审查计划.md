# M3U8 视频嗅探器（M3U8D v0.5.0）全面严格审查计划

<aside>
🎯

**审查对象**：基于 PyQt6 的 Windows 桌面视频嗅探/下载工具（[main.py](http://main.py) / mvs.pyw，集成 Playwright + Chrome、N_m3u8DL-RE / yt-dlp / Streamlink / Aria2 / FFmpeg 五引擎、CatCatch HTTP 服务与 `m3u8dl://` 协议）。

**审查目标**：在正式发布/交付前，系统性发现功能缺陷、安全漏洞、并发与进程隐患、健壮性与兼容性问题，并建立可回归的测试基线。

**优先级标记**：🔴 高（阻断/安全级） · 🟡 中（影响体验/可靠性） · 🟢 低（优化/可维护）

</aside>

## 审查总则与方法

- 采用「白盒代码走查 + 黑盒功能测试 + 针对性安全渗透 + 并发/故障注入」四线并行。
- 每条缺陷记录：复现步骤、期望/实际、影响范围、严重级别、修复建议、回归用例。
- 安全相关项一律按「不可信输入」假设审查：浏览器扩展、协议、命令行、config.json、远端清单、站点返回内容均视为攻击面。
- 重点验证文档第 14 节「代码存在但未完成」的项，避免把预留类当成已完成能力。

---

## 0. 审查准备与环境搭建 🟡

- [ ]  在干净的 Windows 10 / 11 64 位环境完整部署（Python 3.9+、requirements.txt、系统 Chrome、bin 下五引擎）
- [ ]  验证「未安装 Chrome」「缺引擎」「缺 ffmpeg」等降级场景的提示与行为
- [ ]  准备测试样本集：普通 m3u8、master+variant、加密 HLS、mpd/DASH、直链 mp4、magnet、页面站（YouTube/B站/TikTok）、直播流
- [ ]  搭建可控的恶意/边界测试服务器（用于 SSRF、超大响应、异常 Content-Type、慢响应、证书错误等）
- [ ]  固定代码版本/commit，建立缺陷跟踪表与回归清单

---

## 1. 架构与代码质量 🟢

- [ ]  核对实际模块职责与文档第 1 节调用链是否一致（[main.py](http://main.py) → main_window → playwright_driver → m3u8_sniffer → resource_panel → download_manager → engines/*）
- [ ]  模块耦合度、循环依赖、UI 层与 core 层职责边界是否清晰
- [ ]  全局状态/单例（DownloadManager、Sniffer、CatCatchServer）的生命周期与可测试性
- [ ]  异常分类逻辑（auth/parse/timeout/unknown）的实现是否集中可维护
- [ ]  魔法数字/硬编码（端口 9527-9539、超时 500ms/1.5s/2s/10min、500MB 估算、1.2× 磁盘系数）是否集中配置
- [ ]  代码风格、类型注解、lint（建议引入 ruff/mypy）与死代码清理

---

## 2. 安全审查（最高优先级）🔴

### 2.1 SSRF 与 URL 校验

- [ ]  验证 `ensure_public()` 对回环/私网/链路本地/云元数据（127/8、10/8、172.16/12、192.168/16、169.254/16、::1、fc00::/7、fe80::/10、169.254.169.254）的拦截是否完整
- [ ]  验证 [main.py](http://main.py) `--url`、协议处理器、CatCatch `POST /download` 共用同一校验路径，无绕过分支
- [ ]  DNS 重绑定（TOCTOU）：解析时为公网、下载时指向内网的攻击是否可行；解析失败是否按不可信处理
- [ ]  重定向跟随是否会绕过 SSRF 校验（302 跳转到内网/metadata）——各引擎与 HLSProbe/HEAD 探测均需核查
- [ ]  IPv6、十进制/八进制/十六进制 IP、`http://[::ffff:127.0.0.1]`、含凭证 URL（user:pass@）、超长(>4096) URL 的边界处理
- [ ]  `allow_private_networks=true` 降权路径的告警是否醒目、默认是否关闭
- [ ]  m3u8 内部 ts/key 子地址、嵌套 master 解析是否同样过 SSRF 过滤

### 2.2 命令注入与子进程参数构造

- [ ]  所有引擎调用是否一律使用参数数组（无 shell=True、无字符串拼接），文档承诺需逐处核实
- [ ]  yt-dlp `format_id` 白名单 `[A-Za-z0-9_.+:\-]+` 实现校验，注入字符（空格/换行/`;`/`|`/反引号/`$()`/`&`/`<>`）确被拒为 `invalid_format_id`
- [ ]  文件名/保存路径/save-dir 注入：Windows 保留名、控制字符、尾部点/空格剥离、240 字节上限、空回退 `media_<ts>`
- [ ]  Streamlink Cookie 按 `;` 拆分、URL 转义、空名丢弃的实现正确性
- [ ]  header 值进入命令行前的清洗（禁 `\r\n\0`、长度限制）是否对所有引擎生效
- [ ]  N_m3u8DL-RE 多候选地址（primary/master/media）与 `--select-video` 拼装的参数安全

### 2.3 本地 HTTP 服务（CatCatch）鉴权与跨站防护

- [ ]  确认仅绑定 `127.0.0.1`，绝不绑 `0.0.0.0`/`::`
- [ ]  `POST /download` 的 `X-Session-Token` 校验：缺失/错误返回 401，Origin 不在白名单返回 403 且不回显 CORS 头
- [ ]  `Access-Control-Allow-Origin` 只回显具体白名单 origin，绝不为 `*`
- [ ]  `GET /download` 确返回 405；`GET /`、`/status` 无敏感信息泄露
- [ ]  请求体 64 KiB 上限（返回 413 + `catcatch_body_too_large`）
- [ ]  `_` 前缀内部 header（如 `_cookie_file`）在转发前被剥离，外部无法注入本地文件读取
- [ ]  header 字段允许集（仅 Referer/User-Agent/Origin/Cookie/Accept-Language）与名称 `[A-Za-z0-9-]`/64 字符、值 4096 字符限制
- [ ]  端口探测 9527-9539 占用/耗尽处理，多实例并存的 token 冲突
- [ ]  CSRF：本地服务被恶意网页跨站请求（DNS rebinding 到 127.0.0.1）的防护是否依赖 token + Origin 双因子

### 2.4 `m3u8dl://` 协议处理器

- [ ]  三种输入格式（带参数行、裸 URL、JSON）解析的健壮性与注入面
- [ ]  读取 `~/.m3u8d/session.token` 并以 token + `Origin:127.0.0.1` 向各端口 POST 的握手逻辑
- [ ]  仅在握手全失败时才拉起 [main.py](http://main.py) 的判定是否可被滥用（重复拉起、参数透传安全）
- [ ]  JSON payload 同样剥离 `_` 前缀键
- [ ]  `M3U8D_HANDOFF_LEGACY=1` 紧急回退路径（无 token/无 Origin）默认关闭、日志留痕
- [ ]  `register_protocol.bat` 注册表写入的安全与卸载清理

### 2.5 组件自动更新校验链（供应链安全）

- [ ]  「无 sha256 不安装」底线：`missing_checksum` 在三类来源（静态 pin / 动态 sidecar / TOFU）全失败时确实触发
- [ ]  下载后逐字节比对 sha256，失败 `checksum_mismatch` 并删除暂存文件
- [ ]  替换前再次校验暂存件（`staging_tampered`）、磁盘 1.2× 预检（`insufficient_disk`）
- [ ]  TOFU pin（N_m3u8DL-RE / streamlink）首次记录、后续 `pin_mismatch` 回滚；`~/.m3u8d/component_pins.json` 防篡改
- [ ]  严格 HTTPS 域名白名单（[github.com](http://github.com) / [objects.githubusercontent.com](http://objects.githubusercontent.com) / PyPI CDN）无法被重定向绕过
- [ ]  Authenticode/内置公钥签名校验（Windows）有效性
- [ ]  `.bak` 备份/回滚、占用文件 `deferred_pending_restart`、失败路径绝不污染 bin 目录
- [ ]  安装后 `--version` 交叉校验与「兼容前缀匹配」规则（如 `8.1.1` 不兼容 `8.1.10`，触发 `version_mismatch` 回滚）
- [ ]  `M3U8D_SECURITY_DIAGNOSTIC` + `allow_weak_manifest_verification` 双开关默认关闭，审计日志完整

### 2.6 敏感信息处理与日志脱敏

- [ ]  命令行/URL 中敏感头（Cookie/Set-Cookie/Authorization/Proxy-Authorization/X-Session-Token/UA/Referer/Origin）与查询参数（token/sign/signature/auth）写盘前 `<redacted>`
- [ ]  `history.json` 写入前 denylist 剥离凭证类头（Cookie/Authorization/Token/Api-Key 等）
- [ ]  `SECURITY_DEBUG` 的 `debug.sensitive.log` 默认关闭，开启有明确警示
- [ ]  `session.token` 权限（POSIX 0600 / Windows owner-only DACL）实测正确
- [ ]  协议处理器日志仅留 `token_loaded`/`token_len`/`status_code` 等脱敏元数据，无 token 明文

### 2.7 文件系统与路径安全

- [ ]  路径穿越（save-dir/filename 含 `..`、UNC 路径 `\\server\share`、绝对路径覆盖）
- [ ]  临时文件清理（`.part`/`.tmp`/残留分段）不误删进行中任务、竞态删除安全
- [ ]  持久化目录权限、并发写 config.json/history.json 的原子性与损坏恢复

---

## 3. 功能正确性（按模块）🟡

### 3.1 浏览器工作台 / Playwright 驱动

- [ ]  持久化用户目录、`SingletonLock` 残留清理、`--disable-blink-features=AutomationControlled` 注入
- [ ]  page/request/response/download/console 事件监听完整性与多标签自动配置
- [ ]  「Browser ready」状态机、Start/Stop Browser 反复操作、Chrome 崩溃/被手动关闭的恢复
- [ ]  地址栏自动补 `https://`、magnet 不导航而直接构造资源（强制 Aria2）
- [ ]  捕获窗口机制：基础时长、命中延长、扫描间隔（video/source/performance）配置生效

### 3.2 嗅探与资源入列

- [ ]  `add_resource()` 头部归一化（小写、referer/UA 自动填充、origin 从 referer 推断）
- [ ]  四条发现路径（页面模式匹配 / request / response Content-Type / 注入脚本 console）覆盖
- [ ]  去重多层策略（同 URL 合并上下文、YouTube 按 videoID+itag+title、master vs media 分键、variant 按 height/bandwidth/variant_url）
- [ ]  `candidate_score` 评分逻辑与边界

### 3.3 资源列表 / M3U8 解析与变体展开

- [ ]  `M3U8FetchThread` 后台解析、`#EXT-X-STREAM-INF` 解析、嵌套 master 递归（`m3u8_nested_depth` 限深，防无限递归/环）
- [ ]  变体行生成、分辨率列回填、标题后缀 `[1080p]` 正确
- [ ]  搜索/类型/来源/分辨率过滤与 UI 本地化映射一致性（Type 列下拉与可见文本匹配）
- [ ]  点击下载路由 A/B/C 分支（页面站优先 page_url、m3u8 复用已缓存 variants、其他直接入队）
- [ ]  四种入队反馈（queued / merged / needs_confirmation / failed）UI 提示准确无歧义

### 3.4 下载中心 / 任务管理

- [ ]  保存位置切换即时写回 config.json、打开文件夹、线程/重试/并发/限速参数映射各引擎正确
- [ ]  幂等键 `sha1(url|engine|out_dir|title)` 命中合并、重复点击不堆叠
- [ ]  磁盘预检（估算回退 500MB、1.2× 系数、bypass 审计记录）
- [ ]  队列状态机 waiting/downloading/paused/failed/completed 与颜色/进度/速度显示
- [ ]  右键菜单与底部按钮（暂停/恢复/停止/删除/重试/打开位置/全部暂停/清除已完成/排序/批量导入/复制链接/播放）
- [ ]  批量导入仅入资源列表不立即下载、非法项过滤

### 3.5 各下载引擎

- [ ]  N_m3u8DL-RE：`--help` 探测参数、primary/master/media 候选、安全模式回退、限速/线程/重试/输出格式、`--adaptive`/`--force-http1`/`--no-date-info` 仅在支持时附加
- [ ]  yt-dlp：`-J` 取格式、Cookie 文件按 host 沿 `.` 边界回退查找、Firefox Cookie 回退、证书错误 `--no-check-certificates` 重试一次、stdout 三级编码回退（utf-8→mbcs→latin-1）标记
- [ ]  Streamlink：`.ts` 输出、无总进度时显示写入量、401/403/超时/地域诊断、Cookie 拆分转义
- [ ]  Aria2：多连接、限速、直链/magnet、头部附加
- [ ]  FFmpeg：remux/合并/字幕/压缩方法（确认 UI 未暴露但代码可用），合并中取消保留已完成分段

### 3.6 历史与重下载

- [ ]  `.m3u8sniffer/history.json` 字段完整、重下载用已脱敏头导致鉴权失败的提示是否到位
- [ ]  重下载/打开位置/查看日志/删除/复制 行为正确

---

## 4. 并发与线程安全 🔴

- [ ]  DownloadManager 的 Queue + 多 worker 线程的数据竞争、共享状态加锁
- [ ]  动态调并发：N→M 软退出信号、30s 内未退出记 `worker_exit_timeout` 且不强杀、`active_workers` 实时准确
- [ ]  Qt 主线程与工作线程交互：信号/槽跨线程、UI 仅主线程更新（QThread/QObject 归属）
- [ ]  M3U8FetchThread、HLSProbe、HEAD 探测等后台线程与列表/任务状态的并发一致性
- [ ]  计数器 success_total/failed_total/by_engine/by_stage 的原子性
- [ ]  站点规则自动学习（site_rules_auto）并发写入与 max_rules 截断
- [ ]  死锁、线程泄漏、关闭程序时线程优雅退出

---

## 5. 进程与资源生命周期管理 🔴

- [ ]  暂停/取消响应：read loop 跳出、500ms terminate、1.5s 后递归 kill、端到端≤2s 实测
- [ ]  子进程树是否被完整结束（避免 N_m3u8DL-RE/ffmpeg 子进程成孤儿/僵尸）
- [ ]  句柄/管道泄漏：长时间多任务后文件句柄、stdout 管道是否回收
- [ ]  Chrome/Playwright 进程在程序退出/崩溃后是否残留
- [ ]  删除任务后 3 秒清理临时文件的竞态、已完成输出文件保留

---

## 6. 错误处理与健壮性 🟡

- [ ]  网络异常（超时/断连/DNS 失败/证书错误/网关错误）的分类与重试/回退
- [ ]  重试与回退链（download_retry_enabled / engine_fallback / auth_retry_first / auth_retry_per_engine、backoff 递增）逻辑闭环、无死循环
- [ ]  HLS 预检（hls_probe_enabled / hard_fail）软/硬失败行为
- [ ]  候选链路评分（https/referer/origin/cookie 加分、广告/tracker 减分）
- [ ]  异常未捕获导致 UI 卡死/崩溃；引擎二进制缺失/损坏/版本不兼容的降级
- [ ]  磁盘写满、目标文件占用、同名文件覆盖决策

---

## 7. 配置管理 🟡

- [ ]  config.json 缺失/字段缺失/类型错误/损坏 的默认值兜底与迁移
- [ ]  全部配置项落地核查（download_dir/temp_dir/并发/限速/重试/backoff、site_rules(_auto)、features.*、engines.*、proxy、catcatch.port、auto_delete_temp）
- [ ]  文档第 14 节「预留项」明确标注：proxy 未统一透传、auto_delete_temp 未接管 → 验证实际行为，避免误导
- [ ]  UI 修改即时写回与并发写一致性、非法值范围校验（线程/并发/端口）

---

## 8. 网络与下载可靠性 🟡

- [ ]  HEAD MIME 探测 2s 超时 + SSRF 保护 + 失败回退扩展名（`engine_select=fallback`）
- [ ]  引擎选择优先级链（用户选 → 扩展名 → MIME → LIVE_PLATFORMS → yt-dlp 兜底）与 `engine_rules.json` 外部化
- [ ]  自动选择顺序 N_m3u8DL-RE → Streamlink → Aria2 → yt-dlp 实际命中正确
- [ ]  限速换算各引擎（MB/s → 各自参数）、Streamlink 不支持限速的说明一致
- [ ]  代理预留项当前不生效的明确性（避免用户误以为已走代理）

---

## 9. UI / UX 与未完成功能 🟡

- [ ]  **后退/前进/刷新为 no-op 占位**：明确标注或禁用，避免用户误解（高优先 UX 缺陷）
- [ ]  「新建标签」实为发起导航的行为是否符合预期
- [ ]  系统通知当前仅写日志、不弹 toast（plyer 注释未启用）——UI 文案需一致
- [ ]  长路径/版本号截断的 hover 提示
- [ ]  国际化/本地化一致性、错误提示可读性、空状态与加载态
- [ ]  可访问性、窗口缩放、拖拽分割区、暗色模式（如有）

---

## 10. 数据持久化与一致性 🟡

- [ ]  history.json / config.json / component_pins.json / session.token 的并发写、原子写（临时文件+rename）、损坏恢复
- [ ]  日志按日轮转、超限节流轮转（每 1000 条/5s 检查）正确，不阻塞下载
- [ ]  异常退出后的状态恢复（进行中任务、deferred 组件更新）

---

## 11. 兼容性、环境与编码 🟡

- [ ]  Windows 10/11、不同 DPI/分辨率、非管理员权限运行
- [ ]  中文/非 ASCII 路径与文件名、CP936 等系统代码页、emoji/特殊字符
- [ ]  Python 3.9 最低版本实测、依赖版本锁定与冲突
- [ ]  系统 Chrome 缺失/多版本/非默认路径的检测
- [ ]  各引擎二进制版本差异下的参数兼容（`--help` 探测覆盖度）

---

## 12. 日志与可观测性 🟢

- [ ]  运行日志面板仅显示关键/WARNING/ERROR/CRITICAL 的过滤正确
- [ ]  `M3U8D_LOG_DEBUG=1` 提升至 DEBUG、文件日志默认 INFO
- [ ]  关键事件埋点齐全（入队/开始/完成/失败/回退/CatCatch/配置变更/启停）
- [ ]  SSRFBlocked、checksum_mismatch、pin_mismatch、worker_exit_timeout 等诊断标记可检索

---

## 13. 性能与压力 🟢

- [ ]  大量资源（数千行）入列时列表渲染/过滤/去重性能
- [ ]  高并发下载、FFmpeg 大文件合并时的 CPU/内存/磁盘 IO
- [ ]  长时间运行内存增长（嗅探缓存、page_url 映射、日志缓冲泄漏）
- [ ]  捕获窗口高频扫描（probe_interval_ms 过小）对页面性能影响

---

## 14. 测试体系建设 🟡

- [ ]  单元测试：URL 校验/SSRF、header 清洗、文件名清洗、format_id 白名单、版本兼容匹配、去重键、引擎选择
- [ ]  集成测试：协议处理器→HTTP 服务→入列→下载 全链路
- [ ]  安全用例：注入/越权/SSRF/篡改清单 回归集
- [ ]  故障注入：网络中断、进程被杀、磁盘满、文件占用
- [ ]  覆盖率统计与 CI 接入（关键安全模块要求高覆盖）

---

## 15. 打包、安装与升级 🟡

- [ ]  安装器默认下载必需组件（yt-dlp/N_m3u8DL-RE/FFmpeg）、可选项（aria2/streamlink）选择
- [ ]  组件管理器：刷新本地状态、检查更新（只读不自动装）、全部更新（二次确认）、单组件安装/更新/重试
- [ ]  升级中暂存目录→校验→替换→回滚 全链路实测（结合第 2.5 节）
- [ ]  卸载清理（注册表、用户目录文件、协议注册）

---

## 16. 文档与可维护性 🟢

- [ ]  手册描述与实际代码行为逐项核对（本计划已据手册编制，发现不符即记缺陷）
- [ ]  关键安全设计有内联注释/设计文档
- [ ]  配置项、环境变量、错误码（missing_checksum/checksum_mismatch/staging_tampered/insufficient_disk/pin_mismatch/version_mismatch/invalid_format_id 等）有集中说明

---

## 17. 合规与法律风险 🟡

- [ ]  视频下载/绕过自动化检测（`AutomationControlled`）涉及目标站点 ToS 与版权风险的免责声明
- [ ]  Cookie/登录态复用、凭证存储的隐私合规
- [ ]  第三方二进制（yt-dlp/N_m3u8DL-RE/ffmpeg/aria2/streamlink）许可证合规与分发

---

## 18. 已知「代码存在但未完成」项专项核查 🟡

<aside>
⚠️

以下项手册已声明为「存在但未作为完整对外功能」，需逐一确认现状并决定：标注为实验性 / 隐藏 / 补全 / 移除。

</aside>

- [ ]  FFmpegProcessor 已加载但 UI 无后处理按钮
- [ ]  QWebEngine NetworkInterceptor 存在但非主嗅探路径
- [ ]  auto_delete_temp / proxy 配置项存在但主流程未统一接管
- [ ]  浏览器 后退/前进/刷新 为占位实现
- [ ]  系统通知 仅日志、未启用 toast

---

## 审查执行阶段与排期建议

| 阶段 | 内容 | 重点章节 | 建议产出 |
| --- | --- | --- | --- |
| :--- | :--- | :--- | :--- |
| P0 准备 | 环境/样本/工具 | 0 | 测试环境 + 样本库 |
| P1 安全 | 安全攻击面渗透 | 2、4、5 | 安全缺陷报告（最高优先） |
| P2 功能 | 模块功能正确性 | 3、6、8 | 功能缺陷清单 |
| P3 健壮 | 并发/进程/故障注入 | 4、5、6、10 | 稳定性报告 |
| P4 体验 | UI/兼容/未完成项 | 9、11、18 | UX 与一致性清单 |
| P5 工程 | 测试/打包/文档/合规 | 14、15、16、17 | 测试基线 + 发布检查单 |

## 高风险热点速览（建议优先攻击）

1. 🔴 **SSRF 绕过**（DNS 重绑定、重定向跟随、IPv6/编码变体）— 第 2.1
2. 🔴 **命令注入**（filename/format_id/header 进入引擎命令行）— 第 2.2
3. 🔴 **本地 HTTP 服务越权/CSRF**（token + Origin 双因子是否可绕过）— 第 2.3 / 2.4
4. 🔴 **供应链/自动更新**（校验链与回滚是否真的不可绕过）— 第 2.5
5. 🔴 **进程残留与线程安全**（孤儿进程、强杀边界、Qt 跨线程）— 第 4 / 5
6. 🟡 **凭证泄露**（日志/history/sensitive.log 脱敏遗漏）— 第 2.6