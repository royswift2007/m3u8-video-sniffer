# 打包与安装器联调说明（第五批）

本轮目标是把当前方案推进到真实打包联调阶段，但不实际执行 [`build_pyinstaller.py`](build_pyinstaller.py) 或 [`installer/M3U8D.iss`](installer/M3U8D.iss)。

## 1. 打包入口

当前新增了两个打包入口：

- [`build_pyinstaller.py`](build_pyinstaller.py)：主构建入口，负责调用 PyInstaller 并整理最终分发目录。
- [`build_pyinstaller.bat`](build_pyinstaller.bat)：Windows `cmd.exe` 下的桥接入口，优先复用仓库内 Python 或 `M3U8D_PYTHON`。

### 推荐执行方式

```bat
build_pyinstaller.bat
```

或直接：

```bat
python build_pyinstaller.py
```

### 该构建入口会做什么

当前正式图标路径统一为 [`resources/icons/mvs.ico`](resources/icons/mvs.ico)；构建脚本以该路径为唯一首选，并仅保留旧路径兼容回退。

1. 使用 PyInstaller 以 `--onedir` 模式打包主程序 [`mvs.pyw`](mvs.pyw) 为 `M3U8D.exe`
2. 使用 PyInstaller 以 `--onedir` 模式打包协议处理器 [`protocol_handler.pyw`](protocol_handler.pyw) 为 `protocol_handler.exe`
3. 将协议处理器独立 one-dir 输出搬运到主分发目录下的 `protocol_handler/`
4. 复制运行时资源与安装器所需辅助文件到最终分发目录，包括：
   - [`resources/`](resources)
   - [`core/`](core)
   - [`utils/`](utils)
   - [`config.json`](config.json)
   - [`deps.json`](deps.json)
   - [`scripts/download_tools.bat`](scripts/download_tools.bat)
   - [`scripts/download_dependencies.py`](scripts/download_dependencies.py)
   - [`scripts/register_protocol.bat`](scripts/register_protocol.bat)
   - [`scripts/uninstall_protocol.bat`](scripts/uninstall_protocol.bat)
5. 预创建空目录：
   - `bin/`
   - `cookies/`（仅创建空运行目录，不复制源码中的任何私有 cookies 文件）
   - `logs/`
   - `Temp/`

## 2. 预期输出目录结构

运行 [`build_pyinstaller.py`](build_pyinstaller.py) 后，预期主要产物布局如下：

```text
dist/
└─ M3U8D/
   ├─ M3U8D.exe
   ├─ _internal/
   ├─ config.json
   ├─ deps.json
   ├─ core/
   ├─ resources/
   ├─ scripts/
   │  ├─ download_tools.bat
   │  ├─ download_dependencies.py
   │  ├─ register_protocol.bat
   │  └─ uninstall_protocol.bat
   ├─ utils/
   ├─ protocol_handler/
   │  ├─ protocol_handler.exe
   │  └─ _internal/
   ├─ bin/
   ├─ cookies/              (empty only; no private cookies staged)
   ├─ logs/
   └─ Temp/
```

### 当前目录约定说明

- 主程序固定为：`dist/M3U8D/M3U8D.exe`
- 主程序 PyInstaller 运行时目录固定为：`dist/M3U8D/_internal/`
- 协议处理器固定为：`dist/M3U8D/protocol_handler/protocol_handler.exe`
- 协议处理器运行时目录固定为：`dist/M3U8D/protocol_handler/_internal/`

这意味着协议处理器不再与主程序共享同级可执行文件路径，而是进入单独子目录，便于后续安装器与协议注册脚本统一引用。

## 3. 安装器如何取用这些产物

[`installer/M3U8D.iss`](installer/M3U8D.iss) 已按上述真实输出结构调整，直接消费 `dist/M3U8D`：

