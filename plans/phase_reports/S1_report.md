# S1 Report

## 阶段
- 阶段编号：S1（同 URL 资源上下文合并）
- 执行日期：2026-03-13

## 代码改动范围
1. `core/m3u8_sniffer.py`
- 重写为 UTF-8 可维护版本。
- 去重策略从“同 URL 直接丢弃”改为“同 URL 合并上下文”。
- 新增 `_find_resource_by_url`、`_merge_resource_context`。

## 测试执行记录
### T1 编译检查
- 命令：`python -m compileall core/m3u8_sniffer.py`
- 结果：通过。

### T2 合并行为验证
- 脚本：同 URL 连续 add，两次 headers 分别提供 `referer` 与 `cookie/origin`。
- 结果：
1. 返回对象为同一对象（`True`）
2. 资源总数保持 1
3. headers 成功合并（cookie/origin 生效，referer 更新）

### T3 结构检查
- 命令：`rg -n 'def _merge_resource_context|def _find_resource_by_url' core/m3u8_sniffer.py`
- 结果：新增函数存在。

## 门禁结论
- 是否允许进入下一阶段（S2）：是。
- 结论依据：T1/T2/T3 全部通过。
