# M3U8D 下载进度显示链路函数级分步修复计划

> 生成时间：2026-07-04  
> 审查重点：下载进度显示链路、进度解析、状态机、UI 队列刷新、历史调试代码清理  
> 参考资料：`plans/M3U8 视频嗅探器（M3U8D v0.5.0）全面严格审查计划.md`  
> 当前基线：`python -m pytest tests/test_base_engine_read_loop.py tests/test_nm3u8dl_re_progress_parsing.py tests/test_download_queue_panel_logic.py tests/test_download_manager_state_machine.py -q` 已通过，结果为 `46 passed in 1.52s`。

---

## 0. 总目标、交付物与验收标准

### 0.1 总目标

彻底修复并稳定 M3U8D 的下载进度显示链路，避免继续通过零散补丁叠加导致新竞态、新日志噪声和新状态机错误。修复完成后，应满足：

1. 下载实际进行时，下载中心能稳定显示进度、速度或至少显示“已下载分片/已写入大小”。
2. N_m3u8DL-RE stdout/stderr 被缓冲时，UI 也不会长时间卡在 `0.0%`。
3. 高频进度输出不会淹没 Qt 主线程 queued signal，不会拖慢 UI。
4. 重试、多候选地址、引擎 fallback 时，进度状态不会无解释回退或停留在错误状态。
5. 调试/RAW 日志默认不刷屏，不泄露 URL token / Cookie / Referer 等敏感信息。
6. 进度相关字段跨线程读写一致，TaskSnapshot 成为 UI 展示的唯一可靠数据源。

### 0.2 交付物

- 代码修复：
  - `engines/base_engine.py`
  - `engines/n_m3u8dl_re.py`
  - `engines/ytdlp_engine.py`
  - `engines/aria2_engine.py`
  - `engines/streamlink_engine.py`
  - `core/task_model.py`
  - `core/download/manager.py`
  - `core/download/task_queue.py`（可选后续）
  - `ui/main_window.py`
  - `ui/download_queue.py`
  - `utils/headers.py` / `utils/redact.py`（如需补强日志脱敏）
- 测试新增/更新：
  - `tests/test_base_engine_read_loop.py`
  - `tests/test_nm3u8dl_re_progress_parsing.py`
  - `tests/test_download_manager_state_machine.py`
  - `tests/test_download_queue_panel_logic.py`
  - 建议新增：`tests/test_download_progress_flow.py`
  - 建议新增：`tests/test_download_progress_throttle.py`
- 文档/清理：
  - 清理调试日志、过时 ISS 注释、误导性阶段注释。
  - 在 changelog 或计划文档中记录进度链路最终设计。

### 0.3 总体验收标准

最低自动化验收：

```bash
python -m pytest tests/test_base_engine_read_loop.py tests/test_nm3u8dl_re_progress_parsing.py tests/test_download_queue_panel_logic.py tests/test_download_manager_state_machine.py -q
```

修复后新增测试也应纳入：

```bash
python -m pytest tests/test_download_progress_flow.py tests/test_download_progress_throttle.py -q
```

人工验收：

1. 使用普通 `.m3u8` 视频下载，下载中心在 1 秒内从 `0.0%` 变为实时进度或已下载分片。
2. 使用 N_m3u8DL-RE 下载长分片视频，日志不再每个进度行输出 INFO RAW。
3. 暂停、取消、删除任务后，进度不再复活被删除任务，不出现 Qt 崩溃。
4. 下载失败后重试，UI 状态能从 `等待中` 回到 `下载中`，不会长期卡住。
5. 日志中 URL 的 `token/sign/auth/signature` 等敏感参数被脱敏。

### 0.4 约束

1. 保持现有引擎 `download(task, progress_callback) -> bool` 外部契约兼容。
2. 不在 UI 主线程增加网络 I/O。
3. 不破坏暂停/取消/删除的进程终止时延目标。
4. 优先补测试再修核心链路，避免继续“盲修”。
5. 进度 UI 的最终来源应是 `TaskSnapshot`，raw `DownloadTask` 只保留控制句柄用途。

### 0.5 阶段测试门禁（强制执行）

本计划采用“**完成一步、测试一步、测试通过后再进入下一步**”的修复纪律，避免再次出现连续叠加补丁后难以定位回归的问题。

1. 每个 P 阶段、子步骤或最小优先修复项完成后，必须先运行该步骤列出的自动化测试。
2. 只有对应测试全部通过后，才允许进入下一步修复。
3. 如果测试失败，必须立即停止后续修复，先定位并修复当前失败原因；禁止在已有失败未解决时继续叠加下一阶段改动。
4. 每次阶段测试都应记录：
   - 修改范围；
   - 测试命令；
   - 测试结果；
   - 失败原因（如有）；
   - 修复说明（如有）。
