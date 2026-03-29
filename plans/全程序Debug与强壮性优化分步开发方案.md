# 全程序 Debug 与强壮性优化分步开发方案

## 1. 文档目的
- 目的：基于本轮静态审计结果，对整个程序的潜在错误、强壮性问题、流程优化点，整理为可执行的分步开发方案。
- 目标：方案细化到“文件 + 函数 + 开发动作 + 阶段测试 + 通过门禁”，供后续逐阶段开发和验收使用。
- 范围：启动入口、配置加载、协议投递、CatCatch HTTP 服务、下载状态机、引擎进程托管、内置浏览器抓取链路、日志与异常处理、UI 维护性、自动化测试基线。

## 2. 本轮已确认的重点问题
1. `utils/config_manager.py` 的 `ConfigManager.load()` 在成功读取配置后，又调用 `_load_defaults()`，存在覆盖用户配置的高风险。
2. `protocol_handler.pyw` 的 `send_to_app()` 固定发送到 `9527`，与 `core/catcatch_server.py` 的端口回退策略不一致。
3. `core/catcatch_server.py` 的 `CatCatchServer.start()` / `_run_server()` / `stop()` 状态管理不完整，可能出现“服务未成功监听，但状态显示已启动”的问题。
4. `core/download_manager.py` 的 `resume_task()`、`remove_task()`、`_execute_download()`、`get_all_tasks()` 存在重复记录、脏状态、状态列表不一致风险。
5. `core/download_manager.py` 的 `shutdown()` 与暂停/取消路径对子进程的清理方式不一致，可能留下孤儿进程。
6. `core/playwright_driver.py`、`protocol_handler.pyw`、`main.py`、`core/request_interceptor.py`、`engines/ytdlp_engine.py` 等存在大量宽泛异常捕获和静默吞错，降低可诊断性。
7. 多个文件存在中文乱码污染，影响日志、UI 和后续维护。
8. 项目缺少自动化测试基线，近期修复项无法做回归兜底。

## 3. 强制执行规则
1. 每个阶段开发完成后，必须先通过该阶段全部测试，才允许进入下一阶段。
2. 任一阶段测试未通过，禁止继续后续阶段，必须先修复并重测。
3. 每个阶段结束后，必须输出一份阶段报告，建议保存到 `plans/phase_reports/DEBUG_Sx_report.md`。
4. 每个阶段至少执行一次 `compileall` 语法检查。
5. 任何涉及下载状态、协议投递、内置浏览器抓取的改动，必须做一轮真实烟测。

## 4. 基线准备阶段（S0）

### 4.1 目标
- 固化当前问题基线，避免后续“修了但不知道收益有多大”。

### 4.2 涉及文件与函数
- `main.py` -> `main()`
- `utils/logger.py`
- `core/download_manager.py` -> `get_stats()`、`get_quality_metrics()`
- `core/playwright_driver.py` -> `run()`
- `protocol_handler.pyw` -> `main()`

### 4.3 开发动作
1. 记录当前版本的手工基线行为：
   - 程序启动
   - CatCatch HTTP API 收包
   - `m3u8dl://` 协议投递
   - 内置浏览器抓取 m3u8
   - 添加下载任务
   - 暂停/恢复/取消/删除任务
   - 程序关闭
2. 统一收集本轮基线日志样本：
   - `logs/m3u8sniffer_*.log`
   - `logs/protocol_handler.log`
3. 整理一组固定回归样本：
   - 直链 media playlist
   - master playlist
   - AES-128 HLS
   - 需要 Referer 的站点
   - 需要 Cookie 的站点
4. 建立基线指标：
   - 启动成功率
   - 协议投递成功率
   - CatCatch 收包成功率
   - 抓取命中率
   - 下载成功率
   - 暂停/取消/关闭清理成功率

### 4.4 阶段测试
1. 执行 `python -m compileall main.py core ui engines utils protocol_handler.pyw`
2. 启动程序并验证基本流程可走通
3. 固化一份基线样本和日志

### 4.5 通过门禁
1. 基线日志收集完成
2. 基线样本整理完成
3. `compileall` 通过

## 5. 第一阶段：配置、入口与服务正确性（S1）

### 5.1 目标
- 保证配置不会被覆盖，协议能投递到真实监听端口，CatCatch HTTP 服务状态准确。

