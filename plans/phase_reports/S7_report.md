# S7 Report

## 阶段
- 阶段编号：S7（站点规则自动学习，可回滚）
- 执行日期：2026-03-13

## 代码改动范围
1. `core/download_manager.py`
- 新增 `_learn_site_rule_from_task`：
  - 从成功任务提取 `host/referer/user-agent/origin`（可选 `cookie`）沉淀到 `site_rules`。
  - 支持开关与上限控制：`site_rules_auto.enabled/max_rules/allow_cookie`。
  - 已有规则按 `name=auto:{host}` 更新，不重复新增。
  - 新增结构化事件：
    - `site_rule_auto_learned`
    - `site_rule_auto_skipped`
- 在任务成功分支中接入自动学习。

2. `utils/config_manager.py`
- 默认配置新增：
  - `site_rules_auto.enabled`（默认 `False`）
  - `site_rules_auto.max_rules`（默认 `50`）
  - `site_rules_auto.allow_cookie`（默认 `False`）

## 测试执行记录

### T1 语法/编译门禁
- 命令：`python -m py_compile core/download_manager.py utils/config_manager.py core/m3u8_sniffer.py engines/n_m3u8dl_re.py core/task_model.py ui/main_window.py ui/history_panel.py`
- 结果：通过。

### T2 自动学习新增与去重（离线脚本）
- 配置：
  - `site_rules_auto.enabled=True`
  - `allow_cookie=False`
- 对同一任务调用 `_learn_site_rule_from_task` 两次。
- 结果：
  - `site_rules` 最终仅 1 条规则
  - 规则名：`auto:video.example.com`
  - `referer/user_agent/origin` 正确写入
  - 未写入 cookie
- 结论：通过。

### T3 cookie 可选持久化（离线脚本）
- 配置：`allow_cookie=True`
- 结果：新规则 `headers` 中包含 `cookie`。
- 结论：通过。

## 门禁结论
- 是否允许进入下一阶段（S8）：是。
- 结论依据：T1/T2/T3 全部通过。