5. 如果某个步骤暂时没有专属测试，至少运行 `0.3 总体验收标准` 中的最低自动化验收命令。
6. 对 P0-P10 的推进必须按顺序执行：当前阶段测试通过后才能进入下一阶段；任何阶段测试失败，都必须停留在当前阶段完成修复与复测。

---

## 1. 当前下载进度链路审查结论

### 1.1 当前链路

```text
引擎子进程 stdout/stderr
  → engines/base_engine.py::_pump()
  → engines/base_engine.py::BaseEngine.read_loop()
  → engines/*::_parse_line() / parse_progress()
  → core/download/manager.py::_execute_download().progress_callback()
  → DownloadTask.progress / speed / downloaded_size
  → core/task_model.py::TaskSnapshot.from_task()
  → ui/main_window.py::_on_task_snapshot() / _task_snapshot_thread_hop
  → ui/main_window.py::task_update_received()
  → ui/download_queue.py::add_or_update_snapshot()
  → ui/download_queue.py::_update_item_from_snapshot()
```

### 1.2 当前最大风险点

| 编号 | 严重度 | 风险 | 核心位置 |
| --- | --- | --- | --- |
| H-01 | 高 | 进度事件无节流，日志与 Qt queued signal 可能淹没 UI | `core/download/manager.py::progress_callback`、`engines/n_m3u8dl_re.py::_parse_progress_fragment` |
| H-02 | 高 | N_m3u8DL temp fallback 依赖 stdout 先给 total，stdout 卡住时兜底失效 | `engines/n_m3u8dl_re.py::_start_temp_progress_monitor` |
| H-03 | 高 | 重试/多候选地址导致 UI 状态不刷新或进度回退 | `core/download/manager.py::_execute_download`、`engines/n_m3u8dl_re.py::_run_command` |
| H-04 | 高 | RAW/URL 日志存在敏感信息泄露与刷屏风险 | `engines/n_m3u8dl_re.py`、`core/download/manager.py` |
| M-01 | 中 | 速度单位不统一，`M/s` 等格式无法被 snapshot 正确解析 | `engines/n_m3u8dl_re.py::_convert_speed_to_mbs`、`core/task_model.py::_coerce_speed_bps` |
| M-02 | 中 | TaskSnapshot 字段不足，UI snapshot 渲染仍回读 raw task | `core/task_model.py::TaskSnapshot`、`ui/download_queue.py::_update_item_from_snapshot` |
| M-03 | 中 | 排序/语言刷新路径绕过 snapshot，可能覆盖实时进度 | `ui/download_queue.py::retranslate_ui`、`_rebuild_tree` |
| M-04 | 中 | progress/speed/engine 等字段部分未加锁写入 | `core/download/manager.py`、`core/task_model.py` |
| M-05 | 中 | 已抽取 `TaskQueue` 但生产 manager 仍操作 `Queue.mutex/.queue` | `core/download/manager.py`、`core/download/task_queue.py` |

---

## 2. 分阶段修复总览

建议按以下顺序执行，前四步优先解决“下载进度不显示”的主问题：

> **阶段推进规则：** P0 完成并测试通过后才能进入 P1；P1 测试通过后才能进入 P2；以此类推。任何阶段或子步骤测试失败，都必须停止后续修复，先修复失败原因并复测通过。

1. **P0：建立进度链路测试与基线**
2. **P1：清理 RAW/敏感/高频日志**
3. **P2：DownloadManager 进度事件节流与合并**
4. **P3：N_m3u8DL-RE stdout 卡住时的 temp fallback 修复**
5. **P4：重试、多候选、fallback 的进度状态一致性**
6. **P5：TaskSnapshot 字段补全，UI 不再回读 raw task 显示字段**
7. **P6：UI 所有重绘路径统一走 snapshot**
8. **P7：进度相关字段锁一致性**
9. **P8：进程终止 expected_name guard 与残留风险修复**
10. **P9：非进度但相关的架构/安全清理**
11. **P10：全量回归、真实样本验证与文档收尾**

---

# P0：建立进度链路测试与基线

## P0.1 新增端到端进度链路单测

### 目标

用自动化测试锁定“引擎 progress callback → DownloadManager → TaskSnapshot → UI 队列”的主链路，防止后续再次出现“进度解析到了但 UI 显示 0”的回归。

### 建议新增文件

- `tests/test_download_progress_flow.py`

### 需要覆盖的函数

- `core/download/manager.py::DownloadManager._execute_download`
- `core/download/manager.py::DownloadManager._emit_snapshot`
- `core/task_model.py::TaskSnapshot.from_task`
- `ui/download_queue.py::DownloadQueuePanel.add_or_update_snapshot`
- `ui/download_queue.py::DownloadQueuePanel._update_item_from_snapshot`

### 测试用例设计

#### 用例 1：普通进度事件能变成 snapshot

构造 fake engine：

```python
class _ProgressEngine(BaseEngine):
    def download(self, task, progress_callback):
        progress_callback({"progress": 12.34, "speed": "1.00 MB/s", "downloaded": "10MB"})
        return True
```