### 5.2 涉及文件与函数
- `utils/config_manager.py`
  - `ConfigManager.load()`
  - `ConfigManager.save()`
  - `ConfigManager._load_defaults()`
- `main.py`
  - `parse_args()`
  - `main()`
- `protocol_handler.pyw`
  - `parse_n_m3u8dl_format()`
  - `parse_m3u8dl_url()`
  - `send_to_app()`
  - `launch_app_with_url()`
  - `main()`
- `core/catcatch_server.py`
  - `DownloadRequestHandler.log_message()`
  - `DownloadRequestHandler.do_POST()`
  - `DownloadRequestHandler._handle_download_request()`
  - `CatCatchServer.start()`
  - `CatCatchServer._run_server()`
  - `CatCatchServer.stop()`
  - `CatCatchServer.is_running()`
  - `CatCatchServer.get_url()`

### 5.3 开发步骤
1. 修复 `ConfigManager.load()`
   - 去掉“成功读取后再次 `_load_defaults()`”的错误逻辑。
   - 将 `_load_defaults()` 重构为“返回默认配置”的纯函数，避免读配置时直接覆盖现有配置。
   - 建议新增辅助函数：
     - `_build_default_config()`
     - `_merge_with_defaults(loaded_config)`
     - `_ensure_directories()`
2. 修复 `ConfigManager.save()`
   - 保留写注释字段的逻辑，但注释仅在保存副本时写入，不污染内存中的运行态配置。
   - 明确“运行态配置”和“写盘配置”分离。
3. 修复 `main.main()`
   - 不再直接覆盖 `QTWEBENGINE_CHROMIUM_FLAGS`，改为“读取已有值后合并”。
   - `args.headers` 解析从裸 `except:` 改为显式 `json.JSONDecodeError`。
4. 修复 `protocol_handler.send_to_app()`
   - 支持探测 `9527-9539` 端口。
   - 建议新增辅助函数：
     - `_iter_candidate_ports()`
     - `_probe_app_port()`
5. 修复 `protocol_handler.launch_app_with_url()`
   - 启动主程序后增加“等待 + 重试投递”。
   - 启动 Python 解释器时不要只依赖固定 `Documents\\.venv` 路径，增加兜底顺序。
6. 修复 `protocol_handler.main()`
   - 区分“解析失败”“投递失败”“启动失败”三类日志。
   - 协议调用失败时不要只写笼统日志。
7. 修复 `DownloadRequestHandler.log_message()`
   - 不能只写 `args[0]`，应输出完整格式化后的 HTTP 请求行或状态。
8. 修复 `DownloadRequestHandler.do_POST()`
   - 表单解析改用标准 `parse_qs()` 或统一 JSON/form 解码逻辑。
   - 避免手工拆 `&` / `=` 丢失编码细节。
9. 修复 `CatCatchServer.start()` / `_run_server()` / `stop()`
   - 只有真正 bind 成功后才设置 `_running=True`。
   - 端口回退成功后更新 `self.port` 并对外可见。
   - `stop()` 需要执行：
     - `shutdown()`
     - `server_close()`
     - `thread.join(timeout=...)`
   - 失败时记录具体端口和异常，不用裸 `except: continue`。
10. 修复 `CatCatchServer.is_running()` / `get_url()`
    - 以真实服务状态为准，不以“是否调用过 start”推断。

### 5.4 阶段测试
1. 配置测试
   - 修改 `config.json` 中下载目录、并发数、限速后重启程序，确认配置不被重置。
   - 删除部分字段后重启，确认只补齐缺省字段，不覆盖已有字段。
2. 协议测试
   - 占用 `9527`，确认主程序可监听到 `9528+`，协议仍能正确投递。
   - 主程序已运行时再次触发协议，确认不会重复启动第二个主程序实例。
3. HTTP 服务测试
   - 连续启动/关闭程序 5 次，确认端口释放正常。
   - 手工 `POST /download`，确认日志与任务入队正常。

### 5.5 通过门禁
1. 重启后配置保持正确
2. 端口回退与协议投递测试通过
3. CatCatch 服务启动/停止行为稳定
4. `compileall` 通过

## 6. 第二阶段：下载状态机与任务生命周期（S2）

### 6.1 目标
- 让任务状态只有一条真实状态链，避免重复记录、状态串联、统计失真。

