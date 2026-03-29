# S5 Report

## 阶段
- 阶段编号：S5（master/media 双链路保留与回退）
- 执行日期：2026-03-13

## 代码改动范围
1. `core/task_model.py`
- `DownloadTask` 新增字段：
  - `master_url`
  - `media_url`
- 同步清理历史损坏字符并重建为可编译版本（保持现有字段兼容）。

2. `ui/main_window.py`
- `_start_download` 增加参数：`master_url/media_url`。
- 入队时把 `master_url/media_url` 写入 `DownloadTask`。
- m3u8 分辨率选择流程中保留双链路：
  - `master_url=master m3u8`
  - `media_url=selected variant url`
- 历史记录写入时保存双链路字段。
- 历史“重新下载”时恢复双链路字段。

3. `ui/history_panel.py`
- `add_record` 新增参数：`master_url/media_url`。
- 持久化记录新增字段：
  - `master_url`
  - `media_url`

4. `engines/n_m3u8dl_re.py`
- 下载逻辑改为链路候选执行：
  - `primary -> master -> media`（去重后顺序）
- 新增方法：
  - `_build_url_candidates`
  - `_run_command`
- `_build_command` 新增参数：
  - `source_url`
  - `allow_select_video`
- 当回退到 media 链路时，禁用 `--select-video`，避免无效选择参数影响下载。

## 测试执行记录

### T1 语法/编译门禁
- 命令：
  - `python -m py_compile core/task_model.py ui/main_window.py ui/history_panel.py engines/n_m3u8dl_re.py core/download_manager.py`
  - `python -m compileall core ui engines`
- 结果：通过。

### T2 双链路回退顺序行为验证（离线脚本）
- 方法：构造 `FakeEngine` 覆盖 `_run_command`，模拟 master 失败、media 成功。
- 结果：
  - 调用顺序：`primary(master) -> media`
  - 最终 `ok=True`
  - 符合“master 失败自动回退 media”预期。

### T3 media 链路参数验证（离线脚本）
- 方法：调用 `_build_command` 比较 `allow_select_video=True/False`。
- 结果：
  - master 命令包含 `--select-video`
  - media 命令不包含 `--select-video`，且包含 `--auto-select`
  - 符合预期。

### T4 链路字段接线检查（静态）
- 命令：`rg -n "master_url|media_url|_build_url_candidates|nm3u8dlre_source_try" core/task_model.py ui/main_window.py ui/history_panel.py engines/n_m3u8dl_re.py`
- 结果：字段定义、UI 入队/历史回放、引擎回退链路均存在。

## 门禁结论
- 是否允许进入下一阶段（S6）：是。
- 结论依据：T1/T2/T3/T4 全部通过。