断言：

- `on_task_snapshot` 收到至少一个 `status="downloading"` 且 `progress=12.34` 的 snapshot。
- 完成后最终 snapshot 为 `status="completed"` 且 `progress=100.0`。

#### 用例 2：snapshot 能驱动 UI item

复用 `tests/test_download_queue_panel_logic.py` 中的 `_SnapshotPanelStub`，断言：

- `item.text(2) == "12.3%"`
- speed/downloaded 显示符合预期。

### 验收标准

新增测试失败时能复现“进度链路断裂”，修复后通过。

---

## P0.2 新增高频进度节流测试骨架

### 建议新增文件

- `tests/test_download_progress_throttle.py`

### 需要覆盖的未来函数

- `core/download/manager.py::DownloadManager._should_emit_progress_snapshot`
- `core/download/manager.py::DownloadManager._record_progress_snapshot_emit`

### 测试用例设计

1. 1000 次小幅度进度变化不应触发 1000 次 UI snapshot。
2. 进度从 `0` 变为正数必须立即发。
3. 状态变化必须立即发。
4. 完成/失败/暂停必须强制发。
5. 直播流 `progress=-1` 时，downloaded 文本变化应按时间节流发。

### 验收标准

节流函数可单测，不依赖真实 Qt 和真实子进程。

---

# P1：清理 RAW/敏感/高频日志

## P1.1 降级或删除 N_m3u8DL-RE RAW 进度日志

### 文件与函数

- 文件：`engines/n_m3u8dl_re.py`
- 函数：`N_m3u8DL_RE_Engine._parse_progress_fragment`
- 当前位置：包含如下逻辑：

```python
if "%" in line or "B/s" in line or "iB/s" in line or "Kbps" in line or "Mbps" in line:
    logger.info(f"[N_m3u8DL-RE RAW] {line}")
```

### 修改方案

1. 默认删除该 INFO 日志。
2. 如仍需排错，改为配置/环境变量控制：

```python
if os.environ.get("M3U8D_PROGRESS_DEBUG") == "1":
    logger.debug("[N_m3u8DL-RE RAW] %s", _redact_progress_line(line))
```

3. `_redact_progress_line()` 不应输出 URL/Cookie，只保留进度、速度、分片数字。

### 测试

- 在 `tests/test_nm3u8dl_re_progress_parsing.py` 中新增：
  - 默认解析进度不产生 INFO RAW 日志。
  - 开启 debug env 时只产生 DEBUG，且不含 token-like 字符串。

### 验收

正常下载时日志不再被进度行刷屏。

---

## P1.2 N_m3u8DL-RE URL 日志统一脱敏

### 文件与函数

- `engines/n_m3u8dl_re.py::download`
- `engines/n_m3u8dl_re.py::_build_url_candidates`

### 当前风险点

```python
logger.info(f"[N_m3u8DL-RE] 尝试地址: {source_label} -> {source_url}")
```

### 修改方案

1. 引入：

```python
from utils.redact import redact_url
```

2. 改为：

```python
logger.info(
    "[N_m3u8DL-RE] 尝试地址: %s -> %s",
    source_label,
    redact_url(source_url),
    event="nm3u8dlre_source_try",
)
```

3. 请求头摘要不要直接打印完整 Referer/Origin/UA。建议只打印：

```text
has_referer=True referer_host=example.com has_origin=True ua_len=... cookie_len=...
```

### 测试

- 新增日志脱敏单测：URL 中 `?token=abc&sign=xyz` 不出现在 caplog。

### 验收

所有引擎重试/候选 URL 日志不出现敏感 query 明文。

---

## P1.3 DownloadManager 候选排序日志脱敏

### 文件与函数

- `core/download/manager.py::DownloadManager._rank_task_candidates`

### 当前风险点

日志输出 `best` 和 `ranked` 原始 URL。

### 修改方案

1. 引入 `redact_url`。
2. `best`、`ranked` 日志使用脱敏 URL。
3. `candidate_scores` 内部可保留原始 URL，但不要进入日志。

### 测试

- `tests/test_download_manager_state_machine.py` 新增候选排序日志脱敏测试。

---

# P2：DownloadManager 进度事件节流与合并

## P2.1 新增进度 emit 状态表

### 文件与函数

- 文件：`core/download/manager.py`
- 函数：`DownloadManager.__init__`

### 新增字段

```python
self._progress_emit_state: dict[int, dict[str, object]] = {}
self._progress_emit_lock = threading.Lock()
```

### 新增常量

放在 `core/download/manager.py` 顶部：

```python
_PROGRESS_EMIT_MIN_INTERVAL_S = 0.5
_PROGRESS_EMIT_MIN_DELTA_PERCENT = 0.2
```

### 设计说明

- key 使用 `id(task)`，保持与现有 UI `task_id=str(id(task))` 一致。
- terminal state 后清理该 key，避免内存泄漏。

---

