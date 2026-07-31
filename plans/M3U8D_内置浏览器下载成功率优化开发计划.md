# M3U8D 内置浏览器下载成功率优化开发计划

## 1. 背景与目标

本计划针对“内置浏览器抓取资源后交给下载引擎”的主路径进行增强，优先提升 raw HLS/DASH 链接在 N_m3u8DL-RE 下载时的成功率。

优化范围：

- 精确请求头复用。
- resource_url 域名 Cookie 优先匹配。
- 短期链接临下载前刷新或风险提示。
- 低并发自适应。
- DRM 与跨域 segment/key 诊断。

当前核心链路：

1. 内置浏览器由 [`PlaywrightDriver._setup_page()`](../core/playwright_driver.py:578) 注入嗅探与监听。
2. 网络请求由 [`PlaywrightDriver.handle_request()`](../core/playwright_driver.py:717) 和 [`PlaywrightDriver.handle_response()`](../core/playwright_driver.py:747) 捕获。
3. 资源统一由 [`PlaywrightDriver._emit_detected_resource()`](../core/playwright_driver.py:1146) 发出。
4. 请求头由 [`PlaywrightDriver._build_default_headers()`](../core/playwright_driver.py:1080) 补齐。
5. UI 在 [`MainWindowSniffFlowMixin._start_download()`](../ui/main_window_sniff_flow.py:701) 创建 [`DownloadTask`](../core/task_model.py:223)。
6. HLS/DASH 优先由 [`N_m3u8DL_RE_Engine.download()`](../engines/n_m3u8dl_re.py:354) 下载。
7. 下载前 Cookie 兜底由 [`N_m3u8DL_RE_Engine._prepare_cookie_header()`](../engines/n_m3u8dl_re.py:201) 完成。
8. 引擎参数由 [`N_m3u8DL_RE_Engine._build_command()`](../engines/n_m3u8dl_re.py:741) 和 [`N_m3u8DL_RE_Engine._append_headers()`](../engines/n_m3u8dl_re.py:897) 生成。

最终目标不是保证 100% 成功，而是在不引入浏览器中继下载的前提下，最大化复用真实浏览器态，减少 Cookie、Referer、Origin、User-Agent、短期 token、并发限流、跨域 key、DRM 造成的误判与失败。

---

## 2. 总体分阶段

### 阶段 0：补观测、补开关、保持行为可回退

目标：让后续优化可灰度、可验证、可快速关闭。

涉及文件：

- [`config.json`](../config.json)
- [`core/playwright_driver.py`](../core/playwright_driver.py)
- [`engines/n_m3u8dl_re.py`](../engines/n_m3u8dl_re.py)
- [`ui/main_window_sniff_flow.py`](../ui/main_window_sniff_flow.py)

建议配置项：

- features.exact_request_header_replay_enabled，默认启用。
- features.resource_domain_cookie_lookup_enabled，默认启用。
- features.ephemeral_m3u8_refresh_enabled，第一版默认关闭。
- features.playlist_diagnostics_enabled，默认启用。
- features.adaptive_low_concurrency_enabled，沿用现有低并发策略或默认启用。

函数级工作：

1. 在 [`PlaywrightDriver._emit_detected_resource()`](../core/playwright_driver.py:1146) 增加请求头摘要日志：来源、是否有 Cookie、Cookie 长度、是否有 Referer、Origin、User-Agent、资源类型、MIME。
2. 在 [`N_m3u8DL_RE_Engine.download()`](../engines/n_m3u8dl_re.py:354) 增加最终下载头摘要日志。
3. 在 [`N_m3u8DL_RE_Engine._prepare_cookie_header()`](../engines/n_m3u8dl_re.py:201) 增加 Cookie 来源摘要：原始请求头、浏览器导出文件、无匹配 Cookie。
4. 所有日志必须继续使用脱敏策略，不输出 Cookie 值、Authorization 值、完整敏感 query。

验证：

1. 日志中不出现 Cookie 明文。
2. 日志中不出现 Authorization 明文。
3. 执行回归测试：
   - python -m pytest tests/test_engine_argv_safety.py tests/test_download_manager_state_machine.py -q

---

## 3. 优化一：精确请求头复用

### 3.1 目标

