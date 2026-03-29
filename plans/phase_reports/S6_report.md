# S6 Report

## 阶段
- 阶段编号：S6（候选链接打分与优选）
- 执行日期：2026-03-13

## 代码改动范围
1. `core/task_model.py`
- `M3U8Resource` 新增 `candidate_score` 字段。
- `DownloadTask` 新增 `candidate_scores` 字段。
- 清理历史损坏字符并重建为可编译版本（字段保持兼容）。

2. `core/m3u8_sniffer.py`
- 新增 `_score_m3u8_candidate` 打分逻辑（URL 特征 + 鉴权上下文）。
- `add_resource` 对 m3u8 资源计算并写入 `candidate_score`。
- 同 URL 合并时，如新分数更高则更新 `candidate_score`。

3. `core/download_manager.py`
- 新增 `_score_m3u8_candidate`、`_rank_task_candidates`。
- 下载开始前对 `task.url/master_url/media_url` 进行打分排序，优选最高分链接作为首选下载 URL。
- 新增结构化日志事件：`download_candidate_rank`。

4. `utils/config_manager.py`
- 新增默认特性开关：
  - `download_candidate_ranking_enabled: true`

## 测试执行记录

### T1 编译门禁
- 命令：`python -m py_compile core/task_model.py core/m3u8_sniffer.py core/download_manager.py utils/config_manager.py ui/main_window.py ui/history_panel.py engines/n_m3u8dl_re.py`
- 结果：通过。

### T2 下载前候选优选行为（离线脚本）
- 构造：`task.url=master.m3u8`，`media_url=/hls/media_1080.m3u8`，带 `referer+cookie`。
- 结果：
  - 产生日志 `event=download_candidate_rank`
  - 优选结果为 media 链接
  - `candidate_scores` 写入任务对象
- 结论：通过。

### T3 Sniffer 打分与同 URL 合并（离线脚本）
- 构造：同 URL 两次上报，第二次带 cookie。
- 结果：
  - `candidate_score` 从 86 提升到 111
  - 对象保持同一实例（去重合并生效）
  - headers 中 cookie 成功合并
- 结论：通过。

### T4 全量编译回归
- 命令：`python -m compileall core ui engines`
- 结果：通过。

## 门禁结论
- 是否允许进入下一阶段（S7）：是。
- 结论依据：T1/T2/T3/T4 全部通过。