## P2.2 新增 `_should_emit_progress_snapshot()`

### 文件与函数

- 文件：`core/download/manager.py`
- 新增函数：`DownloadManager._should_emit_progress_snapshot`

### 建议签名

```python
def _should_emit_progress_snapshot(
    self,
    task: DownloadTask,
    *,
    progress: float,
    speed: str,
    downloaded: str,
    status: str,
    now: float | None = None,
    force: bool = False,
) -> bool:
    ...
```

### 发射规则

返回 `True` 的情况：

1. `force=True`。
2. task 没有发过 snapshot。
3. `status` 与上次发射不同。
4. 进度从 `<=0` 变成 `>0`。
5. `progress_delta >= 0.2`。
6. 距离上次发射 `>= 0.5s`。
7. `progress < 0` 且 `downloaded` 文本变化，并距离上次发射 `>= 0.5s`。
8. `speed` 从空变非空，并距离上次发射 `>= 0.5s`。

### 不发射的情况

1. 高频重复相同进度。
2. 进度轻微抖动。
3. 已删除任务。

---

## P2.3 新增 `_record_progress_snapshot_emit()`

### 文件与函数

- 文件：`core/download/manager.py`
- 新增函数：`DownloadManager._record_progress_snapshot_emit`

### 建议签名

```python
def _record_progress_snapshot_emit(
    self,
    task: DownloadTask,
    *,
    progress: float,
    speed: str,
    downloaded: str,
    status: str,
    now: float | None = None,
) -> None:
    ...
```

### 字段

```python
{
    "last_emit_time": now,
    "last_progress": progress,
    "last_speed": speed,
    "last_downloaded": downloaded,
    "last_status": status,
}
```

---

## P2.4 修改 `progress_callback()` 使用节流

### 文件与函数

- 文件：`core/download/manager.py`
- 函数：`DownloadManager._execute_download` 内部闭包 `progress_callback`

### 当前逻辑

每次引擎进度都会：

```python
self._emit_snapshot(task)
```

### 修改方案

1. 更新 task 字段仍然每次做。
2. UI snapshot 根据 `_should_emit_progress_snapshot()` 决定是否发。
3. 发射后调用 `_record_progress_snapshot_emit()`。

伪代码：

```python
should_emit = self.on_task_update or self.on_task_snapshot
if should_emit and self._should_emit_progress_snapshot(
    task,
    progress=progress_value,
    speed=speed_value,
    downloaded=downloaded_value,
    status=current_status,
):
    self._emit_snapshot(task)
    self._record_progress_snapshot_emit(...)
```

### 注意

完成/失败/暂停等终态不应依赖节流，应强制 `_emit_snapshot()`。

---

## P2.5 终态清理 progress emit state

### 文件与函数

- `core/download/manager.py::DownloadManager._execute_download`
- `core/download/manager.py::DownloadManager.remove_task`
- `core/download/manager.py::DownloadManager.cancel_task`
- `core/download/manager.py::DownloadManager.shutdown`

### 新增函数

```python
def _forget_progress_emit_state(self, task: DownloadTask) -> None:
    with self._progress_emit_lock:
        self._progress_emit_state.pop(id(task), None)
```

### 调用位置

- completed
- failed
- removed
- shutdown-drained waiting task

---

## P2.6 测试

### 文件

- `tests/test_download_progress_throttle.py`
- `tests/test_download_manager_state_machine.py`

### 测试点

1. 1000 条相同进度只产生少量 snapshot。
2. `0 → 0.1` 即使小于 delta，也应发一次。
3. `downloading → completed` 必须发，不被节流挡住。
4. `progress=-1` 时 downloaded 文本变化可按节流发。

---

# P3：N_m3u8DL-RE stdout 卡住时的 temp fallback 修复

## P3.1 `parse_progress()` 补充 downloaded 解析

### 文件与函数

- 文件：`engines/n_m3u8dl_re.py`
- 函数：`N_m3u8DL_RE_Engine.parse_progress`

### 当前问题

只解析百分比和速度，不解析：

```text
17.10MB/387.52MB
17/340
```

### 修改方案

解析 downloaded：

```python
size_match = re.search(r"([0-9.]+\s*[KMGT]?i?B)\s*/\s*([0-9.]+\s*[KMGT]?i?B)", line, re.I)
if size_match:
    result["downloaded"] = f"{size_match.group(1)}/{size_match.group(2)}"
else:
    seg_match = re.search(r"\b(\d+)\s*/\s*(\d+)\b", line)
    if seg_match:
        result["downloaded"] = f"{seg_match.group(1)}/{seg_match.group(2)} segments"
```

### 测试

- `tests/test_nm3u8dl_re_progress_parsing.py` 增加：
  - `17.10MB/387.52MB` 能写入 downloaded。
  - `17/340` 能写入 segments。

---

## P3.2 提取总分片数学习函数

### 文件与函数