对于 Playwright 网络事件真实捕获到的 m3u8/mpd 请求，应尽量复用该请求当时的 headers。尤其是由 performance API 或页面动态变量发现的 PWR-CAP 资源，其 headers 为空时，应优先查最近真实网络请求缓存，而不是完全依赖合成 headers。

当前相关函数：

- [`PlaywrightDriver.handle_request()`](../core/playwright_driver.py:717)
- [`PlaywrightDriver.handle_response()`](../core/playwright_driver.py:747)
- [`PlaywrightDriver._probe_dynamic_media_urls()`](../core/playwright_driver.py:992)
- [`PlaywrightDriver._emit_detected_resource()`](../core/playwright_driver.py:1146)
- [`PlaywrightDriver._build_default_headers()`](../core/playwright_driver.py:1080)
- [`M3U8Sniffer.add_resource()`](../core/m3u8_sniffer.py:48)

### 3.2 函数级开发步骤

#### 3.2.1 在 PlaywrightDriver 初始化缓存

修改 [`PlaywrightDriver.__init__()`](../core/playwright_driver.py:47)：

- 新增最近媒体请求头缓存。
- 建议结构：URL 到记录对象，记录对象包含 headers、page_url、source、resource_type、mime、captured_at。
- 缓存容量建议 500 到 1000 条。
- 缓存过期时间建议 3 到 10 分钟。

#### 3.2.2 新增真实请求头缓存写入函数

新增 [`PlaywrightDriver._remember_media_request_headers()`](../core/playwright_driver.py:717)：

职责：

- 入参：url、headers、page_url、source、resource_type、mime。
- 调用 [`normalized_forward_headers()`](../utils/headers.py:330) 做白名单过滤。
- Cookie 是否保留受 features.forward_cookie_headers 控制。
- Authorization 是否保留受 features.forward_authorization_headers 控制。
- 不保存 hop-by-hop headers。
- 不保存空 headers。
- 不覆盖更完整的旧 headers，除非新 headers 得分更高。

建议增加辅助函数 [`PlaywrightDriver._score_forward_headers()`](../core/playwright_driver.py:1080)：

- 有 Cookie 加高分。
- 有 Referer 加分。
- 有 Origin 加分。
- 有 User-Agent 加分。
- 有 Accept-Language、Sec-Fetch-*、Sec-CH-UA-* 适度加分。

#### 3.2.3 新增真实请求头缓存读取函数

新增 [`PlaywrightDriver._lookup_recent_media_request_headers()`](../core/playwright_driver.py:1080)：

职责：

- 优先精确 URL 匹配。
- 对带明显短期签名参数的 URL，不做去 query 匹配，避免旧 token 误配。
- 对不带 query 的普通 m3u8/mpd，允许同 host、同 path 匹配。
- 返回 headers 副本，避免后续修改污染缓存。
- 过期记录在读取或写入时清理。

#### 3.2.4 修改 request 捕获逻辑

修改 [`PlaywrightDriver.handle_request()`](../core/playwright_driver.py:717)：

- 当 [`PlaywrightDriver._is_video_url()`](../core/playwright_driver.py:1200) 命中时，先调用 [`PlaywrightDriver._remember_media_request_headers()`](../core/playwright_driver.py:717)。
- 然后继续调用 [`PlaywrightDriver._emit_detected_resource()`](../core/playwright_driver.py:1146)。
- 不改变现有 PWR-REQ 发射行为。

#### 3.2.5 修改 response 捕获逻辑

修改 [`PlaywrightDriver.handle_response()`](../core/playwright_driver.py:747)：

- 当 Content-Type 命中 HLS、DASH 或视频资源时，调用 [`PlaywrightDriver._remember_media_request_headers()`](../core/playwright_driver.py:717)。
- response 阶段可补全 mime、resource_type。
- 如果 response 阶段 headers 比 request 阶段更完整，则允许覆盖缓存。

#### 3.2.6 修改资源发射逻辑

修改 [`PlaywrightDriver._emit_detected_resource()`](../core/playwright_driver.py:1146)：

合并顺序：

1. 显式传入 headers 最高。
2. 最近真实请求缓存 headers 第二。
3. [`PlaywrightDriver._build_default_headers()`](../core/playwright_driver.py:1080) 合成 headers 最低。

典型行为：

- PWR-REQ：直接使用 request.headers，并写入缓存。
- PWR-RSP：确认 MIME 后再次发射，使用 request.headers，并补充 MIME。
- PWR-CAP：headers 为空，先查缓存；如果命中则复用真实 headers，否则再合成。

