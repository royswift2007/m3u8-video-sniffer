"""
PyInstaller build entry for the real one-dir packaging layout.

Outputs:
- dist/M3U8D/M3U8D.exe
- dist/M3U8D/_internal/
- dist/M3U8D/protocol_handler/protocol_handler.exe
- dist/M3U8D/protocol_handler/_internal/
- dist/M3U8D/resources/
- dist/M3U8D/core/
- dist/M3U8D/utils/
- dist/M3U8D/scripts/
- dist/M3U8D/config.json
- dist/M3U8D/deps.json
- dist/M3U8D/cookies/ (empty runtime directory only; source private cookies are not copied)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
DIST_ROOT = ROOT_DIR / "dist"
APP_DIST_DIR = DIST_ROOT / "M3U8D"
PROTOCOL_BUILD_DIR = DIST_ROOT / "protocol_handler"
BUILD_ROOT = ROOT_DIR / "build" / "pyinstaller"
SPEC_ROOT = BUILD_ROOT / "spec"

MAIN_ENTRY = ROOT_DIR / "mvs.pyw"
PROTOCOL_ENTRY = ROOT_DIR / "protocol_handler.pyw"
PRIMARY_APP_ICON = ROOT_DIR / "resources" / "icons" / "mvs.ico"
APP_ICON_CANDIDATES = (
    PRIMARY_APP_ICON,
    ROOT_DIR / "resources" / "mvs.ico",
    ROOT_DIR / "mvs.ico",
)


def _resolve_app_icon() -> Path:
    for candidate in APP_ICON_CANDIDATES:
        if candidate.exists():
            return candidate
    attempted = ", ".join(str(path) for path in APP_ICON_CANDIDATES)
    raise FileNotFoundError(
        f"未找到应用图标。正式路径应为: {PRIMARY_APP_ICON}；已尝试: {attempted}"
    )


APP_ICON = _resolve_app_icon()

STAGED_FILES = (
    (ROOT_DIR / "config.json", APP_DIST_DIR / "config.json"),
    (ROOT_DIR / "deps.json", APP_DIST_DIR / "deps.json"),
)

STAGED_DIRECTORIES = (
    (ROOT_DIR / "resources", APP_DIST_DIR / "resources"),
    (ROOT_DIR / "core", APP_DIST_DIR / "core"),
    (ROOT_DIR / "utils", APP_DIST_DIR / "utils"),
)

STAGED_SCRIPTS = (
    "download_tools.bat",
    "download_dependencies.py",
    "register_protocol.bat",
    "uninstall_protocol.bat",
)

# Writable runtime directories that should exist in the packaged app.
# Note: source-side cookies/ may contain private user data and must never be staged.
PLACEHOLDER_DIRECTORIES = (
    APP_DIST_DIR / "bin",
    APP_DIST_DIR / "cookies",
    APP_DIST_DIR / "logs",
    APP_DIST_DIR / "Temp",
)


def _format_add_data(source: Path, target_relative_dir: str) -> str:
    return f"{source}{os.pathsep}{target_relative_dir}"


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _run_pyinstaller(name: str, entry_script: Path, work_dir: Path, add_data: list[str] | None = None) -> None:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--onedir",
        "--name",
        name,
        "--distpath",
        str(DIST_ROOT),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(SPEC_ROOT),
        "--icon",
        str(APP_ICON),
    ]

    for item in add_data or []:
        command.extend(["--add-data", item])

    command.append(str(entry_script))

    print("[BUILD]", " ".join(f'"{part}"' if " " in part else part for part in command))
    subprocess.run(command, check=True, cwd=str(ROOT_DIR))


def _stage_support_files() -> None:
    for source_dir, dest_dir in STAGED_DIRECTORIES:
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        shutil.copytree(
            source_dir,
            dest_dir,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )

    for source_file, dest_file in STAGED_FILES:
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, dest_file)

    scripts_dir = APP_DIST_DIR / "scripts"
    if scripts_dir.exists():
        shutil.rmtree(scripts_dir)
    scripts_dir.mkdir(parents=True, exist_ok=True)
    for script_name in STAGED_SCRIPTS:
        shutil.copy2(ROOT_DIR / "scripts" / script_name, scripts_dir / script_name)

    for directory in PLACEHOLDER_DIRECTORIES:
        directory.mkdir(parents=True, exist_ok=True)


def _stage_protocol_handler_bundle() -> None:
    destination = APP_DIST_DIR / "protocol_handler"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.move(str(PROTOCOL_BUILD_DIR), str(destination))


def main() -> int:
    print("=" * 60)
    print("PyInstaller 构建开始")
    print(f"ROOT_DIR = {ROOT_DIR}")
    print(f"APP_DIST_DIR = {APP_DIST_DIR}")
    print("[INFO] 源码 cookies/ 目录仅用于本机运行，打包时不会复制其中任何私有文件。")
    print("=" * 60)

    if not MAIN_ENTRY.exists():
        raise FileNotFoundError(f"未找到主程序入口: {MAIN_ENTRY}")
    if not PROTOCOL_ENTRY.exists():
        raise FileNotFoundError(f"未找到协议处理器入口: {PROTOCOL_ENTRY}")
    if not APP_ICON.exists():
        raise FileNotFoundError(f"未找到应用图标: {APP_ICON}")

    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    SPEC_ROOT.mkdir(parents=True, exist_ok=True)

    _remove_path(APP_DIST_DIR)
    _remove_path(PROTOCOL_BUILD_DIR)

    main_add_data = [
        _format_add_data(ROOT_DIR / "resources", "resources"),
    ]

    protocol_add_data = [
        _format_add_data(ROOT_DIR / "resources", "resources"),
    ]

    _run_pyinstaller(
        name="M3U8D",
        entry_script=MAIN_ENTRY,
        work_dir=BUILD_ROOT / "main",
        add_data=main_add_data,
    )
    _run_pyinstaller(
        name="protocol_handler",
        entry_script=PROTOCOL_ENTRY,
        work_dir=BUILD_ROOT / "protocol_handler",
        add_data=protocol_add_data,
    )

    _stage_protocol_handler_bundle()
    _stage_support_files()

    print()
    print("构建完成，当前输出目录：")
    print(f"- {APP_DIST_DIR}")
    print(f"- {APP_DIST_DIR / 'M3U8D.exe'}")
    print(f"- {APP_DIST_DIR / '_internal'}")
    print(f"- {APP_DIST_DIR / 'protocol_handler' / 'protocol_handler.exe'}")
    print(f"- {APP_DIST_DIR / 'resources'}")
    print(f"- {APP_DIST_DIR / 'scripts'}")
    print(f"- {APP_DIST_DIR / 'config.json'}")
    print(f"- {APP_DIST_DIR / 'deps.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
