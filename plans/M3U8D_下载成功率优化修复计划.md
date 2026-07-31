# M3U8D 下载成功率优化修复计划

## 0. 背景与目标

本计划基于前期排查结论制定，当前失败主要集中在：

- 防盗链导致的 `403`。
- 登录态缺失导致的 `401`、`403`。
- CDN 限流导致的 `429`。
- `.ts`、`.m4s`、`.aac` 等 segment 误捕获，导致用户下载到单个分片或错误候选。

项目现有基础已经较完整：[`select_engine()`](core/engine_selector.py:343) 支持 context、HEAD MIME、extension、live、yt-dlp fallback；[`DownloadManager._execute_download()`](core/download/manager.py:1421) 支持 retry、backoff、auth retry、engine fallback、candidate ranking；[`HLSProbe.probe()`](core/services/hls_probe.py:40) 支持 playlist、variant、key、segment 预探测；[`N_m3u8DL_RE_Engine.download()`](engines/n_m3u8dl_re.py:163)、[`YtdlpEngine.download()`](engines/ytdlp_engine.py:298)、[`Aria2Engine.download()`](engines/aria2_engine.py:37) 已有一定自恢复能力。

本计划目标不是重写下载体系，而是在现有架构上分阶段修复真实高频失败点：

1. 降低 segment 误捕获造成的错误下载。
2. 补齐防盗链和登录态所需 headers、cookies 传递链路。
3. 对 `403`、`429` 做更明确的失败分类和策略化重试。
4. 用指标和 smoke 测试验证成功率提升，避免盲改。

## 1. 总体实施原则

1. 先修复高频根因，再优化边缘场景。
2. 默认安全保守，Cookie、Authorization 不做默认持久化和跨域转发。
3. 对 segment 做“降权、折叠、确认”，不做“一刀切删除”。
4. 对 `403`、`429`、登录态缺失分别处理，不盲目统一重试。
5. 每个阶段必须配套测试、日志事件、回滚方案。
6. 优先复用已有结构：[`M3U8Sniffer.add_resource()`](core/m3u8_sniffer.py:43)、[`infer_resource_type()`](core/download_context.py:108)、[`select_engine()`](core/engine_selector.py:343)、[`DownloadManager._execute_download()`](core/download/manager.py:1421)、[`sanitize_headers()`](utils/headers.py:111)、[`iter_engine_headers()`](utils/headers.py:321)。

## 2. 里程碑总览

| 里程碑 | 主题 | 主要解决问题 | 优先级 | 预计收益 | 风险 |
|---|---|---|---:|---:|---:|
| M0 | 基线、指标、测试保护 | 优化前无法量化收益 | P0 | 间接高 | 低 |
| M1 | segment 降噪和 playlist 优先 | segment 误捕获、错误下载 | P0 | 高 | 中低 |
| M2 | headers 捕获与传递增强 | 防盗链、登录态、`403` | P0 | 高 | 中高 |
| M3 | site rules 与临时 Cookie 授权 | 稳定反盗链站点、登录态站点 | P0 | 高 | 中高 |
| M4 | `403`、`429` 失败分类和策略化重试 | auth、rate limit 混在 unknown | P1 | 中高 | 中 |
| M5 | N_m3u8DL-RE 和 HLS probe 自适应 | CDN 限流、弱网、probe 误判 | P1 | 中高 | 中 |
| M6 | yt-dlp、Streamlink 专项增强 | 页面类、直播类失败 | P2 | 中 | 中 |

建议实施顺序：M0 → M1 → M2 → M3 → M4 → M5 → M6。

## 3. M0：建立基线、指标和测试保护

### 3.1 目标

先建立优化前基线，确保后续能判断是否真正提升下载成功率。

### 3.2 实施任务

#### M0-T1：扩展日志指标口径

在 [`DownloadManager._record_metric()`](core/download/manager.py:1172)、[`DownloadManager.get_quality_metrics()`](core/download/manager.py:2346) 基础上补充这些计数：

- `download_start`
- `task_completed`
- `task_failed`
- `fail_reason_auth`
- `fail_reason_rate_limit`
- `fail_reason_timeout`
- `fail_reason_parse`
- `fail_reason_segment_noise`
- `auth_retry_success`
- `fallback_success`
- `low_concurrency_retry_success`
- `hls_probe_soft_fail_then_success`
- `candidate_suppressed_segment`

日志事件必须结构化，URL 要继续使用现有脱敏逻辑，不输出明文 Cookie、Authorization、token query。

#### M0-T2：增强日志统计脚本

在 [`scripts/s5_metrics_from_logs.py`](scripts/s5_metrics_from_logs.py:5) 和 [`scripts/s5_compare_metrics.py`](scripts/s5_compare_metrics.py:5) 增加新的 pattern：

- `event=fail_reason_auth`
- `event=fail_reason_rate_limit`
- `event=segment_suppressed`
- `event=auth_retry_success`
- `event=fallback_success`
- `event=low_concurrency_retry_success`

#### M0-T3：建立最小样本集

准备以下样本并记录期望行为：

1. 公开 `.m3u8`：应走 N_m3u8DL-RE。
2. 公开 `.mp4`：应走 Aria2。
3. 需要 Referer 的 `.m3u8`：缺 Referer 时失败，补 Referer 后成功。
4. 需要 Cookie 的页面视频：未授权 Cookie 时提示登录态缺失，授权后成功。
5. 连续 `.ts` 分片页面：应折叠或降权 segment，优先展示 playlist。
6. 模拟 `429` segment：应触发 soft fail 或低并发策略。

#### M0-T4：补齐 smoke 测试入口

复用并扩展现有脚本：

- [`scripts/smoke_engine_select_mime.py`](scripts/smoke_engine_select_mime.py:1)：验证引擎选择不退化。
- [`scripts/smoke_hls_probe_soft_fail.py`](scripts/smoke_hls_probe_soft_fail.py:1)：验证 segment `429` soft fail。
- [`scripts/smoke_backoff_retry.py`](scripts/smoke_backoff_retry.py:1)：验证 parser retry 和 backoff。
- [`scripts/smoke_catcatch_auth.py`](scripts/smoke_catcatch_auth.py:1)：验证 CatCatch auth gate。

新增建议脚本：

- `scripts/smoke_segment_suppression.py`：验证 segment 降权和隐藏。
- `scripts/smoke_header_forwarding.py`：验证 Referer、Origin、Accept-Language 可传递到任务 headers。
- `scripts/smoke_auth_retry_site_rules.py`：验证 auth fail 后应用 site rules retry。
- `scripts/smoke_rate_limit_strategy.py`：验证 `429` 后降并发或低连接重试。

### 3.3 验收标准

- 能统计优化前后成功率和失败原因分布。
- smoke 测试能覆盖引擎选择、HLS probe、retry、CatCatch auth。
- 日志中无明文 Cookie、Authorization、token、sign 等敏感信息。
- 不改变用户现有下载行为。

### 3.4 回滚方案

M0 主要是观测和测试，不改变核心行为。若日志量过大或指标异常，可通过配置关闭新增结构化事件输出。

## 4. M1：segment 降噪和 playlist 优先

### 4.1 目标

解决 `.ts`、`.m4s`、`.aac` 等分片误捕获导致的错误下载、资源列表噪声和用户误点问题。

### 4.2 现状依据

[`infer_resource_type()`](core/download_context.py:108) 已有 `segment` 识别能力，但 [`resources/engine_rules.json`](resources/engine_rules.json:4) 仍把 `.ts` 放入 direct extensions，可能使 segment 经 [`select_engine()`](core/engine_selector.py:343) 路由到 Aria2。资源入库在 [`M3U8Sniffer.add_resource()`](core/m3u8_sniffer.py:43)，候选评分在 [`M3U8Sniffer._score_m3u8_candidate()`](core/m3u8_sniffer.py:378)，UI 入口在 [`MainWindowSniffFlowMixin._on_resource_found()`](ui/main_window_sniff_flow.py:88)。