#### 3.2.7 修改资源合并策略

修改 [`M3U8Sniffer.add_resource()`](../core/m3u8_sniffer.py:48) 和 [`M3U8Sniffer._merge_resource_context()`](../core/m3u8_sniffer.py:243)：

- 同 URL 资源重复出现时，优先保留 headers 更完整的一份。
- 不让 PWR-CAP 的空 headers 覆盖 PWR-REQ/PWR-RSP 的真实 headers。
- resource_type、mime、master_url、media_url 可从后续更明确的事件补齐。

### 3.3 自动测试

新增 [`tests/test_playwright_header_replay.py`](../tests/test_playwright_header_replay.py)：

1. PWR-CAP 空 headers 时命中最近真实请求缓存。
2. PWR-REQ 已有 Cookie 时，不被合成 Cookie 覆盖。
3. Authorization 默认不缓存。
4. 开启 Authorization 转发后，Authorization 带内部允许标记时才进入下载 headers。
5. 同 URL 重复资源合并时，完整 headers 不被空 headers 覆盖。

### 3.4 手工验证

1. 用内置浏览器打开视频页并播放。
2. 观察日志：先出现 PWR-REQ 或 PWR-RSP 捕获真实 headers。
3. 如果 PWR-CAP 再发现同 URL，应日志显示命中 header replay。
4. 开始下载后，N_m3u8DL-RE 命令摘要应显示 Referer、Origin、User-Agent、Cookie 状态正确。

---

## 4. 优化二：resource_url 域名 Cookie 优先匹配

### 4.1 目标

当 page_url 和 resource_url 不同域时，Cookie 应优先按 resource_url 域名取，而不是优先按 page_url 取。这样可以避免把页面域 Cookie 错用于 CDN/m3u8 域，也能补齐 surrit.com 这类资源域 Cookie。

当前相关函数：

- [`PlaywrightDriver._build_default_headers()`](../core/playwright_driver.py:1080)
- [`PlaywrightDriver.export_cookies_to_file()`](../core/playwright_driver.py:1427)
- [`N_m3u8DL_RE_Engine._prepare_cookie_header()`](../engines/n_m3u8dl_re.py:201)
- [`N_m3u8DL_RE_Engine._cookie_header_from_netscape_file()`](../engines/n_m3u8dl_re.py:145)
- [`N_m3u8DL_RE_Engine._domain_matches()`](../engines/n_m3u8dl_re.py:84)
- [`N_m3u8DL_RE_Engine._path_matches()`](../engines/n_m3u8dl_re.py:93)

### 4.2 函数级开发步骤

#### 4.2.1 新增资源域 Cookie 读取函数

新增 [`PlaywrightDriver._cookie_header_for_resource()`](../core/playwright_driver.py:1080)：

读取优先级：

1. 使用 Playwright context.cookies(resource_url) 获取资源域 Cookie。
2. 若资源域没有 Cookie，且 page_url 与 resource_url 同站，再尝试 context.cookies(page_url)。
3. 若两者同站且都有 Cookie，可以合并；同名 Cookie 以 resource_url 结果优先。
4. 若两者跨站，不把 page_url Cookie 强行塞给 resource_url。

#### 4.2.2 修改默认请求头构造

修改 [`PlaywrightDriver._build_default_headers()`](../core/playwright_driver.py:1080)：

- 当前 Cookie 合成逻辑应改为调用 [`PlaywrightDriver._cookie_header_for_resource()`](../core/playwright_driver.py:1080)。
- 仅当 headers 中没有 Cookie 时才补齐。
- 不覆盖真实请求头中的 Cookie。
- 对 m3u8 和 mpd 都适用，不只判断 .m3u8。

#### 4.2.3 保留 N_m3u8DL-RE 下载前兜底

保留并增强 [`N_m3u8DL_RE_Engine._prepare_cookie_header()`](../engines/n_m3u8dl_re.py:201)：

- task.headers 已有 Cookie 时不覆盖。
- task.headers 无 Cookie 时，通过 cookie_exporter 导出浏览器 Cookie。
- 使用 [`N_m3u8DL_RE_Engine._cookie_header_from_netscape_file()`](../engines/n_m3u8dl_re.py:145) 按 source_url 域名、path、secure、expiry 过滤。
- 对 master_url、media_url fallback 每次 source_url 都可重新尝试匹配。

