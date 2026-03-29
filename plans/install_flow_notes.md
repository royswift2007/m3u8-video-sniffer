# 安装链路当前接入说明

本轮在安装链路范围内补齐了批处理桥接与安装器骨架，当前职责划分如下。

## 1. 依赖下载入口

[`scripts/download_dependencies.py`](scripts/download_dependencies.py) 是当前统一的依赖下载 CLI：

- 默认只下载 `required`
- 传入 `--include-recommended` 时同时下载 `required` 和 `recommended`
- 返回码保持为：
  - `0`：全部成功或已存在
  - `1`：执行完成但有依赖失败
  - `3`：入口运行时异常

[`scripts/download_tools.bat`](scripts/download_tools.bat) 现在只负责在 Windows `cmd.exe` 下桥接到上述 CLI：

- 从脚本位置推导项目根目录
- 优先使用 `M3U8D_PYTHON`
- 其次尝试仓库内 `.venv\Scripts\python.exe`
- 再回退到 `py -3` 或 `python`
- 透传 CLI 参数与退出码

当前调用方式：

```bat
scripts\download_tools.bat
scripts\download_tools.bat --include-recommended
```

## 2. 协议注册入口

[`scripts/register_protocol.bat`](scripts/register_protocol.bat) 已改为面向打包后的 `protocol_handler.exe`：

- 优先尝试安装目录下的 `protocol_handler\protocol_handler.exe`
- 兼容回退到安装目录根下的 `protocol_handler.exe`
- 最后兼容回退到 `scripts` 目录下的 `protocol_handler.exe`
- 注册到 `HKCU\Software\Classes\m3u8dl`
- 命令行为 `"protocol_handler.exe" "%1"`
- 通过 `reg add` 直接写入，避免 `.reg` 文件转义复杂度

这意味着安装后的推荐布局是：

```text
{app}\protocol_handler\protocol_handler.exe
{app}\scripts\register_protocol.bat
```

## 3. 安装器骨架

已新增 [`installer/M3U8D.iss`](installer/M3U8D.iss) 作为 Inno Setup 骨架。

当前已覆盖：

- `[Setup]` 基本安装信息
- `[Dirs]` 预建 `bin`、`logs`、`cookies`、`Temp`
- `[Files]` 复制：
  - `M3U8D.exe`
  - `protocol_handler\protocol_handler.exe`
  - `protocol_handler\_internal\`
  - `resources/`
  - 安装后需要保留的 `scripts/`
  - `deps.json`
  - `config.json`
- `[Icons]` 开始菜单快捷方式与卸载入口
- `[Run]` 预留并接入：
  - [`scripts/download_tools.bat`](scripts/download_tools.bat)
  - [`scripts/register_protocol.bat`](scripts/register_protocol.bat)
- `[Code]` 提供依赖确认与协议注册确认的基础骨架

## 4. 当前缺口

当前仍是“可继续接线”的骨架，未完成项主要包括：

- 尚未固化真实的 PyInstaller 输出目录与构建流水线
- 依赖确认仍是交互骨架，未接入基于文件存在性或清单的精确检测
- 安装器尚未对下载失败结果做更细粒度提示与回滚策略
- 尚未补齐静默安装、升级安装、卸载期协议清理等细节
- 尚未验证 Inno Setup 对当前 `[Run]` 参数转义在真实安装环境中的最终行为

## 5. 后续优先接入点

建议后续按以下顺序补齐：

1. 真实编译并验证 [`installer/M3U8D.iss`](installer/M3U8D.iss)
2. 增加安装期依赖缺失检测 helper 或更稳妥的 Inno 调用桥接
3. 为安装/卸载补齐协议注册与清理闭环
4. 更新正式安装发布文档，使之与实际安装产物一致
