# DEBUG S3 阶段报告（关闭流程与进程清理）- 进行中

## 1. 阶段进度
- 状态：进行中（核心关闭链路已加强，待实机引擎场景回归）
- 日期：2026-03-14

## 2. 本轮已完成改动
文件：`core/download_manager.py`

1. `shutdown()` 收口增强
- 关闭时先标记 `stop_flag` 并设置 active 任务 `stop_reason=shutdown`。
- active 任务统一走 `_kill_process_tree()`，替代弱化的 `terminate()`。
- 增加 waiting 队列主动清空，避免关闭后残留待执行任务。

2. 队列计数修复
- 清空 waiting 队列时按数量递减 `unfinished_tasks`，避免 `task_done() called too many times`。
- 在计数归零时正确通知 `all_tasks_done`。

3. worker 收尾兼容
- worker 线程退出后 `_workers` 清空，避免重复 join 与旧句柄残留。

## 3. 验证记录
1. 语法检查通过  
命令：`python -m compileall core\download_manager.py`

2. 关闭链路脚本验证通过
- 构造“1 active + 2 waiting”场景后调用 `shutdown()`。
- 结果：
  - `queued=0`
  - `active=0`
  - 无 `task_done` 计数异常

3. 删除并发场景验证通过
- active 任务删除后不会回流到 `failed/completed`。

## 4. 待完成项（下一轮）
1. 真实引擎回归
- `N_m3u8DL-RE`、`yt-dlp`、`streamlink` 下载中直接关闭程序。
2. 临时文件清理策略验证
- 关闭/取消/删除三种场景的临时文件行为核对。
