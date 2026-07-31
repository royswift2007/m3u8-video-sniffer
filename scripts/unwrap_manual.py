#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
合并 manual_zh.md / manual_en.md 中被硬换行的长句（保守、基于缩进的精确规则）。

核心规则：只合并"缩进相同"的连续正文行；不破坏"标签行 + 更深缩进内容"的结构。

判定逻辑（逐行）：
- 空行 / 结构行(标题/分隔线/表格/引用/围栏) -> 原样输出，重置上下文。
- 列表项行(行首缩进 + 列表标记) -> 原样输出；记录其缩进与标记列，作为"列表项续行"基准。
- 其余正文行：
  - 若上一保留行是"同缩进的正文行" -> 合并(空格连接)。  # 纯文本段落的硬换行
  - 若上一保留行是"列表项"，且当前行缩进 > 该列表项内容缩进 -> 合并为列表项续行。
  - 否则 -> 新行原样输出。
- "标签行 + 更深缩进内容"因缩进不同，不会被合并。

用法:
    python scripts/unwrap_manual.py resources/manual_zh.md [--write]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# 中文字符范围（CJK 统一表意文字 + 中文标点）
CJK_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def smart_join(prev: str, cont: str) -> str:
    """合并两段文本：仅当连接处两侧都不是中文/中文标点时才加空格。"""
    prev = prev.rstrip()
    cont = cont.lstrip()
    if not prev or not cont:
        return prev + cont
    last_char = prev[-1]
    first_char = cont[0]
    if CJK_RE.match(last_char) or CJK_RE.match(first_char):
        return prev + cont
    return prev + " " + cont

LIST_RE = re.compile(r"^(\s*)(?:- |· |\d+\. |[A-Za-z]\. )")
TOP_HEADING_RE = re.compile(r"^(?:#+\s|\d+(?:\.\d+)*\s+\S)")
RULE_RE = re.compile(r"^\s*[=]{3,}\s*$|^\s*[-]{3,}\s*$")
TABLE_RE = re.compile(r"^\s*\|")
QUOTE_RE = re.compile(r"^\s*>")
FENCE_RE = re.compile(r"^\s*```")


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def is_blank(line: str) -> bool:
    return line.strip() == ""


def is_structure(line: str) -> bool:
    if RULE_RE.match(line):
        return True
    if TABLE_RE.match(line):
        return True
    if QUOTE_RE.match(line):
        return True
    if FENCE_RE.match(line):
        return True
    if TOP_HEADING_RE.match(line):
        return True
    return False


def list_item_indent(line: str) -> int | None:
    """返回列表项的缩进（标记前的空格数），非列表项返回 None。"""
    m = LIST_RE.match(line)
    if not m:
        return None
    return len(m.group(1))


def process(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    # 上一保留行状态
    prev_kind: str | None = None  # "text" | "list" | None
    prev_indent: int = 0  # 上一正文行缩进 / 列表项缩进

    for raw in lines:
        if is_blank(raw):
            out.append("")
            prev_kind = None
            continue

        if is_structure(raw):
            out.append(raw)
            prev_kind = None
            continue

        li = list_item_indent(raw)
        if li is not None:
            out.append(raw)
            prev_kind = "list"
            prev_indent = li
            continue

        # 正文行
        cur_indent = indent_of(raw)
        cont = raw.strip()

        if prev_kind == "text" and cur_indent == prev_indent:
            # 同缩进正文续行 -> 合并
            out[-1] = smart_join(out[-1], cont)
        elif prev_kind == "list" and cur_indent > prev_indent:
            # 列表项续行（缩进比列表项标记更深）-> 合并到列表项
            out[-1] = smart_join(out[-1], cont)
        else:
            # 新行（含"标签行+更深内容"中的更深内容行：因 prev 是同缩进才合并，
            #  这里 prev_kind 可能是 text 但缩进不同，或 prev 是 list 但缩进更浅/相等）
            out.append(raw.rstrip())
            prev_kind = "text"
            prev_indent = cur_indent

    return "\n".join(out)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: unwrap_manual.py <file.md> [--write]", file=sys.stderr)
        return 2
    path = Path(argv[1])
    do_write = len(argv) > 2 and argv[2] == "--write"
    src = path.read_text(encoding="utf-8")
    dst = process(src)
    if do_write:
        path.write_text(dst, encoding="utf-8")
        print(f"written: {path}  ({len(src)} -> {len(dst)} chars)")
    else:
        bl = src.count("\n") + 1
        al = dst.count("\n") + 1
        print(f"{path}: {bl} -> {al} lines ({bl - al} joined)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
