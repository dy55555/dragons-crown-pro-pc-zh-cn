#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重建 zh-overlay.ttf（F1/F10 菜单用的中文子集字体）。

普通用户用不到这个脚本 —— zh-overlay.ttf 已随包附带。
只有要「补符号 / 换字体源 / 加译文字符」时才需要跑：

    python build_overlay_font.py

依赖：fontTools（pip install fonttools）
字体源：C:\\Windows\\Fonts\\msyh.ttc（微软雅黑，含中文 + ‹ › 箭头 + 中点）。
        微软雅黑 100% 覆盖 zh-UI.tsv 的全部译文汉字。
        注意不要用 simhei.ttf —— 它没有 U+2039/U+203A（‹ ›），
        那两个箭头正是 F1 菜单左右切换选项用的符号。

收录字符：
  - zh-UI.tsv 第二列（译文）里所有非 ASCII 字符
  - ASCII 0x20–0x7E（F10 / 30 FPS 等）
  - ‹ ›  U+2039/U+203A  —— 选项左右箭头
  - ·    U+00B7         —— 在线页/不兼容提示的分隔点
  - 常用全角标点
子集大小必须 <= 759676 字节（exe 里 DejaVuSans.ttf 的原长度，
apply_zh_ui.py 会补零到这个长度），当前约 435 KB。
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TABLE = HERE / "zh-UI.tsv"
OUT = HERE / "zh-overlay.ttf"
SLOT_LIMIT = 759676
SRC = Path("C:/Windows/Fonts/msyh.ttc")


def main() -> int:
    try:
        from fontTools import subset
        from fontTools.ttLib import TTFont
    except ImportError:
        raise SystemExit("需要先安装 fontTools：pip install fonttools")
    if not SRC.exists():
        raise SystemExit(f"找不到字体源文件：{SRC}")
    if not TABLE.exists():
        raise SystemExit(f"找不到译文表：{TABLE}")

    chars: set[str] = set()
    for line in TABLE.read_text(encoding="utf-8").splitlines():
        parts = line.rstrip("\n").split("\t", 1)
        if len(parts) != 2:
            continue
        for ch in parts[1]:
            if ord(ch) > 127:
                chars.add(ch)

    unicodes = {ord(c) for c in chars}
    unicodes.update(range(0x20, 0x7F))  # ASCII
    unicodes.update([
        0x2039, 0x203A,              # ‹ › 选项左右箭头（F1 菜单）
        0x00B7,                      # · 中点分隔符
        0xFF0C, 0xFF0E, 0xFF1A, 0xFF1B, 0xFF08, 0xFF09, 0xFF01, 0xFF1F,  # 全角标点
    ])

    font = TTFont(str(SRC), fontNumber=0)
    cmap = font.getBestCmap()
    missing = sorted(cp for cp in unicodes if cp > 127 and cp not in cmap)
    if missing:
        print(f"字体源缺少 {len(missing)} 个字符：{[hex(c) for c in missing[:20]]}", file=sys.stderr)
        raise SystemExit("字体源覆盖不全，未生成文件")

    opts = subset.Options()
    opts.desubroutinize = True
    opts.layout_features = []
    sub = subset.Subsetter(options=opts)
    sub.populate(unicodes=sorted(unicodes))
    sub.subset(font)
    font.save(str(OUT))

    size = OUT.stat().st_size
    if size > SLOT_LIMIT:
        raise SystemExit(f"子集 {size} 字节超过槽位 {SLOT_LIMIT} 字节，需进一步精简")
    got = TTFont(str(OUT)).getBestCmap()
    for cp in (0x2039, 0x203A, 0x00B7):
        if cp not in got:
            raise SystemExit(f"生成结果缺少关键符号 {hex(cp)}")
    print(f"已生成 {OUT.name}：{size} 字节 / {len(unicodes)} 字符")
    print("箭头 ‹ ›、中点 ·、ASCII、全部译文汉字均已覆盖。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