#### 4.2.4 增强 Cookie 导出日志

修改 [`PlaywrightDriver.export_cookies_to_file()`](../core/playwright_driver.py:1427)：

- 增加导出目标 URL 或 domain_filter 的脱敏摘要。
- 增加导出 Cookie 数量、匹配域数量。
- 不记录 Cookie 值。

### 4.3 自动测试

新增或扩展 [`tests/test_playwright_header_replay.py`](../tests/test_playwright_header_replay.py)：

1. page_url 为 page.example，resource_url 为 cdn.example，两者同站时允许合理合并。
2. page_url 为 page.example，resource_url 为 surrit.com，两者跨站时优先 surrit.com Cookie。
3. resource_url 无 Cookie 且跨站时，不注入 page_url Cookie。
4. 原始 request.headers 已有 Cookie 时不覆盖。

保留已有测试：

- [`test_nm3u8dl_re_prepares_cookie_header_from_browser_cookie_file()`](../tests/test_engine_argv_safety.py:266)
- [`test_nm3u8dl_re_download_exports_playwright_cookies_before_build()`](../tests/test_engine_argv_safety.py:295)

---

## 5. 优化三：短期链接临下载前刷新

### 5.1 目标

对带短期签名参数的 m3u8/mpd，减少“抓到后过一会儿再下载导致 URL 已失效”。第一版先做风险识别和提示；第二版再做自动刷新和重新捕获。

典型参数：expires、exp、token、signature、sig、policy、Policy、Key-Pair-Id、X-Amz-Date、X-Amz-Expires、X-Amz-Signature、e。

### 5.2 第一版：短期 URL 识别与提示

#### 5.2.1 新增短期 URL 分析模块

新增 [`core/media_url_ttl.py`](../core/media_url_ttl.py)：

函数：

- [`analyze_media_url_ttl()`](../core/media_url_ttl.py:1)：返回短期链接分析结果。
- [`is_probably_ephemeral_media_url()`](../core/media_url_ttl.py:1)：判断是否疑似短期 URL。
- [`estimate_expiry_timestamp()`](../core/media_url_ttl.py:1)：从 query 中尽量推算过期时间。
- [`seconds_until_expiry()`](../core/media_url_ttl.py:1)：计算剩余秒数。

建议数据结构：

- ephemeral：是否疑似短期链接。
- expires_at：可推算的过期时间戳。
- matched_params：命中的参数名列表。
- risk_level：low、medium、high、expired。
- reason：简短原因。

#### 5.2.2 扩展资源模型

修改 [`M3U8Resource`](../core/task_model.py:41)：

新增字段：

- detected_at。
- expires_at。
- ephemeral_url。
- ttl_warning。

填充位置：

- [`M3U8Resource.__post_init__()`](../core/task_model.py:75)，或
- [`M3U8Sniffer.add_resource()`](../core/m3u8_sniffer.py:48)。

#### 5.2.3 扩展下载任务模型

修改 [`DownloadTask`](../core/task_model.py:223)：

新增字段：

- detected_at。
- expires_at。
- ephemeral_url。
- ttl_warning。

在 [`MainWindowSniffFlowMixin._start_download()`](../ui/main_window_sniff_flow.py:701) 中从 [`M3U8Resource`](../core/task_model.py:41) 传递到 [`DownloadTask`](../core/task_model.py:223)。

#### 5.2.4 下载前风险提示

新增 [`MainWindowSniffFlowMixin._warn_if_ephemeral_url_stale()`](../ui/main_window_sniff_flow.py:701)：

调用位置：

- [`MainWindowSniffFlowMixin._start_download()`](../ui/main_window_sniff_flow.py:701) 创建任务前。

行为：

- 已过期：提示建议重新播放并抓取最新链接。
- 即将过期：提示应立即下载或重新抓取。
- 带签名但无法推算过期时间：提示抓到后应尽快下载。
- 普通 URL：不提示。

外部 CatCatch 来源：

- 只能提示，不能自动刷新外部浏览器页面。

### 5.3 第二版：临下载前自动刷新

#### 5.3.1 BrowserView 增加刷新捕获入口

新增 [`BrowserView.refresh_and_capture_current_page()`](../ui/browser_view.py:259)：

职责：

