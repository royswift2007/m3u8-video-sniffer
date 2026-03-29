# DEBUG S2 阶段报告（下载状态机一致性）- 进行中

## 1. 阶段进度
- 状态：进行中（核心逻辑已落地，自动脚本已通过，待实机 UI 回归）
- 日期：2026-03-14

## 2. 本轮已完成改动
文件：`core/download_manager.py`

1. 增加任务状态与去重辅助函数
- `_reset_task_runtime()`
- `_remove_task_from_state_lists()`
- `_is_task_queued()`
- `_snapshot_queued_tasks()`
- `_unique_tasks()`

2. `add_task()` 修复
- 避免任务执行中重复入队。
- 避免队列中重复对象重复入队。
- 入队前清理历史状态列表并重置运行态字段。

3. `_worker()` 修复
- `task_done()` 改为 `finally` 保障执行。
- 增加防御性回收，避免队列计数泄漏。

4. `_execute_download()` 修复
- 下载开始时先做状态列表清理，再加入 `active_tasks`。
- 成功/失败路径统一去重后再归档，避免重复记录。
- 修复 `hls_probe_hard_fail` 分支失败任务被误清空的回归。
- 下载结束后显式清理 `task.process`。

5. `resume_task()` / `remove_task()` 修复
- 恢复前先清理历史状态列表，避免任务在多个集合并存。
- 删除任务时统一使用去重清理函数。

6. `get_all_tasks()` / `get_stats()` 修复
- 引入按任务对象去重统计，避免 total/completed/failed 计数膨胀。

7. `shutdown()` 修复
- 从 `process.terminate()` 改为 `_kill_process_tree()`，提升退出清理强度。

8. `paused` 状态统计补齐
- 增加 `paused_tasks` 列表。
- `pause_task()`、`_execute_download()`、`get_all_tasks()`、`get_stats()` 统一纳入暂停态统计。
- 修复“暂停任务从统计与列表视图消失”的一致性问题。

9. 任务移除语义修复（避免“删后回流”）
- `remove_task()` 引入独立 `stop_reason=removed`。
- `_execute_download()` 新增 removed 终态分支，不再把已删除任务归档为 failed/completed。
- 修复并发场景下“UI 已删，后台又回到失败列表”的问题。

10. 队列移除能力补齐
- 新增 `_remove_task_from_queue()`，支持 waiting 任务立即出队。
- 修复删除/取消 waiting 任务后仍留在内部队列的问题。
- 修正队列 `unfinished_tasks` 计数，避免 `task_done() called too many times`。

## 3. 验证记录
1. 语法检查通过  
命令：`python -m compileall core\download_manager.py`

2. 下载状态机脚本验证通过（Dummy 引擎）
- 成功路径：重复入队拦截 + 完成计数正确。
- 恢复路径：`get_all_tasks()` 去重正确。
- 失败路径：失败计数最终为 1，不重复。

3. 回归确认
- `CatCatchServer` 端口回退脚本继续通过（`9527 -> 9528`）。

4. 暂停/取消统计验证通过
- 暂停的非运行态任务会进入 `paused` 统计。
- 取消的非运行态任务会进入 `failed` 统计。
- `get_all_tasks()` 结果去重正确。

5. 删除并发回流验证通过
- 活动任务删除后不再回流到 `failed/completed`。
- waiting 任务删除后 `queued` 立即归零。

## 4. 待完成项（进入下一轮）
1. UI 实机联调
- 下载中心对暂停/继续/删除/清除已完成的联动回归。
2. 真实引擎回归
- `N_m3u8DL-RE`、`yt-dlp`、`streamlink` 的恢复/失败路径核对。
3. 长任务场景验证
- 多任务并发下 `get_stats()` 与 UI 展示一致性。