- 文件：`engines/n_m3u8dl_re.py`
- 当前函数：`_update_progress_state_from_fragment`
- 建议新增纯函数：`_extract_total_segments_from_text`

### 建议签名

```python
@staticmethod
def _extract_total_segments_from_text(text: str) -> int:
    ...
```

### 识别格式

- `17/340`
- `340 Segments`
- 未来可扩展 metadata 文本。

### 好处

单测更清晰，`_update_progress_state_from_fragment` 只负责写 state。

---

## P3.3 temp monitor 在 total unknown 时也发“已下载分片”

### 文件与函数

- 文件：`engines/n_m3u8dl_re.py`
- 函数：`N_m3u8DL_RE_Engine._start_temp_progress_monitor`

### 当前问题

```python
if total <= 0:
    continue
```

这会导致 stdout 卡住且 total 未知时完全无 UI 反馈。

### 修改方案

当 `total <= 0` 但 `downloaded_count > 0` 时，发 unknown-progress payload：

```python
payload = {
    "progress": -1,
    "speed": "",
    "downloaded": f"{downloaded_count} segments",
}
progress_callback(payload)
```

当后续 total 已知后，再发百分比：

```python
progress = min(99.0, downloaded_count / total * 100.0)
```

### 注意

- `progress=-1` 会让 UI 显示 downloaded 文本。
- DownloadManager 节流逻辑必须允许 `downloaded` 文本变化触发 snapshot。

### 测试

在 `tests/test_nm3u8dl_re_progress_parsing.py` 新增：

1. `state={"total_segments": 0}` 且 temp 目录有 segment 文件时，monitor 发 `progress=-1`。
2. 后续 state total 变为 4 时，monitor 发 `25.0%`。

---

## P3.4 尝试从任务/manifest 预置 total_segments

### 文件与函数

- 文件：`engines/n_m3u8dl_re.py`
- 函数：`N_m3u8DL_RE_Engine._run_command`
- 建议新增函数：`_seed_progress_state_from_task`

### 建议签名

```python
def _seed_progress_state_from_task(self, task: DownloadTask, state: dict, state_lock: threading.Lock) -> None:
    ...
```

### 可选来源

按低风险顺序：

1. `task.probe_result["segment_count"]`，如果 HLSProbe 已提供。
2. `task.selected_variant` 内的 segment count，如果已有。
3. N_m3u8DL temp metadata 文件，例如 `meta.json` / `raw.json` / `mediainfo.json`。
4. 后续再考虑后台 m3u8 parser，不要在 UI 主线程做网络 I/O。

### 验收

stdout 完全没有进度行时，也能尽早显示基于 temp 文件的估算进度。

---

# P4：重试、多候选、fallback 的进度状态一致性

## P4.1 retry 后重新进入 downloading 时立即发 snapshot

### 文件与函数

- 文件：`core/download/manager.py`
- 函数：`DownloadManager._execute_download`
- 位置：backoff 后：

```python
if not self._is_task_stop_requested(task):
    task.transition("downloading")
```

### 修改方案

改为：

```python
if not self._is_task_stop_requested(task):
    task.transition("downloading")
    if self.on_task_update or self.on_task_snapshot:
        self._emit_snapshot(task)
```

### 测试

- 在 `tests/test_download_manager_state_machine.py` 中新增：
  - fake engine 第一次失败、第二次成功。
  - 收集 snapshots。
  - 断言序列包含 `waiting → downloading → waiting → downloading → completed`。

---

## P4.2 引入 attempt/source 概念，防止无解释进度回退

### 文件与函数

- `core/download/manager.py::DownloadManager._execute_download`
- `core/download/manager.py::_try_download`
- `engines/n_m3u8dl_re.py::_run_command`
- `engines/n_m3u8dl_re.py::_parse_progress_fragment`

### 最小兼容方案

不改变引擎 `download()` 签名，只在 progress payload 中允许可选字段：

```python
{
    "progress": 5.0,
    "speed": "...",
    "downloaded": "...",
    "source": "primary",
    "attempt": 1,
}
```

### DownloadManager 行为

1. 同一 attempt 内，进度不允许明显回退：

```python
if same_attempt and progress_value >= 0 and previous_progress >= 0:
    progress_value = max(previous_progress, progress_value)
```

2. 新 attempt 可以回退，但需要 snapshot 中带 attempt/source，UI 可显示“重试中”。

### 测试

- 同一 attempt：`20% → 10%`，最终显示保持 `20%`。
- 新 attempt：允许 `20% → 0%`，但 snapshot attempt 增加。

---

## P4.3 N_m3u8DL 多 source 下载时标记 source_label

### 文件与函数

- 文件：`engines/n_m3u8dl_re.py`
- 函数：`_run_command`

### 修改方案

在 `_run_command` 内包一层 callback：

```python
def source_progress_callback(payload: dict):
    payload = dict(payload or {})
    payload.setdefault("source", source_label)
    progress_callback(payload)
```