- 通知 Playwright 刷新当前页面或重新打开 page_url。
- 开启捕获窗口。
- 等待新媒体资源出现。
- 返回新 URL 和 headers，或返回失败原因。

#### 5.3.2 PlaywrightDriver 增加刷新等待动作

修改 [`PlaywrightDriver._handle_action()`](../core/playwright_driver.py:1359)：

- 增加 refresh_and_wait_for_media 动作分支。

新增 [`PlaywrightDriver.refresh_and_wait_for_media()`](../core/playwright_driver.py:1359)：

入参：

- page_url。
- previous_resource_url。
- timeout_ms。

行为：

1. 导航或 reload。
2. 调用 [`PlaywrightDriver._begin_capture_window()`](../core/playwright_driver.py:940)。
3. 等待同页面新 m3u8/mpd 出现。
4. 优先返回与 previous_resource_url 同 host、同 path 或同播放页的最新资源。
5. 超时则返回空结果。

#### 5.3.3 UI 下载前刷新流程

修改 [`MainWindowSniffFlowMixin._start_download()`](../ui/main_window_sniff_flow.py:701)：

- 如果 ephemeral_url 为 true 且风险高，询问用户是否刷新。
- 用户同意后调用 [`BrowserView.refresh_and_capture_current_page()`](../ui/browser_view.py:259)。
- 成功后替换 url、headers、detected_at、expires_at。
- 失败时提示用户重新手动播放。

### 5.4 自动测试

新增 [`tests/test_media_url_ttl.py`](../tests/test_media_url_ttl.py)：

1. exp 秒级时间戳可识别。
2. expires 秒级时间戳可识别。
3. X-Amz-Date 加 X-Amz-Expires 可推算。
4. 过期 URL 标记 expired。
5. 即将过期 URL 标记 high risk。
6. 只有 token 但无过期时间时标记 ephemeral 但 expires_at 为空。

### 5.5 手工验证

1. 抓取带 expires 的 m3u8，等待接近过期后点击下载，应出现提示。
2. 抓到后立即下载，不应阻断。
3. 外部 CatCatch 来源只提示，不尝试自动刷新。
4. 内置浏览器来源开启自动刷新后，应尝试重新打开页面并等待新资源。

---

## 6. 优化四：低并发自适应

### 6.1 目标

对浏览器播放正常但下载器高并发请求触发 403、429、限速、CDN 拒绝的站点，自动降线程重试。项目已有基础低并发逻辑，本阶段主要增强触发条件、去重保护与 host 级记忆。

当前相关函数：

- [`N_m3u8DL_RE_Engine.download()`](../engines/n_m3u8dl_re.py:354)
- [`N_m3u8DL_RE_Engine._should_retry_low_concurrency()`](../engines/n_m3u8dl_re.py:1138)
- [`N_m3u8DL_RE_Engine._low_concurrency_retry_enabled()`](../engines/n_m3u8dl_re.py:1156)
- [`N_m3u8DL_RE_Engine._low_concurrency_thread_count()`](../engines/n_m3u8dl_re.py:1162)
- [`N_m3u8DL_RE_Engine._auto_thread_count()`](../engines/n_m3u8dl_re.py:1186)
- [`N_m3u8DL_RE_Engine._build_command()`](../engines/n_m3u8dl_re.py:741)

### 6.2 函数级开发步骤

#### 6.2.1 扩展低并发触发判断

修改 [`N_m3u8DL_RE_Engine._should_retry_low_concurrency()`](../engines/n_m3u8dl_re.py:1138)：

增加可恢复关键词：

- HTTP 403 且上下文包含 segment、fragment、ts、m4s、retry、forbidden。
- HTTP 429。
- too many requests。
- rate limit。
- throttled。
- connection reset。
- timeout。
- temporarily unavailable。
- segment retry exhausted。
- download speed too low。

增加不可恢复排除：

- DRM。
- widevine。
- fairplay。
- playready。
- unsupported。
- invalid url。
- 404 not found。
- no such host。
- name resolution。

#### 6.2.2 增加 host 级限流记忆

新增 [`N_m3u8DL_RE_Engine._remember_rate_limited_host()`](../engines/n_m3u8dl_re.py:1138)：

- 入参：url、reason。
- 保存 host 到时间戳。
- 过期时间建议 30 到 60 分钟。

新增 [`N_m3u8DL_RE_Engine._host_has_recent_rate_limit()`](../engines/n_m3u8dl_re.py:1138)：