### 4.3 实施任务

#### M1-T1：强化 segment 类型识别

在 [`infer_resource_type()`](core/download_context.py:108) 中明确识别：

- `.ts`
- `.m4s`
- `.aac`
- `.mp4` init segment 的典型 path pattern
- `.key`
- 带序号 pattern 的 segment URL，例如 `seg-00001.ts`、`chunk_001.m4s`

输出上下文中保留 `resource_type=segment`，供 [`select_engine()`](core/engine_selector.py:343)、[`M3U8Sniffer.add_resource()`](core/m3u8_sniffer.py:43)、UI 使用。

#### M1-T2：资源列表 segment 降权

在 [`M3U8Sniffer.add_resource()`](core/m3u8_sniffer.py:43) 入库时：

1. 若资源是 segment，默认降低 `candidate_score`。
2. 若同页面已发现 `.m3u8` 或 `.mpd`，同源 segment 默认隐藏或折叠。
3. 若短时间内出现多个同 pattern segment，合并为“疑似 HLS/DASH 分片组”。
4. 记录 `event=segment_suppressed` 或 `event=segment_grouped`。

#### M1-T3：playlist 优先策略

在 [`M3U8Sniffer._score_m3u8_candidate()`](core/m3u8_sniffer.py:378) 和 [`DownloadManager._score_m3u8_candidate()`](core/download/manager.py:1105) 中提高 playlist 权重：

- `.m3u8`、`.mpd` 加权。
- `playlist`、`index.m3u8`、`master.m3u8`、`media.m3u8` 加权。
- 同源同页的 segment 降权。
- 有 Cookie、Referer、Origin 的候选加权。

#### M1-T4：引擎选择修正

在 [`select_engine()`](core/engine_selector.py:343) 或 [`EngineSelector.get_candidates()`](core/engine_selector.py:468) 中增加规则：

- `resource_type=segment` 不直接因 `.ts` 扩展名走 Aria2 优先。
- 若存在 `page_url`，优先提示等待 playlist 或尝试 yt-dlp 页面解析。
- 若存在 `master_url` 或 `media_url`，优先使用 playlist。
- 用户明确选择“下载单个分片”时才允许 Aria2。

#### M1-T5：UI 层折叠 segment

在 [`MainWindowSniffFlowMixin._on_resource_found()`](ui/main_window_sniff_flow.py:88) 或资源列表逻辑中：

- 默认不把 segment 作为主资源展示。
- 提供“显示分片资源”的高级开关。
- 对被折叠的 segment 显示计数，例如“已隐藏 128 个分片”。

### 4.4 验收标准

- 连续 `.ts` 页面不再刷出大量可点击主资源。
- 已发现 playlist 时，segment 自动折叠或隐藏。
- `.mp4` 直链仍走 Aria2。
- 用户仍可通过高级入口下载单个 `.ts`。
- [`scripts/smoke_engine_select_mime.py`](scripts/smoke_engine_select_mime.py:1) 通过。
- 新增 `scripts/smoke_segment_suppression.py` 通过。

### 4.5 风险与回滚

风险：部分真实完整 `.ts` 文件被降权。  
控制：不删除，只降权；保留高级开关；可通过配置关闭 segment suppression。

## 5. M2：headers 捕获与传递增强

### 5.1 目标

提升防盗链和登录态下载成功率，重点解决缺 Referer、Origin、Cookie、Accept-Language、Sec-Fetch 导致的 `403`、`401`。

### 5.2 现状依据

二次复核后需要区分两条内置浏览器链路：[`PlaywrightDriver._emit_detected_resource()`](core/playwright_driver.py:1138) 已经发出结构化 [`resource_context_detected`](core/playwright_driver.py:1176)，[`PlaywrightDriver._build_default_headers()`](core/playwright_driver.py:1079) 已能补 Referer、Origin、User-Agent，并会在 `.m3u8` 缺 Cookie 时尝试从浏览器上下文合并 Cookie；而 Qt WebEngine 的 [`NetworkInterceptor.interceptRequest()`](core/request_interceptor.py:29) 仍主要只有 URL、Referer、User-Agent，且不应假设能可靠读取 Cookie。因此 M2 不是“从零增加 headers 捕获”，而是把 Playwright、Qt、CatCatch、手动任务统一到受控 header policy 下。

[`M3U8Sniffer._merge_resource_context()`](core/m3u8_sniffer.py:205) 已能合并更多 headers，但需要统一清洗和敏感字段策略。CatCatch 路径在 [`DownloadRequestHandler._handle_download_request()`](core/catcatch_server.py:582) 已经可以接收并清洗 headers。headers 清洗在 [`sanitize_headers()`](utils/headers.py:111)，引擎转发在 [`iter_engine_headers()`](utils/headers.py:321)。

### 5.3 实施任务

#### M2-T1：扩展内置 interceptor 输出结构

将 [`NetworkInterceptor.video_detected`](core/request_interceptor.py:14) 从 `url, referer, user_agent` 扩展为结构化 payload 或新增信号，包含：

- URL。
- page_url。
- resource_type。
- request headers。
- timestamp。
- frame/page context。

保持兼容：旧信号可以保留一段时间，新增 signal 或 adapter 避免一次性破坏 UI 连接。

#### M2-T2：捕获非敏感 headers

优先捕获并转发这些 header：

- Referer。
- User-Agent。
- Origin。
- Accept。
- Accept-Language。
- Range。
- Sec-Fetch-Site。
- Sec-Fetch-Mode。
- Sec-Fetch-Dest。
- Sec-Ch-Ua。
- Sec-Ch-Ua-Mobile。
- Sec-Ch-Ua-Platform。

这些字段与 [`FORWARDABLE_HEADER_ALLOWLIST`](utils/headers.py:65) 基本一致，适合默认捕获和临时转发。

#### M2-T3：统一 headers 清洗入口

所有入口统一走 [`sanitize_headers()`](utils/headers.py:111)：

- 内置浏览器 interceptor。
- CatCatch。
- 手动添加任务。
- site rules 注入。

进入引擎前统一使用 [`iter_engine_headers()`](utils/headers.py:321)。

#### M2-T4：下载前 headers 兜底

保留并增强 [`DownloadManager._normalize_task_headers_for_download()`](core/download/manager.py:1045)：

- 缺 User-Agent 时补默认浏览器 UA。
- 有 Referer 但缺 Origin 时从 Referer 推导 Origin。
- 对 HLS/DASH 保证 Referer、Origin、UA 尽量完整。
- 不覆盖用户或规则显式设置的 header。

#### M2-T5：header 完整度评分

在 [`M3U8Sniffer._score_m3u8_candidate()`](core/m3u8_sniffer.py:378) 和 [`DownloadManager._score_m3u8_candidate()`](core/download/manager.py:1105) 中：

- 有 Referer 加分。
- 有 Origin 加分。
- 有 Cookie 加高分，但只在临时授权范围内。
- 有 User-Agent 加分。
- 同 host 或 page host 匹配加分。

### 5.4 验收标准

- 防盗链样本缺 Referer 时失败，补齐后成功。
- 内置浏览器路径和 CatCatch 路径对同一 URL headers 行为一致。
- N_m3u8DL-RE、Aria2、yt-dlp 都能收到允许转发的 headers。
- 日志中不输出明文敏感 headers。
- 新增 `scripts/smoke_header_forwarding.py` 通过。

### 5.5 风险与回滚

风险：捕获范围扩大可能增加隐私风险。  
控制：默认只捕获 allowlist 非敏感 headers；敏感 headers 另走 M3 显式授权；可配置关闭增强捕获。

