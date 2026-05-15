#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零服务端改动守卫脚本(快照模式)/ zero-server-side-change guard (snapshot mode).

对应 spec `protocol-handler-session-handoff` 的 tasks.md 任务 1.2。

本 bugfix **只允许** 修改仓库根目录下的 `protocol_handler.pyw`。为避免与
`security-stability-hardening` 未提交的硬化改动互相干扰,本守卫采用
**快照模式**:

1. 首次运行 `--capture-baseline` 时,对受守卫路径下的全部 Python 源文件
   计算 SHA-256 哈希,写入基线快照文件:

       ``.kiro/specs/protocol-handler-session-handoff/guard_baseline.sha256``

   该文件是纯文本,每行格式 ``<sha256>  <repo-relative-path>``(两个空格
   分隔,按路径字典序排序,使用 POSIX 斜杠,行尾 ``\n``),便于人眼 diff
   与 CI 复放。请将该文件连同 `protocol_handler.pyw` 的修复一起提交。

2. 后续默认运行(无参数)重新计算受守卫树的哈希,与快照比对。发现
   **Added / Removed / Modified** 任一偏离即非零退出并逐行打印:

       * ``+ <path>``  新增文件
       * ``- <path>``  删除文件
       * ``M <path>``  内容变更

受守卫路径(与 tasks.md 1.2 对齐):

    * core/catcatch_server.py
    * main.py
    * ui/main_window.py
    * engines/      (目录:递归枚举所有 ``*.py``)
    * core/         (目录:递归枚举所有 ``*.py``)

目录展开时排除 ``__pycache__``、``*.pyc``,以及 ``core/download/`` 下的
任何 ``__pycache__`` 缓存目录。位于仓库根的 ``protocol_handler.pyw`` 不
在受守卫路径内,无需显式过滤。

降级策略:若当前环境缺少 git 或不在 git 仓库内,仍以 0 退出并打印
WARNING —— 这与原先 ``git diff`` 版本的姿态一致,避免在 CI 外的裸目录场景
误杀。快照模式本身不依赖 git,这两条 WARNING 仅为行为对齐保留。

仅使用 Python 标准库。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 仓库根:本脚本位于 <repo>/scripts/,向上一级即根。
_REPO_ROOT: Path = Path(__file__).resolve().parent.parent

# 基线快照文件的仓库相对路径。
_BASELINE_REL: str = (
    ".kiro/specs/protocol-handler-session-handoff/guard_baseline.sha256"
)
_BASELINE_PATH: Path = _REPO_ROOT / _BASELINE_REL

# 受守卫路径:单文件 + 目录混合。目录项必须以 "/" 结尾,便于 `_enumerate`
# 通过后缀判断是否递归展开。顺序仅影响去重前的枚举顺序,不影响最终排序。
GUARDED_PATHS: List[str] = [
    "core/catcatch_server.py",
    "main.py",
    "ui/main_window.py",
    "engines/",
    "core/",
]

# 目录枚举时跳过的路径片段(命中任一即排除整条路径)。
_EXCLUDE_DIR_PARTS = frozenset({"__pycache__"})


# ---------------------------------------------------------------------------
# 文件枚举与哈希
# ---------------------------------------------------------------------------


def _is_excluded(rel_parts: Tuple[str, ...]) -> bool:
    """判断相对路径是否命中排除规则(``__pycache__`` 等)。"""
    return any(part in _EXCLUDE_DIR_PARTS for part in rel_parts)


def _enumerate_guarded_files() -> List[Path]:
    """按 GUARDED_PATHS 规则枚举仓库内受守卫的所有 ``.py`` 文件。

    返回绝对路径的有序去重列表(按仓库相对路径 POSIX 形式字典序)。不存在
    的条目安静忽略(目录可能尚未创建)。
    """
    collected: Dict[str, Path] = {}

    for spec in GUARDED_PATHS:
        target = _REPO_ROOT / spec
        if spec.endswith("/"):
            # 目录:递归枚举所有 .py 文件。
            if not target.is_dir():
                continue
            for candidate in target.rglob("*.py"):
                if not candidate.is_file():
                    continue
                rel = candidate.relative_to(_REPO_ROOT)
                if _is_excluded(rel.parts):
                    continue
                key = rel.as_posix()
                collected[key] = candidate
        else:
            # 单文件:直接收录(若存在)。
            if not target.is_file():
                continue
            rel = target.relative_to(_REPO_ROOT)
            if _is_excluded(rel.parts):
                continue
            key = rel.as_posix()
            collected[key] = target

    return [collected[k] for k in sorted(collected)]


def _hash_file(path: Path) -> str:
    """对单个文件计算 SHA-256,分块读取避免一次性载入大文件。"""
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _compute_hashes(files: List[Path]) -> Dict[str, str]:
    """返回 {repo-relative-posix-path: sha256-hex}。"""
    out: Dict[str, str] = {}
    for abs_path in files:
        rel = abs_path.relative_to(_REPO_ROOT).as_posix()
        out[rel] = _hash_file(abs_path)
    return out