### 6.2 涉及文件与函数
- `core/task_model.py`
  - `DownloadTask.get_status_display()`
- `core/download_manager.py`
  - `add_task()`
  - `_worker()`
  - `_execute_download()`
  - `pause_task()`
  - `resume_task()`
  - `cancel_task()`
  - `remove_task()`
  - `get_all_tasks()`
  - `get_stats()`
  - `get_quality_metrics()`
- `ui/download_queue.py`
  - `add_or_update_task()`
  - `_update_item()`
  - `_on_pause_selected()`
  - `_on_resume_selected()`
  - `_on_stop_selected()`
  - `_on_retry_selected()`
  - `_on_clear_completed()`

### 6.3 开发步骤
1. 为 `DownloadManager` 增加统一状态迁移入口
   - 建议新增辅助函数：
     - `_reset_task_runtime_fields(task)`
     - `_remove_task_from_all_lists(task)`
     - `_set_task_status(task, status, error="")`
     - `_finalize_task(task, status, stage, notify=True)`
2. 修复 `add_task()`
   - 入队前先清理旧状态引用。
   - 统一重置：
     - `retry_count`
     - `error_message`
     - `stop_requested`
     - `stop_reason`
     - `process`
     - `started_at`
     - `completed_at`
3. 修复 `_worker()`
   - 对“已成功取出队列元素”的路径使用 `try/finally` 保证 `task_done()` 一定执行。
   - 避免 `_execute_download()` 抛异常时队列永远不 `task_done()`。
4. 修复 `_execute_download()`
   - 成功、失败、暂停、取消、关闭四类收尾统一走同一套出口。
   - 成功或失败前，先从其他状态列表移除，再进入目标列表。
   - `task.status`、列表归属、通知、统计、日志要保持一致。
5. 修复 `resume_task()`
   - 恢复前从 `failed_tasks` / `completed_tasks` / `active_tasks` 清理旧引用。
   - 恢复不要复用脏状态。
6. 修复 `cancel_task()` 和 `remove_task()`
   - 区分“取消任务”和“删除记录”。
   - 删除已完成任务记录时，不应再次触发取消逻辑。
7. 修复 `get_all_tasks()` / `get_stats()`
   - 同一任务不能因同时存在于多个列表而被重复计数。
   - 若保留多来源聚合，需按对象 ID 或任务 ID 去重。
8. 评估 `DownloadTask` 是否需要增加稳定字段
   - 例如：
     - `task_id`
     - `last_failure_stage`
     - `last_failure_kind`
     - `state_version`
   - 若引入，需同步 `ui/download_queue.py` 展示逻辑。
9. 修复 `ui/download_queue.py`
   - `add_or_update_task()` 必须支持同一任务对象的状态更新，而不是生成重复项。
   - `_on_clear_completed()` 与 `DownloadManager.remove_task()` 的语义保持一致。

### 6.4 阶段测试
1. 状态流测试
   - waiting -> downloading -> completed
   - waiting -> downloading -> failed
   - downloading -> paused -> waiting -> downloading -> completed
   - downloading -> cancelled -> removed
2. UI 一致性测试
   - 下载中心列表数量与 `get_stats()` 一致。
   - “清除已完成”后，任务记录消失，本地文件保留。
3. 边界测试
   - 同一任务连续恢复 3 次，不出现重复记录。
   - 删除失败任务、删除已完成任务、删除等待中任务都行为正确。

### 6.5 通过门禁
1. 任一任务在任一时刻只属于一个最终列表
2. 下载中心统计无重复
3. 恢复/重试/删除行为稳定
4. `compileall` 通过

## 7. 第三阶段：关闭流程与子进程清理（S3）

### 7.1 目标
- 保证暂停、取消、删除、程序关闭时都能彻底收口，不留下下载子进程和临时状态。

### 7.2 涉及文件与函数
- `core/download_manager.py`
  - `pause_task()`
  - `cancel_task()`
  - `_kill_process_tree()`
  - `shutdown()`
- `engines/n_m3u8dl_re.py`
  - `download()`
  - `_run_command()`
- `engines/ytdlp_engine.py`
  - `download()`
  - `_do_download()`
- `engines/streamlink_engine.py`
  - `download()`
- `engines/aria2_engine.py`
  - `download()`
- `core/playwright_driver.py`
  - `stop()`
  - `run()`