- 入参：url。
- 返回该 host 是否最近触发过限流。

#### 6.2.3 增加是否初始低并发判断

新增 [`N_m3u8DL_RE_Engine._should_start_low_concurrency()`](../engines/n_m3u8dl_re.py:1186)：

触发条件建议：

- host 最近低并发重试成功。
- task.ephemeral_url 为 true。
- task.source 为内置浏览器且站点曾触发限流。
- task.headers 带 Cookie 且 URL 疑似签名 CDN。

第一版建议只对 host 最近低并发成功场景启用，避免普遍降低速度。

#### 6.2.4 修改下载主循环

修改 [`N_m3u8DL_RE_Engine.download()`](../engines/n_m3u8dl_re.py:354)：

- 每个 source_url 最多低并发重试一次。
- 低并发成功后调用 [`N_m3u8DL_RE_Engine._remember_rate_limited_host()`](../engines/n_m3u8dl_re.py:1138)。
- 如果 host 已有近期限流记录，则初始命令可直接使用低线程，或先降低默认线程。
- stop_requested 时不进入低并发重试。

#### 6.2.5 确认低并发命令保留 headers

验证 [`N_m3u8DL_RE_Engine._build_command()`](../engines/n_m3u8dl_re.py:741)：

- safe_mode 下仍调用 [`N_m3u8DL_RE_Engine._append_headers()`](../engines/n_m3u8dl_re.py:897)。
- thread_override 只影响线程数，不影响 Cookie、Referer、Origin、User-Agent。

### 6.3 自动测试

扩展 [`tests/test_engine_argv_safety.py`](../tests/test_engine_argv_safety.py)：

1. 403 segment 失败触发低并发重试。
2. 429 触发低并发重试。
3. DRM 不触发低并发重试。
4. 404 不触发低并发重试。
5. 低并发命令仍包含 Cookie、Referer、Origin、User-Agent。
6. 同一 source_url 低并发只重试一次。
7. 低并发成功后，host 级状态被记录。

保留已有测试：

- [`test_nm3u8dl_re_rate_limit_hint_uses_low_thread_count()`](../tests/test_engine_argv_safety.py:349)
- [`test_nm3u8dl_re_rate_limit_hint_respects_disabled_flag()`](../tests/test_engine_argv_safety.py:368)

### 6.4 手工验证

1. 构造 N_m3u8DL-RE 输出含 429 或 segment retry exhausted。
2. 确认第一次失败后进入低并发重试。
3. 确认低并发命令仍携带 headers。
4. 确认 stop、pause、cancel 不会继续重试。

---

## 7. 优化五：DRM 与跨域诊断

### 7.1 目标

尽早判断失败类型，避免把 DRM、跨域 key、跨域 segment 误判为普通 Cookie 失败。诊断不一定直接提升下载成功率，但能减少无效重试并给用户明确反馈。

当前相关函数：

- [`M3U8FetchThread.run()`](../core/m3u8_parser.py:193)
- [`M3U8FetchThread._fetch_with_retry()`](../core/m3u8_parser.py:241)
- [`MainWindowSniffFlowMixin._show_m3u8_variant_dialog()`](../ui/main_window_sniff_flow.py:411)
- [`MainWindowSniffFlowMixin._start_download()`](../ui/main_window_sniff_flow.py:701)
- [`DownloadManager._classify_failure()`](../core/download/manager.py:1012)

### 7.2 函数级开发步骤

#### 7.2.1 新增诊断数据结构

在 [`core/m3u8_parser.py`](../core/m3u8_parser.py) 新增 PlaylistDiagnostics 数据结构：

字段建议：

- is_drm。
- drm_type。
- playlist_host。
- segment_hosts。
- key_hosts。
- cross_domain_segments。
- cross_domain_keys。
- has_key_uri。
- has_aes128_key。
- warnings。

#### 7.2.2 新增 playlist 诊断函数

新增 [`analyze_playlist_diagnostics()`](../core/m3u8_parser.py:245)：

入参：

- playlist_url。
- playlist_content。

解析内容：

- EXT-X-KEY。
- EXT-X-SESSION-KEY。
- EXT-X-MAP。
- 普通 segment 行。
- 相对路径通过 urljoin 转为绝对 URL。

DRM 特征：