## 6. M3：site rules、临时 Cookie、登录态修复

### 6.1 目标

解决稳定防盗链站点和登录态站点下载失败，重点提升 `403`、`401` 场景成功率。

### 6.2 现状依据

[`DownloadManager._apply_site_rules_to_task()`](core/download/manager.py:1015)、[`DownloadManager._learn_site_rule_from_task()`](core/download/manager.py:1211)、[`M3U8Sniffer._apply_site_rules()`](core/m3u8_sniffer.py:357) 已有 site rules 基础，但当前 [`config.json`](config.json:10) 中规则为空，auto learn 关闭。

### 6.3 实施任务

#### M3-T1：site rules 模板化

定义站点规则结构，至少包含：

- domain pattern。
- path pattern。
- apply_to：HLS、DASH、direct、page。
- required headers：Referer、Origin、User-Agent、Accept-Language。
- cookie_policy：disabled、temporary、prompt。
- authorization_policy：disabled、prompt。
- priority。
- enabled。

#### M3-T2：规则应用顺序

规则应用顺序建议：

1. 用户手动 headers。
2. 临时 Cookie 授权。
3. site rules 显式规则。
4. 自动学习的非敏感规则。
5. [`DownloadManager._normalize_task_headers_for_download()`](core/download/manager.py:1045) 兜底。

不得让自动学习覆盖用户显式输入。

#### M3-T3：Cookie 临时授权

Cookie 只做临时授权，不默认写入规则：

- UI 增加“本次下载使用当前站点 Cookie”确认。
- Cookie 仅在当前任务或当前会话中使用。
- Cookie 只能转发给同站或规则明确匹配的域。
- Cookie 不进入普通日志。
- Cookie 不参与自动学习。

#### M3-T4：Authorization 显式 opt-in

默认继续不允许 Authorization，因为 [`FORWARDABLE_HEADER_ALLOWLIST`](utils/headers.py:65) 当前排除了 Authorization。若后续支持：

- 只允许 site rule 显式开启。
- UI 明确风险。
- 不跨域。
- 不落盘，或仅加密临时存储。
- 日志强制脱敏。

#### M3-T5：auth retry 与 site rules 结合

当 [`classify_failure()`](core/download/classifier.py:65) 返回 auth 后，在 [`DownloadManager._execute_download()`](core/download/manager.py:1421) 中：

1. 应用 site rules。
2. 若 headers 有变化，进行 auth retry。
3. 若仍失败，提示需要 Cookie 或登录态。
4. 记录 `event=auth_retry_success` 或 `event=auth_retry_failed`。

### 6.4 验收标准

- Referer/Origin 规则能自动修复防盗链样本。
- Cookie 登录态样本在用户授权后成功。
- Cookie 不写入普通配置和日志。
- Authorization 默认仍不转发。
- auth retry 成功可被 metrics 统计。
- 新增 `scripts/smoke_auth_retry_site_rules.py` 通过。

### 6.5 风险与回滚

风险：敏感凭据泄露、规则污染。  
控制：默认不学习 Cookie/Authorization；规则可禁用；新增规则有最大数量限制；保留清除学习规则入口。

## 7. M4：`403`、`429` 失败分类和策略化重试

### 7.1 目标

让系统能区分防盗链、登录态、限流、超时、解析失败，按原因执行不同恢复策略。

### 7.2 实施任务

#### M4-T1：扩展失败分类

增强 [`classify_failure()`](core/download/classifier.py:65)：

| 分类 | 关键词 | 策略 |
|---|---|---|
| auth | `401`、`403`、`Forbidden`、`Unauthorized` | 应用 site rules、补 headers、临时 Cookie 提示 |
| rate_limit | `429`、`Too Many Requests`、`rate limit` | 降并发、增加 backoff |
| timeout | `timeout`、`timed out`、`connection reset` | 重试、低并发、低连接 |
| parse | `no formats`、`signature`、`nsig`、`parse` | 尝试 yt-dlp 或提示更新组件 |
| expired | `expired`、`signature expired`、`token expired` | 提示重新嗅探 |
| drm | `DRM`、`Widevine`、`license`、`encrypted media` | 直接提示不支持 |
| geo | `geo restricted`、`region`、`not available in your country` | 提示代理或地域限制 |
| tls | `certificate`、`SSL`、`TLS` | TLS 提示或受控 fallback |

#### M4-T2：扩展失败阶段识别

增强 [`detect_failure_stage()`](core/download/classifier.py:171)：

- sniff。
- probe。
- parse。
- engine_start。
- segment_download。
- merge。
- postprocess。
- disk。

#### M4-T3：按失败类型控制 retry

在 [`DownloadManager._execute_download()`](core/download/manager.py:1421)：

- auth：先补 headers/site rules，再 retry。
- rate_limit：降并发或低连接，并增加 backoff。
- timeout：允许 retry，但限制最大次数。
- parse：换 yt-dlp 或提示更新。
- expired：不反复 retry，提示重新嗅探。
- drm：不 retry，不 fallback。

#### M4-T4：用户提示结构化

对 UI 输出建议：

- auth：需要登录态或 Cookie。
- rate_limit：源站限流，已尝试低并发。
- segment_noise：检测到分片，建议选择 playlist。
- expired：链接已过期，请刷新页面重新嗅探。
- drm：DRM 内容不支持下载。

### 7.3 验收标准

- `403` 不再落入 unknown。
- `429` 可触发 rate_limit 策略。
- DRM 不做无效重试。
- expired URL 提示重新嗅探。
- unknown 失败占比下降。
- 新增 `scripts/smoke_rate_limit_strategy.py` 通过。

### 7.4 风险与回滚

风险：分类误判导致不该 retry 的 retry 或该 retry 的不 retry。  
控制：保留 fallback 到 unknown；策略开关可配置；日志记录原始失败摘要。

## 8. M5：N_m3u8DL-RE 与 HLS probe 自适应

### 8.1 目标

针对 CDN 限流和弱网提升 HLS 下载稳定性，减少 `429` 和 probe 误判。

### 8.2 实施任务

#### M5-T1：N_m3u8DL-RE 并发自适应

增强 [`N_m3u8DL_RE_Engine._auto_thread_count()`](engines/n_m3u8dl_re.py:938)：

- 根据分辨率、估计大小、历史站点限流情况选择初始并发。
- 对未知分辨率不要直接使用过低或过高默认值。
- 对同站历史 `429` 站点默认低并发。

#### M5-T2：`429` 后低并发重试

复用并增强 [`N_m3u8DL_RE_Engine._should_retry_low_concurrency()`](engines/n_m3u8dl_re.py:919)：

- `429` 后降到 1 或 2 线程。
- timeout/reset 后降到低并发并增加 retry。
- `403` 先走 auth/site rules，不优先降并发。

#### M5-T3：Aria2 低连接策略

增强 [`Aria2Engine._should_retry_low_connection()`](engines/aria2_engine.py:244)：

- `429` 降低 `max_connection_per_server` 和 `split`。
- timeout/reset 允许低连接 retry。
- auth 类错误不盲目低连接 retry。

#### M5-T4：HLS probe 诊断与小流量探测增强

二次复核确认 [`DownloadManager._execute_download()`](core/download/manager.py:1421) 当前已经只让 SSRF/security 类 probe 失败提前终止，普通 playlist、key、segment HTTP 失败会继续进入真实引擎。因此这里不再把“允许 probe 失败后继续下载”作为主要改动，而是增强 [`HLSProbe.probe()`](core/services/hls_probe.py:40) 和 [`HLSProbe._is_soft_segment_failure()`](core/services/hls_probe.py:401)：