# ---------------------------------------------------------------------------
# 快照文件 I/O
# ---------------------------------------------------------------------------


def _format_snapshot(hashes: Dict[str, str]) -> str:
    """生成确定性快照文本:按路径字典序,每行 ``<sha256>  <path>\\n``。"""
    lines = [f"{hashes[key]}  {key}" for key in sorted(hashes)]
    return "\n".join(lines) + ("\n" if lines else "")


def _parse_snapshot(text: str) -> Dict[str, str]:
    """解析快照文本为 {path: sha256}。空行与 ``#`` 开头注释行忽略。"""
    out: Dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # 约定以两个空格分隔;若快照被误手编辑导致单空格,也容错处理。
        if "  " in line:
            sha, _, path = line.partition("  ")
        else:
            sha, _, path = line.partition(" ")
        sha = sha.strip()
        path = path.strip()
        if not sha or not path:
            raise ValueError(
                f"malformed snapshot line {lineno}: {raw!r}"
            )
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha.lower()):
            raise ValueError(
                f"invalid sha256 on snapshot line {lineno}: {sha!r}"
            )
        out[path] = sha.lower()
    return out


# ---------------------------------------------------------------------------
# Git 可用性降级检测(与旧版行为对齐,不影响快照模式正确性)
# ---------------------------------------------------------------------------


def _git_available() -> bool:
    return shutil.which("git") is not None


def _in_git_repo() -> bool:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and completed.stdout.strip() == "true"


def _emit_git_warnings() -> None:
    """打印 git 降级警告但不影响退出码(与旧版一致的信息性姿态)。"""
    if not _git_available():
        print(
            "[guard] WARNING: 未在 PATH 找到 git;快照模式不依赖 git,继续执行",
            file=sys.stderr,
        )
        return
    if not _in_git_repo():
        print(
            "[guard] WARNING: 当前工作目录不在 git 仓库内;快照模式不依赖 git,继续执行",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# 模式:捕获基线
# ---------------------------------------------------------------------------


def _mode_capture_baseline() -> int:
    if _BASELINE_PATH.exists():
        print(
            f"[guard] baseline already exists; remove it manually to re-capture: "
            f"{_BASELINE_REL}",
            file=sys.stderr,
        )
        return 2

    files = _enumerate_guarded_files()
    hashes = _compute_hashes(files)

    _BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _BASELINE_PATH.write_text(_format_snapshot(hashes), encoding="utf-8", newline="\n")

    print(f"[guard] baseline captured: {len(hashes)} files \u2192 {_BASELINE_REL}")
    return 0


# ---------------------------------------------------------------------------
# 模式:默认比对
# ---------------------------------------------------------------------------


def _mode_compare() -> int:
    if not _BASELINE_PATH.exists():
        print(
            "[guard] WARNING: baseline snapshot missing; guard is in non-blocking "
            f"fallback (expected at {_BASELINE_REL}). "
            "Run `python scripts/guard_protocol_handler_diff.py --capture-baseline` "
            "to establish it.",
            file=sys.stderr,
        )
        return 0

    try:
        baseline_text = _BASELINE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"[guard] WARNING: baseline snapshot unreadable ({type(exc).__name__}: "
            f"{exc}); non-blocking fallback",
            file=sys.stderr,
        )
        return 0

    try:
        baseline = _parse_snapshot(baseline_text)
    except ValueError as exc:
        print(
            f"[guard] FAIL: baseline snapshot is malformed: {exc}",
            file=sys.stderr,
        )
        return 1

    current = _compute_hashes(_enumerate_guarded_files())

    baseline_keys = set(baseline)
    current_keys = set(current)

    added = sorted(current_keys - baseline_keys)
    removed = sorted(baseline_keys - current_keys)
    modified = sorted(
        path
        for path in baseline_keys & current_keys
        if baseline[path] != current[path]
    )

    if not (added or removed or modified):
        print(
            f"[guard] OK: {len(current)} files match baseline snapshot"
        )
        return 0

    print("zero-server-side-change guard violated (snapshot deviation):")
    for path in added:
        print(f"+ {path}")
    for path in removed:
        print(f"- {path}")
    for path in modified:
        print(f"M {path}")

    total = len(added) + len(removed) + len(modified)
    print(
        f"[guard] FAIL: {total} deviation(s) from baseline "
        f"(+{len(added)} -{len(removed)} M{len(modified)})",
        file=sys.stderr,
    )
    return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="guard_protocol_handler_diff.py",
        description=(
            "Snapshot-mode zero-server-side-change guard for the "
            "protocol-handler-session-handoff bugfix."
        ),
    )
    parser.add_argument(
        "--capture-baseline",
        action="store_true",
        help=(
            "Capture a new SHA-256 baseline snapshot of the guarded tree. "
            "Refuses to overwrite an existing snapshot."
        ),
    )
    return parser


def main(argv: List[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    _emit_git_warnings()

    if args.capture_baseline:
        return _mode_capture_baseline()
    return _mode_compare()


if __name__ == "__main__":
    sys.exit(main())
