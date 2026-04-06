# 强壮性排查修复计划

日期：2026-04-02

## 审查范围

- 入口与协议链路：`main.py`、`mvs.pyw`、`protocol_handler.pyw`
- 核心执行链路：`core/download_manager.py`、`core/engine_selector.py`、`core/task_model.py`
- 网络与解析：`core/m3u8_parser.py`、`core/services/hls_probe.py`、`core/m3u8_sniffer.py`、`core/catcatch_server.py`
- 浏览器与自动化：`core/playwright_driver.py`
- UI 状态面板：`ui/main_window.py`、`ui/resource_panel.py`、`ui/download_queue.py`、`ui/history_panel.py`
- 配置与日志：`utils/config_manager.py`、`utils/logger.py`、`utils/log_retention.py`
- 引擎层：`engines/n_m3u8dl_re.py`、`engines/ytdlp_engine.py`、`engines/streamlink_engine.py`、`engines/aria2_engine.py`

## 当前验证现状

- 已运行：`python -m pytest -q`
- 结果：`48 passed, 2 failed, 3 errors`
- 直接失败项：
  - `tests/test_download_manager_state_machine.py:295-327`
  - `tests/test_engine_selector.py:49-52`
- 额外错误项：
  - `tests/test_log_retention.py` 的 3 个用例在 `tmp_path` 建立阶段触发本机临时目录权限错误，属于当前运行环境阻塞，暂不能直接当作业务代码缺陷结论

## 问题清单

### P0. 下载并发上限控制不是原子操作，可能实际跑超 `max_concurrent`

- 位置：`core/download_manager.py:487-495`、`core/download_manager.py:540-543`
- 现象：工作线程先读 `active_tasks` 长度，再去 `Queue.get()`，最后在另一个代码段把任务放入 `active_tasks`。多个 worker 可同时看到相同的活跃数并各自取走任务。
- 风险：
  - 实际下载数超过配置值
  - 高负载时进程/带宽/磁盘争抢失控
  - “暂停/恢复/统计”状态更容易出现竞争条件
- 修复方向：
  - 用 `BoundedSemaphore` 或“占位 slot”把“获取执行资格 + 出队”做成单一原子流程
  - 把 `active_tasks` 变成严格受锁保护的状态机
  - 增加 `max_concurrent=1/2` 的并发竞态测试

### P0. 配置与历史记录都采用原地覆写，掉电/崩溃时会直接损坏 JSON，并触发数据回退/丢失

- 位置：
  - `utils/config_manager.py:49-83`
  - `utils/config_manager.py:66-73`
  - `ui/history_panel.py:191-215`
  - `ui/history_panel.py:264-303`
  - `ui/history_panel.py:347-350`
- 现象：
  - `save()`/`add_record()`/`_delete_record()` 都直接 `open(..., "w")`
  - 配置加载失败时直接回退默认值并重新保存
  - 历史文件损坏时会移动/删除原文件
- 风险：
  - 部分写入后文件损坏
  - 一次异常即可抹掉用户配置或历史
  - 无法区分“临时写坏”与“真实配置缺失”
- 修复方向：
  - 引入“写临时文件 -> `flush/fsync` -> 原子替换”的统一写盘工具
  - 失败时保留坏文件和上一个可恢复版本，不立即覆盖
  - 为配置与历史增加 `.bak` 或版本化快照
  - 补充崩溃恢复测试与损坏文件回放测试

### P0. Playwright 启动前强删 Chromium lock 文件，存在浏览器配置目录损坏风险

- 位置：`core/playwright_driver.py:94-110`
- 现象：启动持久化 profile 前直接删除 `SingletonLock`、`SingletonCookie`、`SingletonSocket`
- 风险：
  - 若 profile 正被其他浏览器实例占用，可能造成数据库/会话损坏
  - 登录态、Cookie、扩展状态出现随机损坏
  - 偶发“无法启动”“启动后立即崩”的问题会非常难排查
