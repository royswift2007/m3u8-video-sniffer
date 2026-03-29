# DEBUG S1 阶段报告（配置/协议/服务正确性）

## 1. 阶段结论
- 状态：通过
- 日期：2026-03-14
- 结论：S1 目标已完成，配置加载、协议投递、CatCatch 服务状态机均已修复并通过脚本验证。

## 2. 本阶段改动
1. 配置管理修复  
   文件：`utils/config_manager.py`  
   变更：
   - 修复 `load()` 成功读取后被默认配置覆盖的问题。
   - 增加默认配置深度合并逻辑：`_build_default_config()`、`_merge_with_defaults()`。
   - 增加目录保障：`_ensure_directories()`。
   - `save()` 只在写盘副本中附加 `_说明`。

2. CatCatch HTTP 服务状态机修复  
   文件：`core/catcatch_server.py`  
   变更：
   - `start()` 等待真实 bind 结果。
   - `_run_server()` 支持 `9527-9539` 回退并准确更新 `self.port`。
   - `stop()` 补齐 `shutdown + server_close + join`。
   - `log_message()` 输出完整格式化 HTTP 日志。
   - `do_POST()` 统一 JSON/form 解析并增加错误返回。

3. 协议处理端口探测修复  
   文件：`protocol_handler.pyw`  
   变更：
   - `send_to_app()` 改为扫描 `9527-9539`。
   - 新增 `_send_to_single_port()`。
   - `launch_app_with_url()` 启动后等待并重试回投。
   - 精确异常：`json.JSONDecodeError`。

4. 入口参数与环境变量细化  
   文件：`main.py`  
   变更：
   - `QTWEBENGINE_CHROMIUM_FLAGS` 改为合并，不覆盖已有值。
   - CLI headers 解析改用 `json.JSONDecodeError`。

## 3. 验证记录
1. 语法检查通过  
   命令：
   - `python -m compileall utils\config_manager.py core\catcatch_server.py protocol_handler.pyw main.py`
   - `python -m py_compile protocol_handler.pyw`
   - `python -m py_compile core\catcatch_server.py utils\config_manager.py main.py`

2. 配置合并验证通过  
   内容：
   - 自定义配置保留
   - 缺省字段自动补齐
   - 自定义扩展键不丢失

3. 协议端口扫描验证通过  
   内容：
   - 在 `9531` 启动测试 HTTP 服务
   - `send_to_app()` 成功投递并收到请求

4. CatCatch 端口回退验证通过  
   内容：
   - 使用原始 socket 占用 `9527`
   - `CatCatchServer` 成功回退到 `9528`
   - `stop()` 后服务退出正常

## 4. 风险与说明
- Windows 上不同占用方式（`HTTPServer`/socket 复用）行为不一致，端口冲突测试必须使用“不可复用原始 socket”方式验证。

## 5. 下一阶段
- 进入 S2：下载任务状态机一致性修复。