然后所有 `_parse_progress_fragment(... progress_callback=...)` 使用 `source_progress_callback`。

### 验收

日志和 snapshot 能区分 `primary`、`master`、`media`、`safe`、`lowcon`。

---

# P5：TaskSnapshot 字段补全，UI 不再回读 raw task 显示字段

## P5.1 扩展 TaskSnapshot 数据模型

### 文件与函数

- 文件：`core/task_model.py`
- 类：`TaskSnapshot`
- 函数：`TaskSnapshot.from_task`
- 函数：`TaskSnapshot.to_dict`

### 新增字段建议

```python
speed_text: str
downloaded_text: str
save_dir: str
attempt: int | None
source_label: str | None
```

### 修改点

1. dataclass 字段新增。
2. `_SERIALIZED_FIELDS` 增加字段。
3. `to_dict()` 输出稳定顺序。
4. `from_task()` 在 `task.lock` 内读取：
   - `task.speed`
   - `task.downloaded_size`
   - `task.save_dir`
   - `getattr(task, "_download_attempt", None)`
   - `getattr(task, "_download_source_label", None)`

### 注意

- 更新 `scripts/lint_main_window_slots.py` 不一定需要，因为只检查类型注解。
- 更新 `scripts/smoke_main_window_slots.py` 中构造 TaskSnapshot 的样例。

---

## P5.2 更新 UI snapshot 渲染

### 文件与函数

- 文件：`ui/download_queue.py`
- 函数：`DownloadQueuePanel._update_item_from_snapshot`

### 当前问题

该函数仍回读 raw task：

```python
task = self.tasks.get(task_id)
save_path = getattr(task, "save_path", "") or getattr(task, "save_dir", "") or "" if task else ""
downloaded_size = getattr(task, "downloaded_size", "") or "" if task else ""
if not speed and task is not None:
    speed = getattr(task, "speed", "") or ""
```

### 修改方案

改为只用 snapshot：

```python
save_path = snapshot.save_dir or ""
downloaded_size = snapshot.downloaded_text or ""
speed = snapshot.speed_text or format_speed_bps(snapshot.speed_bps)
```

### 测试

- `tests/test_download_queue_panel_logic.py`
  - raw task 被改成 `progress=0/status=failed` 后，snapshot 显示仍保持。
  - snapshot 的 `downloaded_text` 能显示在 progress=-1 场景。

---

# P6：UI 所有重绘路径统一走 snapshot

## P6.1 `retranslate_ui()` 优先使用 snapshot

### 文件与函数

- 文件：`ui/download_queue.py`
- 函数：`DownloadQueuePanel.retranslate_ui`

### 当前问题

```python
for task_id, item in self.task_items.items():
    task = self.tasks.get(task_id)
    if task:
        self._update_item(item, task)
```

会绕过 snapshot。

### 修改方案

```python
for task_id, item in self.task_items.items():
    snapshot = self.task_snapshots.get(task_id)
    if snapshot is not None:
        self._update_item_from_snapshot(item, snapshot)
        continue
    task = self.tasks.get(task_id)
    if task:
        self._update_item(item, task)
```

### 测试

- 先 snapshot 25%，再 raw task 0%，调用 `retranslate_ui()`，仍显示 25%。

---

## P6.2 `_rebuild_tree()` 优先使用 snapshot

### 文件与函数

- 文件：`ui/download_queue.py`
- 函数：`DownloadQueuePanel._rebuild_tree`

### 当前问题

排序后只用 raw task 绘制。

### 修改方案

1. 重建 item 时先根据 `task_id=str(id(task))` 找 snapshot。
2. 有 snapshot 则 `_update_item_from_snapshot()`。
3. 没有 snapshot 才 `_update_item()`。

### 测试

- 先 snapshot 30%，调用 `_on_sort_by_status()`，仍显示 30%。

---

## P6.3 抽取统一渲染 helper

### 文件与函数

- 文件：`ui/download_queue.py`
- 新增函数：`DownloadQueuePanel._render_item_for_task_id`

### 建议签名

```python
def _render_item_for_task_id(self, task_id: str, item: QTreeWidgetItem) -> None:
    snapshot = self.task_snapshots.get(task_id)
    if snapshot is not None:
        self._update_item_from_snapshot(item, snapshot)
        return
    task = self.tasks.get(task_id)
    if task is not None:
        self._update_item(item, task)
```

### 替换调用点

- `add_or_update_task`
- `add_or_update_snapshot`
- `retranslate_ui`
- `_rebuild_tree`

---

# P7：进度相关字段锁一致性

## P7.1 扩展 `_LOCKED_FIELDS`

### 文件与函数

- 文件：`core/task_model.py`
- 类：`DownloadTask`
- 字段：`_LOCKED_FIELDS`

### 当前字段

```python
("status", "stop_requested", "stop_reason", "process", "retry_count", "error_message")
```

### 建议扩展