- 保持 segment `403`、`429` 作为 soft fail 诊断，不提前阻断真实引擎。
- 评估 key `429` 是否标记为 soft fail，同时记录 key 阶段诊断。
- 对 key `404`、playlist `404` 继续 hard fail 或强提示。
- segment 探测优先 Range 小范围请求，减少触发防盗链或限流。
- 输出 playlist、key、segment 各阶段的结构化 probe 结果，供 [`DownloadManager._record_metric()`](core/download/manager.py:1172) 统计。

#### M5-T5：fallback 事件可观测

在 [`N_m3u8DL_RE_Engine.download()`](engines/n_m3u8dl_re.py:163) 中记录：

- primary 失败。
- master fallback。
- media fallback。
- safe mode retry。
- low concurrency retry。
- fallback 成功或失败。

### 8.3 验收标准

- segment `429` 不阻断下载。
- `429` 后可自动低并发重试。
- playlist `404` 仍明确失败。
- HLS 失败能定位到 playlist、key、segment 或 engine 阶段。
- [`scripts/smoke_hls_probe_soft_fail.py`](scripts/smoke_hls_probe_soft_fail.py:1) 继续通过。

### 8.4 风险与回滚

风险：过度 soft fail 放过坏链接，导致后续下载时间变长。  
控制：soft fail 只是不提前终止，最终下载失败仍分类明确；通过配置开关控制扩展 soft fail。

## 9. M6：yt-dlp 与 Streamlink 专项增强

### 9.1 yt-dlp 任务

1. 扩展 [`COOKIE_DOMAIN_MAP`](engines/ytdlp_engine.py:27)，覆盖更多用户常用站点。
2. 在 [`YtdlpEngine._diagnose_failure()`](engines/ytdlp_engine.py:724) 中识别 login required、bot check、no formats、nsig、extractor outdated。
3. 在 [`YtdlpEngine._build_command()`](engines/ytdlp_engine.py:567) 中支持用户选择 Chrome、Edge、Firefox browser cookies。
4. [`YtdlpEngine.get_formats()`](engines/ytdlp_engine.py:760) 失败时走同一诊断链路。

### 9.2 Streamlink 任务

1. 在 [`StreamlinkEngine.download()`](engines/streamlink_engine.py:36) 增加短暂网络失败 retry。
2. 在 [`StreamlinkEngine._build_command()`](engines/streamlink_engine.py:180) 增加质量 fallback：best → 720p → 480p。
3. 在 [`StreamlinkEngine._diagnose_failure()`](engines/streamlink_engine.py:113) 识别 offline、subscriber-only、geo、login required。
4. 对直播 HLS URL 可 fallback 到 N_m3u8DL-RE，对直播页面可 fallback 到 yt-dlp。

### 9.3 验收标准

- yt-dlp 登录态缺失有明确提示。
- no formats 能提示更新组件或 Cookie。
- Streamlink 短暂断线可重试。
- best 质量失败可尝试次优质量。

### 9.4 风险与回滚

风险：页面类和直播站点差异大，专项优化容易误判。  
控制：放在 P2，基于真实样本逐步扩展；每个站点规则可独立关闭。

## 10. 配置设计建议

建议新增或整理以下配置项，统一放入 [`config.json`](config.json:1) 的 features 或独立命名空间。

```json
{
  "features": {
    "segment_suppression_enabled": true,
    "segment_advanced_view_enabled": false,
    "enhanced_header_capture_enabled": true,
    "temporary_cookie_forwarding_enabled": false,
    "authorization_forwarding_enabled": false,
    "failure_strategy_enabled": true,
    "rate_limit_low_concurrency_enabled": true,
    "hls_probe_extended_soft_fail_enabled": true
  },
  "site_rules_auto": {
    "enabled": false,
    "allow_cookie": false,
    "allow_authorization": false,
    "learn_non_sensitive_headers": true,
    "max_rules": 50
  }
}
```

注意：上述是设计建议，不要求一次性修改全部配置。

## 11. 测试计划

### 11.1 单元测试

| 测试文件 | 覆盖点 |
|---|---|
| [`tests/test_download_context.py`](tests/test_download_context.py:1) | segment、HLS、direct resource type 推断 |
| [`tests/test_engine_selector.py`](tests/test_engine_selector.py:1) | segment 不误路由、MIME 优先 |
| [`tests/test_headers.py`](tests/test_headers.py:1) | header sanitize、Cookie/Authorization 策略 |
| [`tests/test_hls_probe.py`](tests/test_hls_probe.py:1) | probe soft/hard fail |
| [`tests/test_sniffer_merge.py`](tests/test_sniffer_merge.py:1) | headers merge、candidate score |
| [`tests/test_download_manager_state_machine.py`](tests/test_download_manager_state_machine.py:1) | retry、fallback、failure strategy |

### 11.2 Smoke 测试

已有脚本：

- [`scripts/smoke_backoff_retry.py`](scripts/smoke_backoff_retry.py:1)
- [`scripts/smoke_hls_probe_soft_fail.py`](scripts/smoke_hls_probe_soft_fail.py:1)
- [`scripts/smoke_engine_select_mime.py`](scripts/smoke_engine_select_mime.py:1)
- [`scripts/smoke_catcatch_auth.py`](scripts/smoke_catcatch_auth.py:1)

建议新增：

- `scripts/smoke_segment_suppression.py`
- `scripts/smoke_header_forwarding.py`
- `scripts/smoke_auth_retry_site_rules.py`
- `scripts/smoke_rate_limit_strategy.py`

### 11.3 人工验收样本

| 样本类型 | 期望 |
|---|---|
| 公开 HLS | 直接成功 |
| Referer 防盗链 HLS | 自动补 Referer/Origin 后成功 |
| Cookie 登录态视频 | 用户授权 Cookie 后成功 |
| `429` HLS | 降并发或 soft fail 后继续尝试 |
| segment 密集页面 | 默认折叠 segment，优先展示 playlist |
| DRM 内容 | 明确提示不支持，不反复 retry |
| expired URL | 提示重新嗅探 |

## 12. 指标验收目标

由于当前没有足够日志样本，不能承诺固定百分比。建议以阶段性指标为目标：

| 指标 | 目标 |
|---|---|
| segment 噪声展示量 | 显著下降 |
| auth 类失败 unknown 占比 | 显著下降 |
| `403` 后 auth retry 成功数 | 可观测增加 |
| `429` 后低并发恢复数 | 可观测增加 |
| 用户看到的失败原因明确率 | 提升 |
| DRM、expired 无效 retry | 下降 |
| 综合下载成功率 | 在防盗链、登录态、`403`、`429`、segment 误捕获样本中提升 |

推荐每个里程碑后使用 [`scripts/s5_compare_metrics.py`](scripts/s5_compare_metrics.py:69) 对 baseline 和 candidate 日志生成对比报告。

## 13. 回滚与灰度策略

每个高风险能力都需要配置开关：

| 能力 | 默认 | 回滚方式 |
|---|---:|---|
| segment suppression | 开启 | 关闭 `segment_suppression_enabled` |
| enhanced header capture | 开启 | 关闭 `enhanced_header_capture_enabled` |
| temporary Cookie forwarding | 关闭 | 不授权 Cookie 或关闭功能 |
| Authorization forwarding | 关闭 | 保持关闭 |
| failure strategy | 开启 | 关闭 `failure_strategy_enabled` 回到旧 retry |
| HLS extended soft fail | 开启或灰度 | 关闭 `hls_probe_extended_soft_fail_enabled` |
| low concurrency retry | 开启 | 关闭 `rate_limit_low_concurrency_enabled` |

灰度建议：

1. 先在 smoke 和本地样本启用。
2. 再对非敏感能力默认开启，例如 segment suppression、非敏感 headers。
3. Cookie、Authorization 继续默认关闭，仅用户显式授权。
4. 收集指标后再扩大范围。

