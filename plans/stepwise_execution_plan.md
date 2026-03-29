# 分步执行方案（细化）

以下为将整体方案细化为可逐步执行的行动清单，按阶段和步骤拆解，方便逐项落地。

## 阶段0：准备与基线

### 0.1 建立测试样本集
- 收集 6~10 个代表性 URL（包含：普通HLS、master playlist、多层嵌套、带Referer、加密、直链MP4、DASH）
- 记录每个样本的预期行为（能否抓取、能否下载、下载引擎）

**涉及文件/位置**
- 记录到 [`/plans/stepwise_execution_plan.md`](plans/stepwise_execution_plan.md)

### 0.2 开启详细日志级别
- 确认日志开关或添加临时调试日志点
- 为后续对比提供基线

**涉及文件**
- [`utils/logger.py`](utils/logger.py)
- [`core/m3u8_parser.py`](core/m3u8_parser.py)
- [`core/request_interceptor.py`](core/request_interceptor.py)

---

## 阶段1：P0 修复（核心稳定性）

### 阶段1冒烟测试（必须通过后进入阶段2）
- 覆盖样本：普通HLS、master playlist、多层嵌套
- 验证点：能抓到m3u8、能列出变体、下载启动成功、速度非0

### 1.1 修复 M3U8 解析（master/media 区分）
- 步骤
  1. 检测内容是否包含 `#EXT-X-STREAM-INF`
  2. 无该标签则判定为 media playlist，返回空变体并记录“media playlist”标记
  3. 增加“多层嵌套”支持：若变体 URL 仍是 m3u8，则再次解析

**涉及文件**
- [`core/m3u8_parser.py`](core/m3u8_parser.py)

**验收**
- media playlist 不再误报“无效”
- master playlist 能正确列出多码率

### 1.2 修复速度显示为 0
- 步骤
  1. 兼容 N_m3u8DL-RE 速度字段在“百分比前/后”的格式
  2. 过滤 0Bps 但不覆盖已有速度

**涉及文件**
- [`engines/n_m3u8dl_re.py`](engines/n_m3u8dl_re.py)

**验收**
- UI 中速度不再长期显示 0

### 1.3 增强下载失败重试（多次+换引擎）
- 步骤
  1. 增加“最大重试次数”配置项
  2. 失败后自动切换引擎（N_m3u8DL-RE → yt-dlp → ffmpeg）
  3. 每次重试记录失败原因

**涉及文件**
- [`core/download_manager.py`](core/download_manager.py)
- [`core/engine_selector.py`](core/engine_selector.py)
- [`config.json`](config.json)

**验收**
- 失败任务会自动重试并切换引擎

---

## 阶段2：抓取增强（P1）

### 阶段2冒烟测试（必须通过后进入阶段3）
- 覆盖样本：带Referer站点、规则命中站点、直链视频
- 验证点：规则补全生效、抓取成功率提升、无明显误报

### 2.1 站点特征库与规则引擎
- 步骤
  1. 新增站点规则配置结构（域名、URL关键字、Referer、User-Agent）
  2. 在拦截时按域名命中规则并补全请求头

**涉及文件**
- [`core/m3u8_sniffer.py`](core/m3u8_sniffer.py)
- [`core/request_interceptor.py`](core/request_interceptor.py)
- [`config.json`](config.json)

### 2.2 URL 捕获规则扩展
- 步骤
  1. 增加更多视频后缀识别（.m4s、.m3u8?token=、.ts?）
  2. 增加 JSON/XML 响应体中 m3u8 解析（可选）

**涉及文件**
- [`core/request_interceptor.py`](core/request_interceptor.py)

**验收**
- 新增站点样本可捕获 m3u8

---

## 阶段3：下载增强（P1/P2）

### 阶段3冒烟测试（必须通过后进入阶段4）
- 覆盖样本：大文件HLS、小文件直链、DASH
- 验证点：多次重试有效、断点续传生效、错误日志可读

### 3.1 自适应线程数
- 步骤
  1. 根据 m3u8 分段数/文件大小自动调整线程数
  2. 可配置最大/最小线程

**涉及文件**
- [`engines/n_m3u8dl_re.py`](engines/n_m3u8dl_re.py)
- [`config.json`](config.json)

### 3.2 断点续传优化
- 步骤
  1. 检测临时目录已有分片
  2. 续传前验证分片完整性

**涉及文件**
- [`core/download_manager.py`](core/download_manager.py)
- [`engines/n_m3u8dl_re.py`](engines/n_m3u8dl_re.py)

### 3.3 错误诊断增强
- 步骤
  1. 下载失败时记录引擎完整输出
  2. UI 上显示可读错误摘要

**涉及文件**
- [`utils/logger.py`](utils/logger.py)
- [`ui/log_panel.py`](ui/log_panel.py)

---

## 阶段4：体验优化（P2/P3）

### 阶段4冒烟测试（必须通过后收尾）
- 覆盖样本：历史任务重试、批量导入、队列拖拽
- 验证点：历史记录可追溯、批量流程稳定、队列顺序正确

### 4.1 下载历史与批量
- 步骤
  1. 记录完成/失败任务到历史文件
  2. 支持历史一键重试

**涉及文件**
- [`ui/history_panel.py`](ui/history_panel.py)
- [`core/download_manager.py`](core/download_manager.py)

### 4.2 队列排序与批量导入
- 步骤
  1. 支持队列拖拽排序
  2. 批量导入 URL 列表

**涉及文件**
- [`ui/download_queue.py`](ui/download_queue.py)

---

## 执行清单（可直接转为任务）

- [ ] 完成样本集与基线日志
- [ ] 修复 M3U8 解析逻辑
- [ ] 修复速度显示
- [ ] 增强下载重试
- [ ] 增加站点规则库
- [ ] 扩展URL捕获
- [ ] 自适应线程数
- [ ] 断点续传优化
- [ ] 错误诊断增强
- [ ] 下载历史/批量功能
- [ ] 队列排序与批量导入