```python
(
    "status",
    "stop_requested",
    "stop_reason",
    "process",
    "retry_count",
    "error_message",
    "progress",
    "speed",
    "downloaded_size",
    "engine",
)
```

### 风险

扩大 `_set_fields_locked` 可写字段后，需要逐步迁移调用点，避免遗漏。

---

## P7.2 新增 `DownloadTask.update_progress_locked()`

### 文件与函数

- 文件：`core/task_model.py`
- 类：`DownloadTask`

### 建议实现

```python
def update_progress_locked(self, *, progress: float, speed: str = "", downloaded_size: str = "") -> tuple[float, str]:
    with self.lock:
        previous = float(self.progress or 0.0)
        self.progress = progress
        self.speed = speed
        self.downloaded_size = downloaded_size
        return previous, self.status
```

### 替换位置

- `core/download/manager.py::progress_callback`

---

## P7.3 替换 manager 内未加锁写入

### 文件与函数

- `core/download/manager.py::_reset_task_runtime`
- `core/download/manager.py::_try_download`
- `core/download/manager.py::_execute_download` completed 分支

### 修改点

```python
task.speed = ""
task.downloaded_size = ""
task.progress = 0.0
task.engine = engine_name
task.progress = 100.0
```

统一改为 `_set_fields_locked(...)` 或新 helper。

### 测试

- 快照读取期间模拟并发 progress 更新，不产生不一致字段。

---

# P8：进程终止 expected_name guard 与残留风险修复

## P8.1 manager kill 调用传 expected engine name

### 文件与函数

- 文件：`core/download/manager.py`
- 函数：
  - `pause_task`
  - `cancel_task`
  - `remove_task`
  - `shutdown`

### 当前调用

```python
self._kill_process_tree(task.process)
```

### 修改方案

```python
self._kill_process_tree(
    task.process,
    expected_name=getattr(task, "_expected_engine_name", None),
)
```

### 测试

- mock `_kill_process_tree`，确认 expected_name 被传递。

---

## P8.2 BaseEngine.read_loop escalation 传 expected_name

### 文件与函数

- 文件：`engines/base_engine.py`
- 函数：`BaseEngine.read_loop`

### 当前调用

```python
kill_process_tree(proc.pid)
```

### 修改方案

```python
kill_process_tree(
    proc.pid,
    expected_name=getattr(task, "_expected_engine_name", None),
)
```

### 测试

- `tests/test_base_engine_read_loop.py` 新增：stop escalation 时 expected_name 传给 kill helper。

---

# P9：非进度但相关的架构/安全清理

## P9.1 `manifest_estimated_size()` SSRF 修复或降级

### 文件与函数

- 文件：`core/download/manager.py`
- 函数：`manifest_estimated_size`

### 当前风险

```python
request_kwargs = {"timeout": 5, "allow_redirects": True}
resp = _requests.head(url, **request_kwargs)
```

### 修改方案 A：补 SSRF guard

1. 调用 `utils.ssrf_guard.ensure_public(url)`。
2. 使用 `make_pinned_session()`。
3. `allow_redirects=False`。
4. 每个 redirect 目标重新 guard。

### 修改方案 B：明确降级为测试/内部函数

如果 UI 不再需要网络估算，直接在 docstring 标记：

```text
Not used by UI path. Do not call from main thread. Network callers must SSRF-guard externally.
```

更推荐方案 A。

---

## P9.2 真正使用 `TaskQueue` 或删除未使用抽象

### 文件与函数

- `core/download/task_queue.py`
- `core/download/manager.py::__init__`
- `core/download/manager.py::_worker`
- `core/download/manager.py::_is_task_queued`
- `core/download/manager.py::_remove_task_from_queue`
- `core/download/manager.py::_snapshot_queued_tasks`
- `core/download/manager.py::shutdown`

### 当前问题

`TaskQueue` 已存在，但 `DownloadManager` 仍使用 `queue.Queue` 并访问 `.mutex`、`.queue` 私有字段。

### 低风险计划

1. 先不和进度主修混在同一个 PR/提交中。
2. 待 P1-P8 稳定后迁移。
3. 迁移时增加兼容 wrapper，使原测试可平滑改写。

---

## P9.3 清理过时 ISS/调试注释

### 文件

- `engines/n_m3u8dl_re.py`
- `core/download/manager.py`
- `ui/download_queue.py`
- `ui/main_window.py`

### 清理规则

保留：

- 解释安全边界的设计注释。
- 解释状态机不变量的注释。

删除/迁移：

- “这正是进度长期显示 0 的根因”这类一次性排障注释。
- `RAW/CALLBACK/FALLBACK` 调试日志说明。
- 已完成迁移但仍写“未来 Stage 4 会迁移”的过时注释。

### 验收

代码注释描述当前设计，而不是历史补丁过程。

---

# P10：全量回归、真实样本验证与发布检查

## P10.1 自动化测试

### 必跑