## 14. 推荐开发拆分

### PR-1：指标与测试基线

- 扩展 metrics。
- 扩展 S5 脚本。
- 新增 smoke skeleton。
- 不改变核心下载行为。

### PR-2：segment 降噪

- 强化 [`infer_resource_type()`](core/download_context.py:108)。
- 修改 [`M3U8Sniffer.add_resource()`](core/m3u8_sniffer.py:43) 的 segment 入库策略。
- 修改 UI 折叠展示。
- 调整 [`select_engine()`](core/engine_selector.py:343) 对 segment 的处理。

### PR-3：headers 捕获增强

- 扩展 [`NetworkInterceptor.interceptRequest()`](core/request_interceptor.py:29)。
- 统一 [`sanitize_headers()`](utils/headers.py:111)。
- 下载前增强 [`DownloadManager._normalize_task_headers_for_download()`](core/download/manager.py:1045)。
- 增加 header forwarding smoke。

### PR-4：site rules 与 Cookie 临时授权

- 完善 [`DownloadManager._apply_site_rules_to_task()`](core/download/manager.py:1015)。
- 限制 [`DownloadManager._learn_site_rule_from_task()`](core/download/manager.py:1211) 只学习非敏感 headers。
- 增加 Cookie 临时授权 UI 和任务字段。
- 增加 auth retry smoke。

### PR-5：失败分类与策略化 retry

- 扩展 [`classify_failure()`](core/download/classifier.py:65)。
- 扩展 [`detect_failure_stage()`](core/download/classifier.py:171)。
- 改造 [`DownloadManager._execute_download()`](core/download/manager.py:1421) 的 retry/fallback 决策。
- 增加 `429` strategy smoke。

### PR-6：N_m3u8DL-RE、Aria2、HLS probe 自适应

- 改造 [`N_m3u8DL_RE_Engine._auto_thread_count()`](engines/n_m3u8dl_re.py:938)。
- 改造 [`N_m3u8DL_RE_Engine._should_retry_low_concurrency()`](engines/n_m3u8dl_re.py:919)。
- 改造 [`Aria2Engine._should_retry_low_connection()`](engines/aria2_engine.py:244)。
- 扩展 [`HLSProbe._is_soft_segment_failure()`](core/services/hls_probe.py:401)。

### PR-7：yt-dlp 和 Streamlink 专项

- 扩展 [`COOKIE_DOMAIN_MAP`](engines/ytdlp_engine.py:27)。
- 增强 [`YtdlpEngine._diagnose_failure()`](engines/ytdlp_engine.py:724)。
- 增强 [`StreamlinkEngine.download()`](engines/streamlink_engine.py:36)。
- 增强 [`StreamlinkEngine._diagnose_failure()`](engines/streamlink_engine.py:113)。

## 15. 最终优先级建议

结合已确认失败主要来自防盗链、登录态、`403`、`429`、segment 误捕获，推荐最小高收益交付范围为：

1. M0 指标和测试基线。
2. M1 segment 降噪和 playlist 优先。
3. M2 非敏感 headers 捕获增强。
4. M3 site rules 与临时 Cookie 授权。
5. M4 `403`、`429` 失败分类和策略化重试。

这五项是最值得优先做的核心优化。M5、M6 作为第二批增强，在核心失败率下降后继续推进。

## 16. 完成定义

当以下条件同时满足，可认为本轮成功率优化完成：

- segment 误捕获不再作为主资源干扰用户下载。
- 防盗链样本可通过 Referer、Origin、site rules 自动恢复。
- 登录态样本可通过用户授权 Cookie 临时下载成功。
- `403`、`429` 能被准确分类并触发不同策略。
- `429` 可触发低并发或低连接重试。
- 日志和指标能说明成功率提升来自哪个环节。
- Cookie、Authorization、token 不出现在普通日志。
- 所有新增 smoke 和相关单元测试通过。

## 17. 二次可行性论证、漏洞修正与函数级细化

本节是对前文计划的二次复核结果，并把实施粒度细化到函数级。若前文存在表述过宽或假设不准确，以本节修正为准。

### 17.1 总体可行性结论

| 方向 | 可行性 | 结论 | 关键约束 |
|---|---:|---|---|
| segment 降噪 | 高 | 可做，且收益高 | 不能删除 `.ts` 能力，只能基于 [`infer_resource_type()`](core/download_context.py:108) 和 UI 策略降权/折叠 |
| 防盗链 header 补齐 | 高 | 可做，优先统一现有 Playwright、CatCatch、手动任务链路 | 必须走 [`sanitize_headers()`](utils/headers.py:111) 和策略开关 |
| 登录态 Cookie | 中高 | 可做，但必须临时授权、同站约束、不自动学习 | [`PlaywrightDriver._build_default_headers()`](core/playwright_driver.py:1079) 已有 Cookie 合并雏形，需受控化 |
| Authorization 支持 | 中 | 可做但不建议默认开启 | [`FORWARDABLE_HEADER_ALLOWLIST`](utils/headers.py:65) 当前不含 Authorization，应仅 site rule 显式 opt-in |
| `403`、`429` 策略化 retry | 高 | 可做，收益中高 | 需要把 [`DownloadManager._execute_download()`](core/download/manager.py:1421) 内部大循环拆成小决策函数 |
| HLS probe 自适应 | 中高 | 可做，但不是“放宽所有 hard fail” | 当前 [`DownloadManager._execute_download()`](core/download/manager.py:1421) 已 soft-allow 非 security probe 失败，应聚焦诊断和 Range 探测 |
| yt-dlp/Streamlink 专项 | 中 | 可做，但放在第二批 | 站点差异大，应基于真实失败样本增量扩展 |

### 17.2 原计划漏洞与修正清单

