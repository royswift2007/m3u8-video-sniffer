# DEBUG S7 报告（自动化测试与回归体系）

## 本次完成项
1. 新增无 UI 依赖单测
- `tests/test_protocol_handler.py`
  - 覆盖命令行格式协议参数解析。
  - 覆盖 JSON 格式协议参数解析。
- `tests/test_hls_probe.py`
  - 覆盖 `HLSProbe.probe` 的 master -> key -> segment 成功链路。
  - 覆盖 playlist 阶段失败分支。
- `tests/test_m3u8_parser.py`
  - 覆盖 master 变体解析与分辨率排序。
  - 覆盖嵌套变体深度限制分支。

2. 修复协议解析缺陷
- `protocol_handler.pyw`
  - `parse_m3u8dl_url()` 改为优先识别 JSON payload（`{...}`），避免被误判为命令行格式导致 URL 为空。

3. 新增一键烟测脚本
- `scripts/run_smoke_tests.bat`
  - 固化命令：`compileall + pytest`，用于每阶段结束后的快速回归。

4. 新增第二批状态机与服务测试
- `tests/test_catcatch_server.py`
  - 覆盖 `/status` 健康检查。
  - 覆盖 `/download` POST 投递与参数透传。
- `tests/test_download_manager_state_machine.py`
  - 覆盖等待中任务 `pause_task`：出队并进入 `paused`。
  - 覆盖等待中任务 `cancel_task`：出队并进入 `failed`。
  - 覆盖等待中任务 `remove_task`：从管理器完全移除。
- `tests/test_engine_selector.py`
  - 覆盖空引擎场景抛出明确错误。

5. 小型强壮性修复
- `core/engine_selector.py`
  - `get_candidates()` 对空引擎列表返回空数组。
  - `select()` 在无候选引擎时抛出清晰异常：`无可用下载引擎，请检查引擎配置或二进制文件`。

6. 新增第三批异常分支与执行链路测试
- `tests/test_catcatch_server.py`
  - 覆盖非法 JSON 请求返回 `400`。
  - 覆盖未注册 handler 返回 `500`。
  - 覆盖端口被占用时自动回退到可用端口。
- `tests/test_download_manager_state_machine.py`
  - 覆盖 `HLSProbe` 硬失败路径：任务标记 `failed` 并记录失败通知。
  - 覆盖失败后重试成功路径：首轮失败、次轮成功，最终进入 `completed`。

7. 新增第四批并发交错场景测试
- `tests/test_download_manager_state_machine.py`
  - 覆盖执行中 `paused`：下载函数内触发停止请求后，任务进入 `paused`，且不误记失败。
  - 覆盖执行中 `cancelled`：下载函数内触发取消后，任务进入 `failed`（取消态）。
  - 覆盖执行中 `shutdown`：下载函数内触发关闭后，记录 `shutdown` 失败阶段指标，不进入失败列表。

8. 新增第五批下载队列 UI 逻辑回归测试（无 Qt 运行时依赖）
- `tests/test_download_queue_panel_logic.py`
  - 覆盖“清除已完成”：只移除完成任务，且触发 `task_removed`。
  - 覆盖“暂停全部”：仅对 `downloading` 任务触发 `task_paused`。
  - 覆盖状态过滤：仅显示匹配状态任务并隐藏其他行。
  - 采用 stub 方式调用 `DownloadQueuePanel` 方法，避免 UI 环境差异导致的测试不稳定。

9. 新增第六批协议投递链路测试（mock 端到端）
- `tests/test_protocol_handler.py`
  - 覆盖 `send_to_app` 端口轮询逻辑：遇到首个成功端口立即停止后续探测。
  - 覆盖 `launch_app_with_url` 启动失败分支：`Popen` 异常时返回 `False`。
  - 覆盖 `launch_app_with_url` 启动后回投成功分支：重试投递直至成功并返回 `True`。

10. 新增第七批入口层测试（main.py）
- `tests/test_main_entry.py`
  - 覆盖 `_merge_chromium_flags()`：确保自动化屏蔽参数可追加且不重复追加。
  - 覆盖 `parse_args()`：CLI 参数 `--url/--headers/--filename` 解析正确。
  - 说明：`main.main()` 完整 GUI 生命周期在当前自动化环境不稳定，已降级为入口纯函数测试，避免引入进程级假失败。

11. 新增第八批配置管理闭环测试（ConfigManager）
- `tests/test_config_manager.py`
  - 覆盖配置文件不存在时：加载默认值并落盘。
  - 覆盖部分配置缺项时：自动补齐缺省项且保留未知字段。
  - 覆盖损坏 JSON 时：回退默认配置并可继续运行。
  - 覆盖 `set/get` 点号路径读写与持久化。
  - 说明：受当前环境 `tmp_path` 权限限制影响，测试改为使用项目内 `Temp/pytest_config_manager` 路径，保证稳定执行。

12. 新增第九批 Sniffer 合并专项测试（M3U8Sniffer）
- `tests/test_sniffer_merge.py`
  - 覆盖 `add_resource()` 同 URL 去重后上下文合并（认证头最新非空优先）。
  - 覆盖 `_normalize_m3u8_headers()` 的键归一化与 referer/origin/user-agent 补齐。
  - 覆盖 `_apply_site_rules()` 站点规则补头逻辑。
  - 覆盖 `_score_m3u8_candidate()` 在认证上下文完善时评分提升。

## 阶段测试
- 语法检查：
  - `python -m compileall protocol_handler.pyw tests\test_protocol_handler.py tests\test_hls_probe.py tests\test_m3u8_parser.py`
  - 结果：通过。
- 单元测试：
  - `python -m pytest tests/test_protocol_handler.py tests/test_hls_probe.py tests/test_m3u8_parser.py -q -p no:cacheprovider`
  - 结果：`6 passed`。
- 一键烟测：
  - `cmd /c scripts\run_smoke_tests.bat`
  - 结果：通过（`11 passed`）。

- 全量测试：
  - `python -m pytest tests -q -p no:cacheprovider`
  - 结果：`35 passed`。

- 一键烟测：
  - `cmd /c scripts\run_smoke_tests.bat`
  - 结果：通过（`35 passed`）。

## 结论
- S7 第一批“可重复执行的本地回归命令 + 关键链路单测”已落地并通过。
- 下一步建议继续补 `CatCatchServer` 和 `DownloadManager` 状态机测试。
