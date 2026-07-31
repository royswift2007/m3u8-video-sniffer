#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
收紧 manual_zh.md / manual_en.md 中"列表项 + 空行 + 紧接内容"的多余空行。

背景：手册的自定义渲染器 render_markdown_to_html 把每个空行转成 <br>，
把每个非空行包成独立 <p>（带 margin）。因此"列表项 + 空行 + 下一行"会
叠加 <br> + <p> margin，视觉上出现约 2 行的间距。

规则（保守、精确）：
- 仅删除"列表项行 + 空行 + 任意非空行"中的那个空行，但下一行若是
  章节分隔线(====/----)、Markdown 标题(#/##/###) 或表格行(|)则保留。
- 列表项行定义：行首为空白 + 列表标记（- / * / + / 数字. / 字母. / · ）。
- 保留所有其他空行（段落分隔、章节分隔、表格/围栏周围等）。
- 保留列表块 *之前* 的空行（用于与上文分隔）。
- 连续多个空行只处理第一个紧跟列表项的空行。

用法:
    python scripts/tighten_manual_lists.py resources/manual_zh.md           # 预览
    python scripts/tighten_manual_lists.py resources/manual_zh.md --write   # 写回
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# 列表项行：行首缩进 + 列表标记
LIST_RE = re.compile(r"^(\s*)(?:[-*+] |· |\d+\. |[A-Za-z]\. )")
# 章节分隔线（==== / ----）
RULE_RE = re.compile(r"^\s*[=]{3,}\s*$|^\s*[-]{3,}\s*$")
# Markdown 标题（#/##/###）
MD_HEADING_RE = re.compile(r"^\s*#{1,6}\s+\S")
# 数字标题（如 "2.2 命令行参数启动"）
NUM_HEADING_RE = re.compile(r"^\s*\d+(?:\.\d+)*\s+\S")
# 表格行
TABLE_RE = re.compile(r"^\s*\|")


def is_list_item(line: str) -> bool:
    return LIST_RE.match(line) is not None


def is_blank(line: str) -> bool:
    return line.strip() == ""


def should_keep_blank_before(next_line: str) -> bool:
    """判断列表项后的空行是否应保留（即下一行是章节分隔/Markdown标题/表格）。"""
    if RULE_RE.match(next_line):
        return True
    if MD_HEADING_RE.match(next_line):
        return True
    if TABLE_RE.match(next_line):
        return True
    return False


def tighten(text: str) -> tuple[str, int]:
    lines = text.split("\n")
    out: list[str] = []
    removed = 0
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        # 模式：列表项 + 空行 + 非空行 -> 删除空行（除非下一行是章节分隔/标题/表格）
        if (
            i + 2 < n
            and is_list_item(line)
            and is_blank(lines[i + 1])
            and not is_blank(lines[i + 2])
            and not should_keep_blank_before(lines[i + 2])
        ):
            out.append(line)
            # 跳过空行（删除）
            removed += 1
            i += 2
            continue
        out.append(line)
        i += 1
    return "\n".join(out), removed


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: tighten_manual_lists.py <file.md> [--write]", file=sys.stderr)
        return 2
    path = Path(argv[1])
    do_write = len(argv) > 2 and argv[2] == "--write"
    src = path.read_text(encoding="utf-8")
    dst, removed = tighten(src)
    if do_write:
        path.write_text(dst, encoding="utf-8")
        print(f"written: {path}  (removed {removed} blank lines)")
    else:
        bl = src.count("\n") + 1
        al = dst.count("\n") + 1
        print(f"{path}: {bl} -> {al} lines (would remove {removed} blank lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