### 7.3 开发步骤
1. 统一 `shutdown()` 的进程清理方式
   - 不再只用 `terminate()`，改为复用 `_kill_process_tree()`。
2. 修复 `_kill_process_tree()`
   - 记录具体 kill 路径：
     - `taskkill`
     - `psutil`
     - `process.kill`
   - 清理失败时必须有日志。
3. 修复各引擎的 `task.process` 生命周期
   - 在进程退出后显式清空 `task.process`。
   - 防止 UI 对已经失效的进程句柄重复操作。
4. 修复 `pause_task()` / `cancel_task()`
   - 进程 kill 成功与否都要进入统一状态收尾。
   - 不允许“UI 显示已暂停，但后台进程仍在跑”。
5. 修复 `PlaywrightDriver.stop()`
   - 增加超时保护，避免 `wait()` 无限阻塞。
   - 关闭时确保 Playwright context/page 释放。
6. 若需要，补充下载临时目录清理策略
   - 明确哪些场景保留续传文件，哪些场景清理。

### 7.4 阶段测试
1. 下载中直接关闭程序，确认无残留下载进程。
2. 下载中暂停任务，确认引擎进程真实退出。
3. 下载中取消/删除任务，确认临时文件和状态一致。
4. 连续启动/关闭内置浏览器，确认不会卡死在退出阶段。

### 7.5 通过门禁
1. `N_m3u8DL-RE` / `yt-dlp` / `ffmpeg` / `streamlink` / `aria2c` 无孤儿进程残留
2. 程序退出耗时稳定
3. `compileall` 通过

## 8. 第四阶段：异常处理与日志可观测性（S4）

### 8.1 目标
- 将“失败了但不知道为什么”改为“能定位到模块、函数、阶段、异常类型、退出码”。

### 8.2 涉及文件与函数
- `main.py`
  - `main()`
- `protocol_handler.pyw`
  - `parse_n_m3u8dl_format()`
  - `parse_m3u8dl_url()`
  - `send_to_app()`
  - `launch_app_with_url()`
  - `main()`
- `core/catcatch_server.py`
  - `do_POST()`
  - `_handle_download_request()`
  - `_run_server()`
- `core/request_interceptor.py`
  - `interceptRequest()`
  - `_is_video_url()`
  - `_is_noise_url()`
- `core/playwright_driver.py`
  - `run()`
  - `_setup_page()`
  - `_handle_console()`
  - `_probe_dynamic_media_urls()`
  - `_build_default_headers()`
  - `export_cookies_to_file()`
- `engines/n_m3u8dl_re.py`
  - `download()`
  - `_run_command()`
  - `_build_command()`
- `engines/ytdlp_engine.py`
  - `_do_download()`
  - `_diagnose_failure()`
  - `get_formats()`

### 8.3 开发步骤
1. 全面清理裸 `except:` 与静默 `pass`
   - 替换为针对性异常类型。
   - 确实需要兜底的地方，至少记录 `module/function/error`。
2. 统一日志字段
   - 建议关键字段固定包含：
     - `event`
     - `engine`
     - `task`
     - `url`
     - `stage`
     - `error_type`
     - `exit_code`
3. 规范引擎错误输出
   - `n_m3u8dl_re` 和 `yt-dlp` 的尾部输出保留，但要附带归类后的失败原因。
4. 规范协议和 HTTP 入口错误
   - 协议解析失败、端口连接失败、JSON/form 解析失败分别记不同事件名。
5. 规范 Playwright 异常
   - 页面关闭、导航超时、脚本执行失败、Cookie 导出失败要区分记录。
6. 对用户可见错误做分层
   - 网络错误
   - 鉴权错误
   - 参数错误
   - 证书错误
   - 子进程退出错误

### 8.4 阶段测试
1. 人造错误测试
   - 伪造坏 URL
   - 伪造错误 headers
   - 占用监听端口
   - 断网或指向不存在域名
2. 检查日志
   - 失败时日志必须能定位到阶段和模块
   - UI 错误提示与日志原因一致

### 8.5 通过门禁
1. 关键链路不再存在裸 `except:` 和无日志 `pass`
2. 日志足以定位失败阶段
3. `compileall` 通过

## 9. 第五阶段：内置浏览器抓取与 m3u8 链路成功率（S5）

