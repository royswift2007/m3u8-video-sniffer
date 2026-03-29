# DEBUG S6 报告（UI 文本与编码治理）

## 本次完成项
- `utils/logger.py`
  - 新增 `Logger._ensure_utf8_console()`，在 Windows 控制台上尽力使用 UTF-8 输出，降低日志中文 `???/乱码` 概率。
  - 在 `Logger.__init__()` 初始化时调用该方法。

## 验证
- `python -m compileall utils\logger.py`
  - 结果：通过。

## 风险与说明
- 控制台编码由运行环境决定，`reconfigure` 失败时会自动回退，不影响程序主流程。
- 文件日志仍为 `utf-8`，与现有日志链路兼容。

## 结论
- S6 当前子目标（编码稳定性增强）已落地，可进入 S7 自动化测试阶段。
