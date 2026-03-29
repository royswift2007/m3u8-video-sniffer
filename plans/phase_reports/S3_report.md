# S3 Report

## 阶段
- 阶段编号：S3（重试策略重排：鉴权优先、同引擎优先）
- 执行日期：2026-03-13

## 代码改动范围
1. `core/download_manager.py`
- 新增配置读取：
  - `features.download_auth_retry_first`
  - `features.download_auth_retry_per_engine`
- 在鉴权失败（`auth`）后，优先执行同引擎重试，再进入引擎回退。
- 新增结构化事件日志：
  - `download_auth_retry`
  - `download_auth_retry_failed`
- 保持暂停/取消语义优先：检测到 stop 请求后立即中断后续回退与重试。

2. `utils/config_manager.py`
- 在默认 `features` 中补齐：
  - `download_auth_retry_first: true`
  - `download_auth_retry_per_engine: 1`
  - `hls_probe_enabled: true`
  - `hls_probe_hard_fail: true`

## 测试执行记录

### T1 编译检查
- 命令：`python -m compileall core/download_manager.py utils/config_manager.py`
- 结果：通过。

### T2 鉴权优先同引擎重试验证
- 方法：内联脚本注入假引擎（`N_m3u8DL-RE` 恒返回 403，`yt-dlp` 成功），并配置 `download_auth_retry_per_engine=1`。
- 预期：调用顺序应为 `N -> N(auth retry) -> yt-dlp`。
- 实际结果：
  - `n_calls=2`
  - `y_calls=1`
  - `status=completed`
  - 第二次 N 调用 headers 已包含补全的 `referer/origin`
- 结论：通过。

### T3 暂停不触发错误回退验证
- 方法：内联脚本让首引擎返回失败前设置 `task.stop_requested=True, stop_reason='paused'`。
- 预期：不应继续尝试 `yt-dlp`。
- 实际结果：
  - `n_calls=1`
  - `y_calls=0`
  - `status=paused`
- 结论：通过。

### T4 与 S2 联动检查
- 命令：`rg -n "hls_probe_enabled|hls_probe_hard_fail|download_auth_retry_first|download_auth_retry_per_engine" core/download_manager.py utils/config_manager.py`
- 结果：S2 与 S3 特性开关共存，读取与默认值均存在。

## 门禁结论
- 是否允许进入下一阶段（S4）：是。
- 结论依据：T1/T2/T3/T4 全部通过。