### 9.1 目标
- 提升内置浏览器抓取到的 m3u8 链接可用率和最终下载成功率。

### 9.2 涉及文件与函数
- `core/playwright_driver.py`
  - `_setup_page()`
  - `_handle_console()`
  - `_begin_capture_window()`
  - `_maybe_extend_capture_window()`
  - `_tick_capture_window()`
  - `_probe_dynamic_media_urls()`
  - `_normalize_emit_url()`
  - `_build_default_headers()`
  - `_is_recent_emit()`
  - `_emit_detected_resource()`
  - `_check_video_page()`
- `core/request_interceptor.py`
  - `interceptRequest()`
  - `_is_video_url()`
  - `_is_noise_url()`
- `core/m3u8_sniffer.py`
  - `add_resource()`
  - `_merge_resource_context()`
  - `_normalize_m3u8_headers()`
  - `_apply_site_rules()`
  - `_score_m3u8_candidate()`
- `core/m3u8_parser.py`
  - `M3U8FetchThread.run()`
  - `_is_master_playlist()`
  - `_resolve_nested_variants()`
  - `_parse_m3u8_variants()`
- `core/services/hls_probe.py`
  - `HLSProbe.probe()`
  - `_pick_first_variant()`
  - `_pick_key_url()`
  - `_pick_first_segment()`
- `ui/main_window.py`
  - `_on_resource_found()`
  - `_on_download_requested()`
  - `_show_m3u8_variant_dialog()`
  - `_start_download()`
  - `_on_catcatch_download()`
- `ui/resource_panel.py`
  - `add_resource()`
  - `_generate_dedup_key()`
  - `_parse_m3u8_variants()`

### 9.3 开发步骤
1. 修复 `PlaywrightDriver._setup_page()` 的重复绑定风险
   - 确保同一页面不会重复注册 `request` / `response` / `console` / `download` / `framenavigated` 事件。
   - 建议新增“页面已配置标记”。
2. 强化 `_build_default_headers()`
   - m3u8 资源优先补齐：
     - `referer`
     - `origin`
     - `user-agent`
     - `cookie`
   - 统一 header key 大小写策略。
3. 优化 `_emit_detected_resource()` 与 `M3U8Sniffer.add_resource()`
   - 对同 URL 多次命中做上下文合并，而不是首条即定案。
   - 认证相关头采用“最新非空值优先”。
4. 强化 `M3U8FetchThread.run()`
   - 区分 master/media playlist
   - 处理伪重定向 body
   - 保留解析失败时的响应信息
5. 强化 `M3U8FetchThread._resolve_nested_variants()`
   - 对多层嵌套 m3u8 做受控递归或深度限制。
   - 防止循环引用或无限跟进。
6. 强化 `HLSProbe.probe()`
   - 探测结果写入明确阶段：
     - `playlist`
     - `variant`
     - `key`
     - `segment`
   - 为下载前筛掉明显不可用链接提供依据。
7. 优化 `MainWindow._show_m3u8_variant_dialog()` 和 `_start_download()`
   - 保留 `master_url` / `media_url`
   - 保证变体选择和实际下载 URL 一致
8. 优化 `ResourcePanel.add_resource()` 和 `_generate_dedup_key()`
   - 保证同视频不同分辨率不被误去重
   - 同一链接多次命中只更新上下文，不反复污染列表

### 9.4 阶段测试
1. 样本站点回归
   - media playlist
   - master playlist
   - AES-128
   - 需要 Referer
   - 需要 Cookie
2. 成功率对比
   - 内置浏览器 vs 外部猫爪插件
   - 统计抓取命中率、下载成功率、失败阶段分布
3. 事件绑定测试
   - 切换标签页、重复导航、刷新页面后不出现重复抓取和重复日志

### 9.5 通过门禁
1. 内置浏览器 m3u8 抓取和下载成功率相对基线提升
2. 同 URL 资源上下文合并稳定
3. `compileall` 通过

## 10. 第六阶段：UI 文本、编码与结构整理（S6）

### 10.1 目标
- 清理乱码、降低大文件维护成本，避免 UI 继续成为回归热点。

### 10.2 涉及文件与函数
- `ui/main_window.py`
  - `_show_manual_dialog()`
  - `_run_quick_manual_script()`
- `utils/config_manager.py`
- `core/catcatch_server.py`
- `main.py`
- `protocol_handler.pyw`
- `ui/download_queue.py`
- `ui/resource_panel.py`