- 主程序来源：`dist/M3U8D/M3U8D.exe`
- 主程序运行时来源：`dist/M3U8D/_internal/*`
- 协议处理器来源：`dist/M3U8D/protocol_handler/protocol_handler.exe`
- 协议处理器运行时来源：`dist/M3U8D/protocol_handler/_internal/*`
- 资源与脚本来源：`dist/M3U8D/resources/*`、`dist/M3U8D/scripts/*`
- 依赖下载 CLI 支撑模块来源：`dist/M3U8D/core/*`、`dist/M3U8D/utils/*`
- 清单与配置来源：`dist/M3U8D/deps.json`、`dist/M3U8D/config.json`
- `cookies/` 仅作为安装后的可写运行目录保留，不会从源码目录打包任何现有 cookie 文本文件

因此当前联调顺序是：

```text
先打包 -> 再产出 dist/M3U8D -> 再编译 installer/M3U8D.iss
```

## 4. 协议处理器分发与注册路径约定

当前统一约定如下：

```text
安装目录根\protocol_handler\protocol_handler.exe
```

[`scripts/register_protocol.bat`](scripts/register_protocol.bat) 已按此优先路径实现：

1. 优先查找：`{安装根}\protocol_handler\protocol_handler.exe`
2. 兼容回退：`{安装根}\protocol_handler.exe`
3. 再兼容旧路径：`{安装根}\scripts\protocol_handler.exe`

协议注册命令最终写入：

```text
"{安装根}\protocol_handler\protocol_handler.exe" "%1"
```

这样可以保证：

- 打包输出目录中的协议处理器位置明确
- 安装器复制后的目标位置明确
- 协议注册批处理与安装器骨架引用策略一致

## 5. 与源码运行方式的兼容说明

[`protocol_handler.pyw`](protocol_handler.pyw) 已做最小必要修正，用于同时兼容：

- 源码模式下启动主程序
- 打包后从 `protocol_handler/protocol_handler.exe` 回拉主程序 `M3U8D.exe`
- 打包后日志仍落在安装根目录下的 `logs/`

当前策略为：

- 源码模式优先尝试启动 [`mvs.pyw`](mvs.pyw)
- 若为打包模式，则优先尝试同安装根下的 `M3U8D.exe`
- 协议处理器自己的日志写入安装根 `logs/protocol_handler.log`

## 6. 制作安装器的建议步骤

在安装器联调阶段，建议按下面顺序执行：

1. 确认 Python 环境已安装 PyInstaller
2. 执行 [`build_pyinstaller.bat`](build_pyinstaller.bat) 或 [`build_pyinstaller.py`](build_pyinstaller.py)
3. 检查 `dist/M3U8D` 是否包含预期目录结构
4. 使用 Inno Setup 打开并编译 [`installer/M3U8D.iss`](installer/M3U8D.iss)
5. 生成安装包后，再进行人工安装联调

## 7. 当前仍未验证的风险点

本轮只把配置、脚本、路径约定整理到可联调状态，尚未实际验证：

1. PyInstaller 对当前 PyQt6 / QtWebEngine 依赖的自动收集是否完整
2. 主程序与协议处理器分别 one-dir 打包后，运行时 DLL / Qt 插件是否缺失
3. [`installer/M3U8D.iss`](installer/M3U8D.iss) 中 `[Run]` 的命令行转义在真实安装环境里的最终表现
4. [`scripts/download_dependencies.py`](scripts/download_dependencies.py) 被安装后以系统 Python 调起时，除已补入 [`core/`](core) 与 [`utils/`](utils) 外，是否还存在额外隐式模块依赖
5. 安装后首次运行时，空的 `bin/` 目录与依赖下载流程是否符合预期
6. 协议注册后，浏览器或扩展触发 `m3u8dl://` 时是否能稳定回投到运行中的 GUI
7. 若主程序已运行但端口未及时响应，协议处理器的 12 秒等待窗口是否足够

## 8. 当前联调结论

到本轮为止，项目已经具备“先打包 one-dir 产物，再由安装器直接消费 `dist/M3U8D`”的接线基础；尚缺的主要是一次真实 PyInstaller 构建与一次 Inno Setup 人工安装联调验证。
