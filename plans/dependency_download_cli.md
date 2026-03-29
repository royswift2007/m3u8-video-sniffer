# 依赖下载命令行入口说明

新增了可供安装脚本或安装器复用的轻量入口 [`scripts/download_dependencies.py`](scripts/download_dependencies.py)。当前约定如下：

## 调用方式

在项目根目录执行：

```powershell
python scripts/download_dependencies.py
```

同时下载必须依赖和建议依赖：

```powershell
python scripts/download_dependencies.py --include-recommended
```

## 参数约定

- 默认仅处理 `required`
- 传入 `--include-recommended` 时处理 `required,recommended`
- 当前不开放 `optional` 下载入口，避免超出本轮最小范围

## 返回码约定

- `0`：全部请求分类下载成功，或目标文件已存在被跳过
- `1`：执行完成，但至少一个依赖下载/安装失败
- `3`：入口运行时异常，例如清单解析失败、环境初始化失败等

> `argparse` 自带参数错误退出码保持 Python 默认行为，不额外覆盖。

## 控制台输出约定

入口会输出简洁摘要，便于安装脚本解析，例如：

```text
[SUMMARY] categories=required,recommended requested=5 success=2 skipped=3 failed=0
[SUMMARY] category=required requested=3 success=1 skipped=2 failed=0
[SUMMARY] category=recommended requested=2 success=1 skipped=1 failed=0
```

若存在失败项，还会追加：

```text
[ERROR] Streamlink (C:\path\bin\streamlink.exe): 未找到匹配的 GitHub 发布资源
```

## 当前接入边界

- 已复用 [`core/dependency_installer.py`](core/dependency_installer.py) 的下载实现
- 已支持按分类调用 `required` / `recommended`
- 已补齐 [`deps.json`](deps.json) 中建议依赖的下载配置，供 CLI 入口直接复用
- 尚未接入任何 `.bat` 脚本或安装器
- 尚未提供可选依赖 `optional` 的公开 CLI 参数