```bash
python -m pytest tests/test_base_engine_read_loop.py tests/test_nm3u8dl_re_progress_parsing.py tests/test_download_queue_panel_logic.py tests/test_download_manager_state_machine.py -q
```

### 新增测试必跑

```bash
python -m pytest tests/test_download_progress_flow.py tests/test_download_progress_throttle.py -q
```

### 推荐扩展

```bash
python -m pytest tests/test_engine_argv_safety.py tests/test_headers.py tests/test_redact.py tests/test_ssrf_guard.py -q
```

---

## P10.2 手工样本验证

准备至少 5 类样本：

1. 普通 HLS `.m3u8`。
2. master + variant HLS。
3. 分片数较多的视频。
4. 需要 Referer/Cookie 的视频。
5. 直播流或无总进度任务。

记录：

| 样本 | 引擎 | 是否显示百分比 | 是否显示速度 | stdout 卡住时是否显示分片 | 是否完成 | 日志是否刷屏 |
| --- | --- | --- | --- | --- | --- | --- |
| 普通 HLS | N_m3u8DL-RE |  |  |  |  |  |
| master variant | N_m3u8DL-RE |  |  |  |  |  |
| 页面站 | yt-dlp |  |  | N/A |  |  |
| 直链 | Aria2 |  |  | N/A |  |  |
| 直播 | Streamlink | N/A |  | N/A |  |  |

---

## P10.3 日志验收

检查日志中：

- 不应出现 `[N_m3u8DL-RE RAW]` INFO 刷屏。
- 不应出现 `token=明文`、`sign=明文`、`signature=明文`。
- 进度异常应有可检索事件：
  - `download_progress_emit`
  - `download_progress_callback_failed`
  - `nm3u8dlre_temp_progress_callback_delivered`
  - `worker_exit_timeout`

---

# 3. 推荐提交拆分

建议不要一次性提交所有修改，按以下顺序拆分，便于回滚：

1. `test(progress): add download progress flow and throttle regression tests`
2. `fix(log): remove noisy N_m3u8DL progress raw info logs and redact urls`
3. `fix(progress): throttle DownloadManager snapshot emissions`
4. `fix(nm3u8dl): emit temp segment progress when stdout is stalled`
5. `fix(progress): stabilize retry and multi-source attempt progress states`
6. `refactor(snapshot): add display fields to TaskSnapshot and stop raw UI reads`
7. `fix(ui): render all queue refresh paths from snapshot first`
8. `fix(process): pass expected engine name to process tree termination`
9. `chore(cleanup): remove obsolete progress debug comments and align docs`

---

# 4. 风险控制与回滚策略

## 4.1 风险最高的步骤

- P2：节流逻辑如果过严，会导致进度更新过少。
- P5：TaskSnapshot 字段扩展会影响测试、smoke、序列化契约。
- P7：锁字段扩展可能暴露旧测试/旧调用点的直接写入问题。

## 4.2 回滚策略

1. P1 可独立回滚，不影响功能。
2. P2 的节流阈值应集中为常量，必要时可配置为 0 关闭。
3. P3 temp fallback 可用配置开关：

```json
"engines": {
  "n_m3u8dl_re": {
    "temp_progress_fallback": true,
    "temp_progress_unknown_total_enabled": true
  }
}
```

4. P5 TaskSnapshot 新字段应向后兼容：`to_dict()` 输出新增字段，旧 UI 不依赖则不破坏。

---

# 5. 最小优先修复包

如果希望先用最小改动解决“进度不显示”，建议只做：

1. P1.1：移除/降级 N_m3u8DL RAW INFO 日志。
2. P2：DownloadManager 进度 snapshot 节流。
3. P3.3：temp monitor total unknown 时也发 downloaded segments。
4. P4.1：retry 后重新进入 downloading 立即发 snapshot。
5. P6.1/P6.2：UI 重绘优先 snapshot。

这 5 项完成后，预计能显著改善：

- 下载中长期显示 0%；
- UI 滞后；
- 日志刷屏；
- 排序/语言刷新后进度回退；
- 重试状态卡住。

---

# 6. 最终完成定义

当满足以下条件时，本轮“下载进度显示链路修复”可以视为完成：

- [ ] 每个阶段/子步骤均已在对应测试通过后才进入下一阶段。
- [ ] 高频进度事件有节流，UI 不被 queued signal 淹没。
- [ ] N_m3u8DL-RE stdout 卡住时仍显示 downloaded segments 或估算百分比。
- [ ] retry / source fallback / engine fallback 状态显示一致。
- [ ] TaskSnapshot 包含 UI 展示所需字段，UI 不再依赖 raw task 展示进度。
- [ ] 排序、过滤、语言刷新不会覆盖 snapshot 进度。
- [ ] RAW 进度日志默认关闭，URL 日志脱敏。
- [ ] 进程 kill expected_name guard 全路径生效。
- [ ] 新旧测试全部通过。
- [ ] 真实 HLS 样本验证通过。