- 修复方向：
  - 先检测持有锁的进程是否存活，再决定是否清理
  - 为应用使用独立 profile，并支持锁冲突时回退到新 profile
  - 把“清理残留锁”改为显式恢复流程，而不是每次启动默认执行

### P0. 删除任务时按文件名子串清理临时文件，可能误删其他任务或用户文件

- 位置：`ui/download_queue.py:412-418`、`ui/download_queue.py:432-448`
- 现象：
  - 只要 `task.filename in item.name` 就删除
  - 未完成任务时，`name == task.filename` 的主文件也会被当作临时文件删掉
- 风险：
  - `movie` 可能误删 `movie-trailer`、`movie_backup`
  - 取消/失败任务时误删同目录内不相关文件
  - 问题一旦出现就是不可逆的数据损失
- 修复方向：
  - 为每个任务记录专属临时目录/前缀，而不是靠子串匹配
  - 对下载主文件与临时文件使用明确命名协议
  - 删除前做严格路径和文件归属校验

### P1. 协议处理器在投递失败时仍返回成功，外部拉起链路可能“看起来成功、实际丢单”

- 位置：`protocol_handler.pyw:243-281`
- 现象：`launch_app_with_url()` 在本地 API 重试超时后仍然 `return True`
- 风险：
  - 浏览器/扩展侧认为唤起成功，但请求未入队
  - 用户重复点击后形成重复任务或误判
  - 后续很难区分“应用启动失败”还是“启动成功但投递失败”
- 修复方向：
  - 明确区分“启动成功”和“投递成功”
  - 超时后返回失败码并把原因写入日志/UI
  - 引入握手文件、命名管道或本地 socket ack，避免盲等

### P1. M3U8 异步解析回调持有的是旧行号，资源列表变化后会更新到错误行

- 位置：`ui/resource_panel.py:542-605`、`ui/resource_panel.py:722-729`
- 现象：后台线程启动时捕获 `row`，回调完成时直接按该行更新表格并插入变体
- 风险：
  - 批量删除/过滤/重建后，清晰度信息写到错误资源上
  - 变体可能挂到错误父资源
  - UI 显示与真实下载对象脱节
- 修复方向：
  - 用资源对象主键或 `id(resource)` 建立稳定映射，不依赖瞬时行号
  - 回调前重新确认该资源仍存在且所在行未变化
  - 对“删除资源后线程返回”的场景补测试

### P1. 解析与预探测默认关闭 TLS 校验，会把证书问题静默吞掉

- 位置：
  - `core/m3u8_parser.py:95-131`
  - `core/services/hls_probe.py:30-84`
- 现象：`requests.get(..., verify=False)` 被作为默认行为，而不是受控降级
- 风险：
  - 中间人攻击、错误证书、站点劫持被静默接受
  - 真实证书问题被伪装成“解析成功但内容异常”
  - 与 `yt-dlp` 的“仅在证书失败时有限回退”策略不一致
- 修复方向：
  - 默认启用证书校验
  - 仅在识别到明确证书错误时提供一次可观测的降级重试
  - 将“不安全重试”挂到可配置特性开关并记录结构化日志

### P1. 站点规则按字符串子串匹配，并且会强制覆盖请求头，容易串站或串鉴权

- 位置：
  - `core/download_manager.py:255-275`
  - `core/m3u8_sniffer.py:227-247`
- 现象：
  - 域名判断使用 `d in url_lower`
  - `headers` 中的扩展头直接覆盖任务头
- 风险：
  - `abc.com` 会匹配到 `evilabc.com`
  - 同域不同业务线/租户的请求头可能被旧规则污染
  - 调试时很难看出“真实请求头”是否被规则改写
- 修复方向：
  - 按解析后的 hostname 做精确匹配或后缀匹配
  - 站点规则默认只补缺，不覆盖显式抓到的头
  - 对 Cookie/Authorization/Origin 使用单独白名单策略

### P1. 启动入口存在双轨分叉，`main.py` 与 `mvs.pyw` 已经漂移

- 位置：
  - `protocol_handler.pyw:226-229`
  - `mvs.pyw:37-40`
  - `mvs.pyw:103-107`
  - `main.py:28-39`