| 编号 | 原计划漏洞或不准确点 | 代码依据 | 修正方案 |
|---|---|---|---|
| L1 | 把“内置浏览器只能传 URL、Referer、User-Agent”说得过于笼统 | [`PlaywrightDriver._emit_detected_resource()`](core/playwright_driver.py:1138) 已发结构化上下文；[`NetworkInterceptor.video_detected`](core/request_interceptor.py:17) 才是旧三元组路径 | M2 区分 Playwright 路径与 Qt WebEngine 路径，优先统一 Playwright payload，Qt 路径只补非敏感字段 |
| L2 | 直接改 [`NetworkInterceptor.video_detected`](core/request_interceptor.py:17) 签名会破坏兼容 | [`BrowserView._on_resource_detected()`](ui/browser_view.py:211) 仍保留旧兼容回调 | 不改旧信号签名；新增结构化 signal 或 adapter，旧信号继续 emit |
| L3 | [`NetworkInterceptor.interceptRequest()`](core/request_interceptor.py:29) 用 `sniffer_rules_enabled` 控制是否 emit，语义不准确 | 当前 rules 开关会影响视频资源上报 | 拆成“是否启用 rules”和“是否启用资源上报”两个判断，避免关闭 rules 后完全不嗅探 |
| L4 | Cookie 策略表述需要更精确 | [`FORWARDABLE_HEADER_ALLOWLIST`](utils/headers.py:65) 已含 Cookie；历史记录和日志又会剔除敏感字段 | 允许“本次任务/本会话/同站”的 Cookie 临时转发，但禁止默认持久化和自动学习 |
| L5 | Authorization 不能直接加入默认转发链路 | [`sanitize_headers()`](utils/headers.py:111) 当前不会允许 Authorization | 新增 policy-aware 清洗；Authorization 仅 site rule 显式开启，且默认关闭 |
| L6 | segment 折叠需要模型和 UI 字段，原计划没有明确 | [`M3U8Resource`](core/task_model.py:40) 当前没有 suppression/group 字段；[`ResourcePanel.add_resource()`](ui/resource_panel.py:256) 会直接插入行 | 在模型或 UI 状态中增加 suppression、reason、group key、count，再接入过滤展示 |
| L7 | HLS probe 计划重复了已有 soft-allow 能力 | [`DownloadManager._execute_download()`](core/download/manager.py:1421) 当前只把 SSRF/security probe 失败作为提前终止条件 | M5 改为 probe 诊断、小 Range 请求、key/segment 阶段分类和 metrics |
| L8 | `.ts` 不能从引擎规则中一刀切移除 | [`resources/engine_rules.json`](resources/engine_rules.json:4) 的 direct extensions 含 `.ts`，但真实完整 TS 文件仍可能需要下载 | 保留 `.ts`，通过 [`EngineSelectContext`](core/download_context.py:61) 和用户确认控制 segment 直链下载 |
| L9 | site rules 当前 page context 可能丢失 | [`DownloadManager._apply_site_rules_to_task()`](core/download/manager.py:1015) 只用 headers 中 referer 当 page_url | 改为优先使用任务 page_url，再兜底 referer |
| L10 | auth retry 当前可能无效重复 | [`DownloadManager._execute_download()`](core/download/manager.py:1421) auth 分支未强依赖 site rules 是否真的 changed | 只有 headers 变化或用户授权 Cookie 后才做 auth retry；否则提示登录态/规则缺失 |
| L11 | metrics 口径尚不足以证明收益 | [`DownloadManager._record_metric()`](core/download/manager.py:1172) 当前只聚合 success/failed/by_engine/by_stage | 增加 by_reason、strategy_action、retry_result，脚本同步解析 |
| L12 | Playwright Cookie 合并需要受控化 | [`PlaywrightDriver._build_default_headers()`](core/playwright_driver.py:1079) 目前 `.m3u8` 缺 Cookie 时会尝试合并 context cookies | 增加 feature flag、same-site 判断、授权状态或非持久化标记，并统一清洗 |

### 17.3 函数级实施拆解：M0 指标和基线

| 函数或文件 | 修改点 | 验收 |
|---|---|---|
| [`DownloadManager._record_metric()`](core/download/manager.py:1172) | 增加可选 reason、strategy、retry_action、probe_stage 维度；保持旧调用兼容 | 旧 metrics 不丢，新 metrics 可统计 auth/rate_limit/segment_noise |
| [`DownloadManager.get_quality_metrics()`](core/download/manager.py:2346) | 返回 by_reason、by_strategy、probe_soft_fail_then_success | UI 或脚本能读取新增聚合 |
| [`classify_failure()`](core/download/classifier.py:65) | 输出 auth、rate_limit、timeout、parse、expired、drm、geo、tls、segment_noise | `403` 和 `429` 不再落入 unknown |
| [`detect_failure_stage()`](core/download/classifier.py:171) | 补 sniff、probe、parse、engine_start、segment_download、merge、postprocess、disk | 失败阶段统计可解释 |
| [`scan_log()`](scripts/s5_metrics_from_logs.py:17) | 增加新 event pattern | 单日志可统计新指标 |
| [`scan_log()`](scripts/s5_compare_metrics.py:17) | baseline/candidate 都支持新指标 | 对比报告能显示优化前后差异 |
| [`tests/test_engine_selector.py`](tests/test_engine_selector.py:1) | 增加指标不应影响引擎选择的回归测试 | 现有引擎选择行为不退化 |

### 17.4 函数级实施拆解：M1 segment 降噪

| 函数或文件 | 修改点 | 验收 |
|---|---|---|
| [`infer_resource_type()`](core/download_context.py:108) | 扩展 `.aac`、`.key`、序号型 chunk、init segment、query suffix 的 segment 识别；保留 HLS/DASH MIME 优先 | [`tests/test_download_context.py`](tests/test_download_context.py:52) 覆盖新增样例 |
| [`_path_suffix()`](core/download_context.py:236) | 保证 query、fragment、大小写、编码路径下的后缀提取稳定 | 带 token query 的 segment 可识别 |
| [`EngineSelectContext`](core/download_context.py:61) | 保持 frozen 结构不破坏；如需 segment_group 先放 M3U8Resource/UI，不强改 context | [`TestEngineSelectContextDataclass`](tests/test_download_context.py:256) 继续通过 |
| [`M3U8Resource`](core/task_model.py:40) | 增加可选 suppression 状态：是否隐藏、原因、group key、group count；默认值保持兼容 | 旧反序列化和旧创建代码无需传新参数 |
| [`M3U8Sniffer.add_resource()`](core/m3u8_sniffer.py:43) | 在 build context 后识别 segment；计算 group key；若同页已有 playlist 或短时间高频 segment，则标记 suppressed/grouped | 连续 segment 不再作为主资源刷屏 |
| [`M3U8Sniffer._merge_resource_context()`](core/m3u8_sniffer.py:205) | 合并 duplicated segment 的 group count、headers 完整度、master/media URL | 同一资源重复捕获不会丢上下文 |
| [`M3U8Sniffer._score_m3u8_candidate()`](core/m3u8_sniffer.py:378) | playlist 加权、segment 降权、headers 完整度加权；避免 segment candidate_score 为默认中性值 | playlist 排名高于同页 segment |
| [`M3U8Sniffer.is_video_resource()`](core/m3u8_sniffer.py:286) | 与 [`infer_resource_type()`](core/download_context.py:108) 保持一致，明确 segment 与 full video 的边界 | 嗅探筛选和下载路由不冲突 |
| [`select_engine()`](core/engine_selector.py:343) | resource_type 为 segment 时不直接因 `.ts` 优先 Aria2；若有 master/media URL 优先 playlist | segment 不误路由成主下载 |
| [`EngineSelector.get_candidates()`](core/engine_selector.py:468) | 候选列表中为 segment 加低优先级或需要用户确认标记 | 自动模式不会首选单分片 |
| [`ResourcePanel.add_resource()`](ui/resource_panel.py:256) | 插入前检查 suppressed；默认隐藏或折叠，仍维护计数和高级开关 | UI 不刷屏，但可显示隐藏计数 |
| [`ResourcePanel._get_resource_display_type()`](ui/resource_panel.py:748) | canonical segment 显示为 Segment 或折叠组，不误显示为 Video Stream | 用户能区分 playlist 与 segment |
| [`ResourcePanel._generate_dedup_key()`](ui/resource_panel.py:988) | 对 segment 使用 group key，避免每个分片一行 | 同 pattern segment 合并 |
| [`ResourcePanel._apply_filters()`](ui/resource_panel.py:1090) | 增加“显示分片资源”过滤条件 | 高级入口可恢复显示 segment |
| [`MainWindowSniffFlowMixin._on_resource_found()`](ui/main_window_sniff_flow.py:88) | 对 suppressed resource 只更新面板计数和日志，不自动提示主资源 | 主流程不被 segment 噪声打断 |

### 17.5 函数级实施拆解：M2 headers 捕获与传递