### 10.3 开发步骤
1. 统一源码编码为 UTF-8
   - 清理已污染的中文字符串和注释。
2. 拆分 `MainWindow._show_manual_dialog()`
   - 将大段手册文案移到独立资源文件，例如：
     - `MANUAL.md`
     - `resources/manual_sections/*.md`
   - UI 代码只负责加载和展示。
3. 统一 UI 文案来源
   - 将重复出现的按钮说明、脚本说明、下载提示收敛到集中位置。
4. 检查 `ui/download_queue.py` 和 `ui/resource_panel.py`
   - 将高频维护逻辑拆为更小的 helper，降低后续回归风险。

### 10.4 阶段测试
1. 打开主要 UI 页面，确认中文显示正常。
2. 快速手册、下载中心、资源列表、历史区均无乱码。
3. 手册入口脚本仍可正常运行。

### 10.5 通过门禁
1. 主要界面与日志无明显乱码
2. 手册与下载中心可正常使用
3. `compileall` 通过

## 11. 第七阶段：自动化测试与回归体系（S7）

### 11.1 目标
- 建立最小可用测试基线，让后续修复不再只靠人工回归。

### 11.2 建议新增文件
- `tests/test_config_manager.py`
- `tests/test_protocol_handler.py`
- `tests/test_catcatch_server.py`
- `tests/test_download_manager_state_machine.py`
- `tests/test_m3u8_parser.py`
- `tests/test_sniffer_merge.py`
- `tests/test_hls_probe.py`
- `tests/test_queue_clear_completed.py`
- `scripts/run_smoke_tests.bat`

### 11.3 开发步骤
1. 先补无 UI 或弱 UI 依赖的单元测试
   - `ConfigManager`
   - `protocol_handler`
   - `CatCatchServer`
   - `HLSProbe`
   - `M3U8Sniffer`
2. 再补下载管理器状态机测试
   - 成功、失败、暂停、恢复、取消、删除
3. 最后补轻量 UI 集成测试或脚本化烟测
   - 下载中心“清除已完成”
   - 资源入队与状态刷新
4. 固化回归命令
   - `python -m compileall main.py core ui engines utils protocol_handler.pyw`
   - `python -m pytest -q`

### 11.4 阶段测试
1. 关键单元测试全部通过。
2. 脚本化烟测可重复执行。
3. 新增修复点都能被测试覆盖到。

### 11.5 通过门禁
1. 至少建立一套可重复执行的本地回归命令
2. P0/P1 问题对应测试已落地
3. `compileall` 与 `pytest` 通过

## 12. 建议实施顺序
1. `S0` 基线准备
2. `S1` 配置、协议、服务正确性
3. `S2` 下载状态机与任务生命周期
4. `S3` 关闭流程与子进程清理
5. `S4` 异常处理与日志可观测性
6. `S5` 内置浏览器抓取与 m3u8 成功率
7. `S6` UI 文本、编码与结构整理
8. `S7` 自动化测试与回归体系

## 13. 阶段交付要求
- 每阶段交付物至少包括：
  - 代码修改
  - 变更说明
  - 本阶段测试结果
  - 回归结果
  - 未解决风险
- 阶段报告命名建议：
  - `plans/phase_reports/DEBUG_S0_report.md`
  - `plans/phase_reports/DEBUG_S1_report.md`
  - `plans/phase_reports/DEBUG_S2_report.md`
  - `plans/phase_reports/DEBUG_S3_report.md`
  - `plans/phase_reports/DEBUG_S4_report.md`
  - `plans/phase_reports/DEBUG_S5_report.md`
  - `plans/phase_reports/DEBUG_S6_report.md`
  - `plans/phase_reports/DEBUG_S7_report.md`

## 14. 当前建议的起始开发点
- 第一优先：`S1`
- 先改：
  - `utils/config_manager.py` 的 `ConfigManager.load()` / `_load_defaults()` / `save()`
  - `protocol_handler.pyw` 的 `send_to_app()` / `launch_app_with_url()` / `main()`
  - `core/catcatch_server.py` 的 `start()` / `_run_server()` / `stop()`
- 原因：
  - 这是当前最明确、最容易造成“配置失效”“协议投递异常”“服务假启动”的 P0 问题。