- 现象：
  - 协议处理器在源码模式优先拉起 `mvs.pyw`
  - `main.py` 已经改成“合并 Chromium flags + 更细的异常处理”
  - `mvs.pyw` 仍然直接覆盖环境变量，并保留裸 `except`
- 风险：
  - 源码运行与打包运行行为不一致
  - 一处修复后，另一入口继续保留旧 bug
  - 协议拉起、手动启动、测试启动会出现不可预期差异
- 修复方向：
  - 统一保留一个真正入口，另一个只做薄包装转发
  - 把启动前置逻辑下沉到共享函数
  - 将协议处理器固定指向同一入口

### P2. 自动化测试守护线已部分失效，当前主分支不是稳定绿灯

- 位置：
  - `tests/test_engine_selector.py:49-52`
  - `tests/test_download_manager_state_machine.py:295-327`
- 现象：
  - 一个用例断言清晰错误消息，但运行时文案已漂移
  - 一个用例断言 fallback 日志文案，但当前实现已更换措辞
- 风险：
  - 回归保护失真，后续改动可信度下降
  - 团队会对“测试红”逐步免疫
- 修复方向：
  - 先决定这些行为是“代码应回退”还是“测试应更新”
  - 对日志类断言改成更稳的结构化字段或事件名断言
  - 把消息文案和行为契约分开测试

## 分阶段修复计划

### 第一阶段：先止血，优先消除数据丢失和错执行

- 修复 `DownloadManager` 的并发配额竞态
- 为配置与历史记录引入统一原子写盘工具
- 修复协议处理器“投递失败仍报成功”
- 把任务清理从“子串匹配删文件”改成“任务专属临时路径删除”
- 补最小回归测试：
  - 并发上限不超发
  - 配置/历史损坏可恢复
  - 协议唤起失败可被上层感知
  - 删除任务不会误删邻近命名文件

### 第二阶段：提升网络与浏览器侧健壮性

- 重构 Playwright profile 锁处理逻辑
- 调整 M3U8 解析与 HLS probe 的 TLS 策略
- 收紧 site rules 的 host 匹配和头覆盖策略
- 给网络异常、证书异常、鉴权异常补结构化日志字段

### 第三阶段：统一入口与 UI 异步状态模型

- 合并 `main.py` / `mvs.pyw` 启动逻辑
- 让 `ResourcePanel` 改用稳定资源 ID 驱动异步回调
- 梳理运行时路径策略，统一 history/logs/temp/config 的数据根
- 补 UI 删除/过滤/异步回填的集成测试

### 第四阶段：恢复质量门禁

- 修正当前 2 个真实测试失败
- 将日志断言尽量改成事件字段断言
- 处理当前环境下 `tmp_path` 的权限阻塞，确保测试可在 CI 与本机稳定运行

## 建议执行顺序

1. `core/download_manager.py`
2. `utils/config_manager.py` + `ui/history_panel.py`
3. `protocol_handler.pyw`
4. `ui/download_queue.py`
5. `core/playwright_driver.py`
6. `core/m3u8_parser.py` + `core/services/hls_probe.py`
7. `core/m3u8_sniffer.py` + `core/download_manager.py` 的 site rules 分支
8. `mvs.pyw` + `main.py` + `protocol_handler.pyw`
9. `ui/resource_panel.py`
10. `tests/*`

## 验收标准

- 任意时刻运行中的下载数不超过 `max_concurrent_downloads`
- 强杀进程/异常退出后，`config.json` 与 `history.json` 不会变成不可恢复空文件
- 删除一个任务不会删除其他任务或用户已有文件
- 协议唤起在“启动成功但投递失败”时能向外部明确返回失败
- Playwright profile 在已有实例占用时不会通过强删锁文件抢占
- M3U8/HLS 预探测默认启用 TLS 校验，只有受控场景才允许降级
- 资源列表在异步解析返回后不会串行、串资源、串清晰度
- `pytest` 主测试集恢复为稳定绿灯

