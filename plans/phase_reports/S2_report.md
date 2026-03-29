# S2 Report

## 阶段
- 阶段编号：S2（下载前 HLS 预探测）
- 执行日期：2026-03-13

## 代码改动范围
1. `core/services/hls_probe.py`（新增）
- 新增 `HLSProbe.probe`，执行 `playlist -> key -> segment` 三段预探测。
- 支持 master playlist 首个变体解析。

2. `core/download_manager.py`
- 在 m3u8 下载前接入预探测。
- 新增特性开关读取：`hls_probe_enabled`、`hls_probe_hard_fail`（默认 True）。
- 新增事件日志：`hls_probe_ok` / `hls_probe_failed` / `hls_probe_exception`。
- 探测硬失败时提前失败并给出失败阶段。

## 测试执行记录
### T1 编译检查
- 命令：`python -m compileall core/services/hls_probe.py core/download_manager.py`
- 结果：通过。

### T2 解析函数单测（离线）
- 使用内联字符串验证：
1. `_pick_first_variant` 可解析 master 首个变体
2. `_pick_key_url` 可解析 key URI 并补全绝对 URL
3. `_pick_first_segment` 可解析首个分片 URL
- 结果：三项均通过。

### T3 接入点检查
- 命令：`rg -n 'hls_probe_enabled|HLSProbe|hls_probe_failed' core/download_manager.py`
- 结果：接入与事件日志存在。

## 风险说明
- 当前阶段主要完成功能接入与门禁能力，端到端网络样本回归需在后续实网样本集上执行。

## 门禁结论
- 是否允许进入下一阶段（S3）：是。
- 结论依据：T1/T2/T3 全部通过。