| 函数或文件 | 修改点 | 验收 |
|---|---|---|
| [`PlaywrightDriver._build_default_headers()`](core/playwright_driver.py:1079) | 保留 Referer、Origin、UA 兜底；Cookie 合并改为受 feature flag、same-site、临时授权或明确规则约束 | Cookie 不越权、不落普通日志 |
| [`PlaywrightDriver._emit_detected_resource()`](core/playwright_driver.py:1138) | emit 前统一调用 header policy 清洗；payload 保留 resource_type、mime、master_url、media_url | [`BrowserView._on_resource_context_detected()`](ui/browser_view.py:189) 收到干净上下文 |
| [`PlaywrightDriver._is_video_url()`](core/playwright_driver.py:1192) | 对 segment 关键词捕获后不直接当 full video；交给 context 推断 | `/hls/chunk` 不再误成主视频 |
| [`NetworkInterceptor.interceptRequest()`](core/request_interceptor.py:29) | 新增结构化 context signal；旧 [`NetworkInterceptor.video_detected`](core/request_interceptor.py:17) 继续 emit；拆除 rules 开关对 emit 的误控 | Qt 路径兼容旧 UI，同时有新 payload |
| [`NetworkInterceptor._is_video_url()`](core/request_interceptor.py:59) | 把 `.ts`、`.m4s` 捕获结果标为 segment，不作为主视频强推 | Qt 路径 segment 噪声下降 |
| [`BrowserView._on_resource_context_detected()`](ui/browser_view.py:189) | 只负责转发受控 headers 和 context，不再自行补敏感字段 | 上游策略集中，UI 不持有策略 |
| [`BrowserView._on_resource_detected()`](ui/browser_view.py:211) | 旧链路 headers 也走清洗和默认 UA/Referer 兜底 | 兼容路径不绕过安全策略 |
| [`M3U8Sniffer.add_resource()`](core/m3u8_sniffer.py:43) | 入口统一清洗 headers；对 HLS/DASH 继续应用 site rules；segment 保留最小 headers | 内部浏览器与 CatCatch 行为一致 |
| [`sanitize_headers()`](utils/headers.py:111) | 保持现有签名兼容；如需敏感字段，新增 keyword-only policy 或新 helper | 旧调用不破坏 |
| [`normalized_forward_headers()`](utils/headers.py:300) | 统一大小写和 allowlist 结果，供任务 headers 存储 | 引擎前 headers 一致 |
| [`iter_engine_headers()`](utils/headers.py:321) | 根据 include_cookie、include_range、policy 控制引擎参数 | N_m3u8DL-RE、Aria2、yt-dlp 行为一致 |
| [`MainWindowSniffFlowMixin._resource_context_kwargs()`](ui/main_window_sniff_flow.py:646) | 下载任务创建时带上 resource_type、mime、master_url、media_url、candidate score | 下载阶段保留嗅探上下文 |
| [`MainWindowSniffFlowMixin._start_download()`](ui/main_window_sniff_flow.py:658) | 传入清洗后的 headers 和临时授权状态 | 手动下载不丢 headers |

### 17.6 函数级实施拆解：M3 site rules、临时 Cookie、登录态

| 函数或文件 | 修改点 | 验收 |
|---|---|---|
| [`site_rule_matches()`](core/site_rule_utils.py:40) | 增加 enabled、priority、path pattern、apply_to、resource_type 条件；保留 domains/url_keywords 兼容 | 老规则继续生效，新规则可精确匹配 |
| [`set_header_if_missing()`](core/site_rule_utils.py:68) | 对敏感字段调用 policy 判断；不覆盖用户显式 headers | 用户输入优先 |
| [`M3U8Sniffer._apply_site_rules()`](core/m3u8_sniffer.py:357) | 仅对匹配 resource_type 的规则注入 headers；Cookie/Authorization 需 policy 允许 | 资源入库阶段不越权注入 |
| [`DownloadManager._apply_site_rules_to_task()`](core/download/manager.py:1015) | page_url 改为任务 page_url 优先、referer 兜底；返回 changed；记录 matched rule id | auth retry 只在 headers 变化后触发 |
| [`DownloadManager._normalize_task_headers_for_download()`](core/download/manager.py:1045) | 保留 UA/Origin 兜底，但不覆盖 site rules 和用户 headers | 下载前 headers 稳定 |
| [`DownloadManager._learn_site_rule_from_task()`](core/download/manager.py:1211) | 自动学习只允许非敏感 headers；即使配置历史存在 allow_cookie，也默认忽略 Cookie 或迁移为 false | Cookie 不自动写入规则 |
| [`M3U8Resource`](core/task_model.py:40) | 若采用模型承载，增加 temporary_cookie_allowed、auth_policy_source 之类的可选字段；默认关闭 | 资源到任务可追踪授权来源 |
| [`DownloadTask`](core/task_model.py:215) | 增加可选 auth policy/runtime flags；不序列化敏感值 | retry 策略能知道是否允许 Cookie |
| [`PlaywrightDriver.export_cookies_to_file()`](core/playwright_driver.py:1419) | 继续作为用户显式导出入口；不作为后台默认动作 | Cookie 文件导出仍需用户触发 |
| [`YtdlpEngine._build_command()`](engines/ytdlp_engine.py:567) | 仅在任务明确提供 cookie file 或 browser cookie 选项时使用 | yt-dlp 登录态路径明确 |

### 17.7 函数级实施拆解：M4 失败分类与策略化 retry

| 函数或文件 | 修改点 | 验收 |
|---|---|---|
| [`classify_failure()`](core/download/classifier.py:65) | 扩展 auth、rate_limit、timeout、parse、expired、drm、geo、tls；优先使用 structured error code，再用关键词 | `403`、`429`、DRM、expired 分类稳定 |
| [`classify_message_keywords()`](core/download/classifier.py:153) | 中英文关键词分组，避免 `403` 全落 unknown | 日志和 UI 提示更准确 |
| [`detect_failure_stage()`](core/download/classifier.py:171) | 增加 segment_download、merge、disk、probe、engine_start | 失败阶段可观测 |
| [`DownloadManager._classify_failure()`](core/download/manager.py:991) | 保持 facade；将 task 上下文传给 classifier | 后续策略可基于 task 类型判断 |
| [`DownloadManager._execute_download()`](core/download/manager.py:1421) | 拆分 retry 决策，避免继续堆大函数；auth、rate_limit、timeout、parse、expired、drm 走不同分支 | 重试次数下降，恢复成功可统计 |
| [`DownloadManager._record_metric()`](core/download/manager.py:1172) | 在每次失败分类后记录 reason/stage/action | metrics 能解释策略效果 |
| [`DownloadTask.transition()`](core/task_model.py:364) | 不改变状态机；只在 task runtime 字段记录 last_failure_kind/stage | 状态转换不被策略改坏 |
| [`TaskSnapshot.from_task()`](core/task_model.py:594) | 若新增可展示 failure_kind，纳入 snapshot；敏感字段不纳入 | UI 可显示结构化原因 |
| [`scripts/smoke_rate_limit_strategy.py`](scripts/smoke_rate_limit_strategy.py:1) | 新增或补齐 `429` 场景 | rate_limit 策略可回归验证 |

建议在 [`DownloadManager`](core/download/manager.py:247) 内新增小函数承接策略逻辑，避免继续膨胀 [`DownloadManager._execute_download()`](core/download/manager.py:1421)：

| 新增函数位置 | 职责 |
|---|---|
| [`DownloadManager`](core/download/manager.py:247) 内部 retry policy helper | 根据 failure_kind、stage、engine、attempt 计算 action |
| [`DownloadManager`](core/download/manager.py:247) 内部 auth failure helper | 应用 site rules、判断 headers 是否 changed、决定是否 auth retry |
| [`DownloadManager`](core/download/manager.py:247) 内部 rate limit helper | 计算 backoff、是否低并发/低连接、是否切换引擎 |
| [`DownloadManager`](core/download/manager.py:247) 内部 fallback helper | 判断当前失败是否值得尝试下一个 engine candidate |

### 17.8 函数级实施拆解：M5 引擎、probe、自适应