- KEYFORMAT 为 com.widevine。
- KEYFORMAT 为 com.apple.streamingkeydelivery。
- KEYFORMAT 为 com.microsoft.playready。
- urn:uuid:edef8ba9。
- skd://。
- widevine、fairplay、playready 相关字段。

普通 HLS 加密：

- METHOD=AES-128 且无 DRM KEYFORMAT 时，不标记 DRM。
- 但标记 has_key_uri 为 true，后续检查 key URL 是否跨域。

跨域诊断：

- segment host 不同于 playlist host，则 cross_domain_segments 为 true。
- key host 不同于 playlist host，则 cross_domain_keys 为 true。

#### 7.2.3 在 M3U8FetchThread 中调用诊断

修改 [`M3U8FetchThread.run()`](../core/m3u8_parser.py:193)：

- fetch 成功后调用 [`analyze_playlist_diagnostics()`](../core/m3u8_parser.py:245)。
- 将诊断结果保存在线程实例字段中。
- 通过新增 signal 或现有结果结构传给 UI。
- 诊断失败不应阻断原有 variant 解析。

#### 7.2.4 扩展资源与任务模型

修改 [`M3U8Resource`](../core/task_model.py:41)：

- 新增 diagnostics 字段。

修改 [`DownloadTask`](../core/task_model.py:223)：

- 新增 diagnostics 字段或轻量 flags：is_drm、cross_domain_keys、cross_domain_segments。

修改 [`M3U8Sniffer._merge_resource_context()`](../core/m3u8_sniffer.py:243)：

- 合并同 URL 资源时保留更完整 diagnostics。

#### 7.2.5 UI 提示

修改 [`MainWindowSniffFlowMixin._show_m3u8_variant_dialog()`](../ui/main_window_sniff_flow.py:411)：

- 如果 diagnostics.is_drm 为 true，提示“DRM 内容，普通下载器无法解密”。
- 如果 cross_domain_keys 为 true，提示“key 地址跨域，可能需要资源域 Cookie 或完整请求头”。
- 如果 cross_domain_segments 为 true，提示“分片跨域，下载器可能需要完整 headers 或低并发”。

修改 [`MainWindowSniffFlowMixin._start_download()`](../ui/main_window_sniff_flow.py:701)：

- 如果 DRM 为 true，允许用户继续但默认建议取消。
- 如果 cross_domain_keys 或 cross_domain_segments 为 true，下载前检查 headers 是否包含 Referer、Origin、User-Agent，必要时提示重新用内置浏览器播放抓取。

#### 7.2.6 失败分类优化

修改 [`DownloadManager._classify_failure()`](../core/download/manager.py:1012) 相关分类链路：

- 如果 task diagnostics 显示 DRM，最终失败原因优先显示 DRM 不支持。
- 如果 diagnostics 显示跨域 key，最终失败原因可提示 key 域名鉴权失败。
- 不改变状态机，只改变诊断文案。

### 7.3 自动测试

新增 [`tests/test_m3u8_playlist_diagnostics.py`](../tests/test_m3u8_playlist_diagnostics.py)：

1. 普通 HLS：is_drm 为 false。
2. AES-128 普通加密：is_drm 为 false，has_key_uri 为 true。
3. Widevine：is_drm 为 true，drm_type 为 widevine。
4. FairPlay：is_drm 为 true，drm_type 为 fairplay。
5. PlayReady：is_drm 为 true，drm_type 为 playready。
6. segment host 跨域：cross_domain_segments 为 true。
7. key host 跨域：cross_domain_keys 为 true。
8. 相对 key 和相对 segment 能正确 urljoin。

### 7.4 手工验证

1. DRM 样本在解析阶段提示，而不是等到下载失败后泛化报错。
2. key 跨域样本显示 key 域名提示。
3. segment 跨域样本显示分片跨域提示。
4. 普通 AES-128 不误报为 DRM。

---

## 8. 端到端测试矩阵

### 8.1 自动测试命令

建议第一轮执行：

python -m pytest tests/test_engine_argv_safety.py tests/test_download_manager_state_machine.py -q

新增测试后执行：

python -m pytest tests/test_engine_argv_safety.py tests/test_download_manager_state_machine.py tests/test_playwright_header_replay.py tests/test_media_url_ttl.py tests/test_m3u8_playlist_diagnostics.py -q

完整回归执行：

python -m pytest -q

### 8.2 手工场景

#### 场景 A：普通内置浏览器 HLS

预期：

