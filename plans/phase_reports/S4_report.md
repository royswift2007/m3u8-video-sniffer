# S4 Report

## 阶段
- 阶段编号：S4（内置浏览器“播放后抓取窗口”）
- 执行日期：2026-03-13

## 代码改动范围
1. `core/playwright_driver.py`
- 新增播放后持续捕获窗口能力：
  - `_begin_capture_window`
  - `_tick_capture_window`
  - `_probe_dynamic_media_urls`
- 在 `navigate`、`url_change`、`video_page_match`、`CATCATCH_PLAY` 等时机开启或延长窗口。
- 统一资源发射入口 `_emit_detected_resource`，集中处理：
  - URL 规范化
  - 短时去重
  - 默认请求头补全（`referer/user-agent/origin`）
  - m3u8 Cookie 兜底注入
- `request/response/console` 三路检测统一接入窗口延长与上报逻辑。

2. `core/sniffer_script.py`
- `CATCATCH_DETECT` 输出扩展为 `URL|DURATION|SOURCE`。
- 新增 `CATCATCH_PLAY` 控制台事件，在 `<video>` `play` 时主动通知 Python 侧开启抓取窗口。

3. `utils/config_manager.py`
- 新增 S4 相关默认特性开关：
  - `browser_capture_window_enabled`
  - `browser_capture_window_seconds`
  - `browser_capture_extend_on_hit_seconds`
  - `browser_capture_probe_interval_ms`

## 测试执行记录

### T1 编译检查
- 命令：`python -m compileall core/playwright_driver.py core/sniffer_script.py utils/config_manager.py`
- 结果：通过。

### T2 播放事件触发窗口 + 探测补头验证（离线脚本）
- 方法：构造 `FakePage/FakeContext`，触发 `CATCATCH_PLAY`，执行 `_tick_capture_window`。
- 关键结果：
  - `capture_window_active=True`
  - `captured_count=1`
  - 首条为 `master.m3u8`
  - headers 含 `referer/origin/cookie`
- 结论：通过。

### T3 JS 检测消息格式兼容验证（离线脚本）
- 输入：`CATCATCH_DETECT:/hls/main.m3u8|125|MediaPlay`
- 关键结果：
  - `captured_count=1`
  - 归一化 URL：`https://site.test/hls/main.m3u8`
  - 标题附带时长：`[02:05]`
- 结论：通过。

### T4 配置与接入点检查
- 命令：`rg -n "CATCATCH_PLAY|CATCATCH_DETECT|_begin_capture_window|browser_capture_window" core/playwright_driver.py core/sniffer_script.py utils/config_manager.py`
- 结果：关键开关与接入点均存在。

## 门禁结论
- 是否允许进入下一阶段（S5）：是。
- 结论依据：T1/T2/T3/T4 全部通过。