| 函数或文件 | 修改点 | 验收 |
|---|---|---|
| [`HLSProbe.probe()`](core/services/hls_probe.py:40) | 返回 playlist/key/segment 阶段结构化诊断；segment 探测优先小 Range | probe 不误阻断真实下载，诊断更清楚 |
| [`HLSProbe._is_soft_segment_failure()`](core/services/hls_probe.py:401) | 保持 segment `403`、`429` soft；谨慎评估 key `429` | [`scripts/smoke_hls_probe_soft_fail.py`](scripts/smoke_hls_probe_soft_fail.py:1) 继续通过 |
| [`HLSProbe._pick_key_url()`](core/services/hls_probe.py:425) | key URL 单独分类，避免与 segment 混淆 | key 失败提示更准确 |
| [`HLSProbe._pick_first_segment()`](core/services/hls_probe.py:432) | 配合 Range 探测和 soft fail 事件 | 限流站点误伤减少 |
| [`N_m3u8DL_RE_Engine.download()`](engines/n_m3u8dl_re.py:163) | 输出 primary/master/media/safe/low-concurrency 各阶段事件 | fallback 成功可统计 |
| [`N_m3u8DL_RE_Engine._should_retry_low_concurrency()`](engines/n_m3u8dl_re.py:919) | `429`、timeout/reset 低并发；auth 类不优先低并发 | `403` 不被误判为限流 |
| [`N_m3u8DL_RE_Engine._auto_thread_count()`](engines/n_m3u8dl_re.py:938) | 根据站点历史 rate_limit、分辨率、估算大小选择初始线程 | 降低 `429` 触发概率 |
| [`Aria2Engine._should_retry_low_connection()`](engines/aria2_engine.py:244) | `429` 降 split/connection；auth 不低连接盲试 | direct 下载更稳 |
| [`Aria2Engine._build_command()`](engines/aria2_engine.py:92) | 接收低连接参数和 headers policy 结果 | retry 参数可控 |

### 17.9 函数级实施拆解：M6 yt-dlp 与 Streamlink

| 函数或文件 | 修改点 | 验收 |
|---|---|---|
| [`YtdlpEngine._diagnose_failure()`](engines/ytdlp_engine.py:724) | 增加 login required、bot check、no formats、nsig、extractor outdated、geo、DRM | 页面类失败提示明确 |
| [`YtdlpEngine._build_command()`](engines/ytdlp_engine.py:567) | Cookie file、browser cookies、impersonate、proxy 选项按用户授权注入 | 登录态下载不越权 |
| [`YtdlpEngine.get_formats()`](engines/ytdlp_engine.py:760) | 格式获取失败走同一诊断链路 | 格式弹窗失败原因明确 |
| [`StreamlinkEngine.download()`](engines/streamlink_engine.py:36) | 短暂网络失败 retry，auth/DRM 不盲试 | 直播短断恢复 |
| [`StreamlinkEngine._build_command()`](engines/streamlink_engine.py:180) | best 失败后可尝试 720p/480p 质量 fallback | 直播质量 fallback 可控 |
| [`StreamlinkEngine._diagnose_failure()`](engines/streamlink_engine.py:113) | offline、subscriber-only、geo、login required 分类 | 直播失败原因明确 |

### 17.10 调整后的实施顺序

1. PR-1：只做 metrics、classifier 测试和脚本，不改变核心行为。
2. PR-2：做 segment resource type、模型字段、UI 折叠和 engine selector 防误路由，全部挂 feature flag。
3. PR-3：统一 headers policy，先规范 Playwright、CatCatch、手动任务，再补 Qt 结构化 signal。
4. PR-4：site rules 精细匹配、临时 Cookie 授权、auth retry changed 检查。
5. PR-5：失败分类驱动的 retry/fallback 策略，把 [`DownloadManager._execute_download()`](core/download/manager.py:1421) 拆成可测试 helper。
6. PR-6：N_m3u8DL-RE、Aria2、HLS probe 自适应，基于 PR-5 的 failure_kind 行动。
7. PR-7：yt-dlp、Streamlink 专项，按真实站点样本逐步推进。

### 17.11 函数级测试映射

| 测试 | 覆盖函数 | 必须验证 |
|---|---|---|
| [`tests/test_download_context.py`](tests/test_download_context.py:1) | [`infer_resource_type()`](core/download_context.py:108)、[`_path_suffix()`](core/download_context.py:236) | `.ts`、`.m4s`、`.aac`、`.key`、init segment、query token |
| [`tests/test_engine_selector.py`](tests/test_engine_selector.py:1) | [`select_engine()`](core/engine_selector.py:343)、[`EngineSelector.get_candidates()`](core/engine_selector.py:468) | segment 不自动首选 Aria2，HLS context 仍首选 N_m3u8DL-RE |
| `tests/test_headers.py` | [`sanitize_headers()`](utils/headers.py:111)、[`iter_engine_headers()`](utils/headers.py:321) | Cookie policy、Authorization opt-in、Range/Accept-Language/Sec-Fetch |
| `tests/test_sniffer_merge.py` | [`M3U8Sniffer.add_resource()`](core/m3u8_sniffer.py:43)、[`M3U8Sniffer._merge_resource_context()`](core/m3u8_sniffer.py:205) | headers merge、segment group、playlist score |
| `tests/test_resource_panel_segment.py` | [`ResourcePanel.add_resource()`](ui/resource_panel.py:256)、[`ResourcePanel._apply_filters()`](ui/resource_panel.py:1090) | 默认隐藏 segment、高级开关显示、计数准确 |
| `tests/test_site_rules.py` | [`site_rule_matches()`](core/site_rule_utils.py:40)、[`DownloadManager._apply_site_rules_to_task()`](core/download/manager.py:1015) | page_url 兜底、priority、apply_to、changed 返回 |
| `tests/test_download_manager_strategy.py` | [`DownloadManager._execute_download()`](core/download/manager.py:1421)、[`classify_failure()`](core/download/classifier.py:65) | auth retry、rate_limit backoff、drm 不 retry、expired 提示刷新 |
| [`scripts/smoke_segment_suppression.py`](scripts/smoke_segment_suppression.py:1) | 嗅探到 UI 全链路 | segment 密集页面不刷主资源 |
| [`scripts/smoke_header_forwarding.py`](scripts/smoke_header_forwarding.py:1) | headers 捕获到引擎参数 | Referer、Origin、UA、Cookie policy 正确 |
| [`scripts/smoke_auth_retry_site_rules.py`](scripts/smoke_auth_retry_site_rules.py:1) | site rules + auth retry | `403` 后 headers changed 才重试，成功可统计 |
| [`scripts/smoke_rate_limit_strategy.py`](scripts/smoke_rate_limit_strategy.py:1) | failure strategy + engine fallback | `429` 后低并发/低连接/backoff 行为正确 |

### 17.12 最小可交付范围

若需要控制实施风险，最小高收益范围建议只做以下函数链路：

1. [`infer_resource_type()`](core/download_context.py:108) → [`M3U8Sniffer.add_resource()`](core/m3u8_sniffer.py:43) → [`ResourcePanel.add_resource()`](ui/resource_panel.py:256) → [`select_engine()`](core/engine_selector.py:343)：解决 segment 误捕获。
2. [`PlaywrightDriver._emit_detected_resource()`](core/playwright_driver.py:1138) → [`M3U8Sniffer._merge_resource_context()`](core/m3u8_sniffer.py:205) → [`DownloadManager._normalize_task_headers_for_download()`](core/download/manager.py:1045) → [`iter_engine_headers()`](utils/headers.py:321)：解决 Referer、Origin、UA、受控 Cookie 传递。
3. [`DownloadManager._apply_site_rules_to_task()`](core/download/manager.py:1015) → [`classify_failure()`](core/download/classifier.py:65) → [`DownloadManager._execute_download()`](core/download/manager.py:1421)：解决 `403`、登录态和 `429` 的策略化恢复。

这三条链路直接对应用户确认的主要失败来源，收益最高，且可以分 PR、可回滚、可测试。