- PWR-REQ 或 PWR-RSP 抓到真实 headers。
- 下载任务使用 N_m3u8DL-RE。
- 下载命令携带 Referer、Origin、User-Agent。
- 如果资源域有 Cookie，应携带 Cookie。

#### 场景 B：PWR-CAP 动态发现 HLS

预期：

- PWR-CAP headers 为空时，优先复用最近真实请求缓存。
- 如果缓存未命中，再使用合成 headers。
- 不应覆盖已有完整 headers。

#### 场景 C：页面域与资源域不同

预期：

- Cookie 优先按 resource_url 域名取。
- resource_url 无 Cookie 且跨站时，不强行带 page_url Cookie。
- N_m3u8DL-RE 下载前仍会尝试按 source_url 从浏览器导出 Cookie 文件兜底。

#### 场景 D：短期签名 URL

预期：

- URL 带 expires、token、signature 等参数时，资源标记为 ephemeral。
- 即将过期时点击下载出现提示。
- 内置浏览器来源可选择刷新重抓。
- CatCatch 来源只提示，不自动刷新。

#### 场景 E：限流站点

预期：

- 普通并发失败后触发低并发重试。
- 低并发命令仍携带 headers。
- DRM、404、unsupported 不触发低并发重试。

#### 场景 F：DRM 样本

预期：

- 解析阶段识别 DRM。
- UI 提示普通下载器无法解密。
- 最终失败不误导为 Cookie 缺失。

---

## 9. 推荐实施顺序

### 第一批：收益最大、风险最低

1. 精确请求头复用。
2. resource_url 域名 Cookie 优先匹配。
3. 对现有 N_m3u8DL-RE Cookie 兜底补测试。

原因：

- 直接针对 403。
- 不改变下载器架构。
- 与现有内置浏览器能力匹配。

### 第二批：增强失败后的恢复能力

1. 低并发触发条件增强。
2. host 级限流记忆。
3. 低并发命令 headers 保真测试。

原因：

- 项目已有低并发基础。
- 改动集中在 [`N_m3u8DL_RE_Engine.download()`](../engines/n_m3u8dl_re.py:354) 和相关 helper。

### 第三批：减少误判和无效重试

1. DRM 诊断。
2. key 跨域诊断。
3. segment 跨域诊断。
4. UI 风险提示。

原因：

- 对成功率间接有帮助。
- 能明显提升用户理解和问题定位效率。

### 第四批：短期 URL 自动刷新

1. 先做短期 URL 识别和提示。
2. 再做内置浏览器刷新重抓。
3. 最后做下载前自动替换最新 URL。

原因：

- 涉及 UI、浏览器线程、资源列表、任务创建，复杂度最高。
- 自动播放可能受站点限制，必须保留手动兜底。

---

## 10. 风险与边界

1. 强 Cloudflare TLS/HTTP2 指纹绑定：
   - 本计划只能提高 headers、Cookie、时效、并发层面的成功率。
   - N_m3u8DL-RE 仍不是 Chromium 网络栈，无法完全模拟浏览器 TLS/HTTP2 指纹。

2. DRM：
   - 只能诊断和提示。
   - 不应尝试绕过 DRM。

3. 短期 URL 自动刷新：
   - 某些站点必须用户手动点击播放。
   - 自动刷新不保证能重新产生 m3u8。

4. Cookie 转发安全：
   - 默认继续受 features.forward_cookie_headers 和临时授权策略控制。
   - 跨站 Cookie 不应随意转发。
   - 日志不得输出 Cookie 明文。

5. Authorization 转发安全：
   - 默认关闭。
   - 仅在显式启用或站点规则允许时转发。

---

## 11. 完成标准

1. 内置浏览器抓取的真实请求头能被缓存并复用于 PWR-CAP 资源。
2. resource_url 域名 Cookie 优先于 page_url 域名 Cookie。
3. N_m3u8DL-RE 下载前 Cookie 兜底仍正常工作。
4. 短期签名 URL 能被识别，并在高风险时提示。
5. 限流、429、分片 403 能触发低并发重试。
6. DRM、404、unsupported 不触发无效低并发重试。
7. playlist 诊断能识别 DRM、key 跨域、segment 跨域。
8. 关键链路日志脱敏。
9. 新增和既有测试通过。
10. 普通 HLS、CatCatch、yt-dlp 路径不发生回归。
