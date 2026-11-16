#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dragon's Crown Pro PC — 启动器界面简体中文汉化

只改界面文字，不改任何功能
==========================
启动器每条界面文本都以英文字面量编译在 dragons_crown_pro.exe 的 .rdata 里。
本工具只做三件事：

1. 把 .rdata 里的英文界面串就地换成中文（中文比英文短时最安全）；
   少数中文更长的，把中文写进 .rdata 的空闲区，只把引用它的
   `lea reg,[rip+xxx]` 指令改成指向新地址。
2. 界面字体名 Segoe UI → 系统中文字体（否则中文显示为方块）。
3. ImGui 默认字形范围由 Latin 扩到 0x20–0x9FFF（让中文字形被光栅化）。

**不改**：游戏内语言设置（user/dcp.ini 的 text_language）、金手指、
联机、存档、任何选项默认值与逻辑分支。

用法
----
    python apply_zh_ui.py               # 汉化（自动备份官方原版）
    python apply_zh_ui.py --check       # 只看状态
    python apply_zh_ui.py --restore     # 还原官方原版
    python apply_zh_ui.py --exe PATH    # 指定 exe
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TABLE = HERE / "zh-UI.tsv"
BACKUP_SUFFIX = ".zhcn-orig"
FONT_SLOT = b"simhei.ttf"          # 10 字符 + NUL，正好填满 11 字节槽
# 跳过：小写配置键 / 明显是取值而非界面文字
SKIP_EXACT = {
    "ok", "on", "off", "auto", "yes", "no", "none", "true", "false",
    "xbox", "playstation", "windowed", "fullscreen", "borderless",
}
# 这三个词由锚点定点补丁处理，不能走通用替换（原因见 patch_special 注释）
SKIP_SPECIAL = {"play", "about", "russian"}

# 定点补丁：(英文锚点, 汉化后锚点, 目标串在英文锚点内的偏移, 英文原文, 中文译文)
# 用整段上下文做锚点，而不是写死文件偏移 —— 这样 exe 升级后只要界面结构没大改，
# 锚点仍能命中，一键汉化可跨版本复用。
# 两份锚点供 --check 精确判断：只看目标词是否落地，不会因别处出现同字而误报。
SPECIAL_ANCHORS = (
    # Play 紧贴 "Generic PnP Moni" 尾部，"前一字节必须是 NUL" 的规则会漏掉它
    (b"Generic PnP MoniPlay\x00", b"Generic PnP Moni" + "玩".encode(),
     16, "Play", "玩"),
    # About 5 字符槽装不下 6 字节的「关于」，但紧邻的下一个字节本来就是 NUL，可借 1 字节
    (b"Bug Report\x00About\x00", "反馈".encode() + b"\x00" * 5 + "关于".encode(),
     11, "About", "关于"),
    # 语言显示名：小写 russian 是内部键（绝不能动），紧跟的才是显示名
    (b"russian\x00Russian\x00", b"russian\x00" + "俄语".encode(), 8, "Russian", "俄语"),
)


# --------------------------------------------------------------------------- PE
class PE:
    def __init__(self, data: bytes):
        self.data = data
        e = struct.unpack_from("<I", data, 0x3C)[0]
        if data[e:e + 4] != b"PE\0\0":
            raise ValueError("不是有效的 PE 文件")
        nsec = struct.unpack_from("<H", data, e + 6)[0]
        optsz = struct.unpack_from("<H", data, e + 20)[0]
        opt = e + 24
        self.image_base = struct.unpack_from("<Q", data, opt + 24)[0]
        sec = opt + optsz
        self.secs = []
        for i in range(nsec):
            o = sec + i * 40
            name = data[o:o + 8].rstrip(b"\0").decode("latin-1")
            vsz, va, rsz, ra = struct.unpack_from("<IIII", data, o + 8)
            self.secs.append((name, va, vsz, ra, rsz))

    def sec(self, name):
        for s in self.secs:
            if s[0] == name:
                return s
        raise KeyError(name)

    def off2rva(self, off):
        for n, va, vsz, ra, rsz in self.secs:
            if ra <= off < ra + rsz:
                return va + (off - ra)
        return None

    def rva2off(self, rva):
        for n, va, vsz, ra, rsz in self.secs:
            if va <= rva < va + max(vsz, rsz):
                return ra + (rva - va)
        return None


def unescape(s: str) -> str:
    """译文表用 \\s / \\n / \\t / \\\\ 表示空格等，这里还原成真实字符。"""
    out, i = [], 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            c = s[i + 1]
            out.append({"s": " ", "n": "\n", "t": "\t", "\\": "\\"}.get(c, "\\" + c))
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def load_table() -> dict[str, str]:
    table = {}
    if not TABLE.exists():
        raise SystemExit(f"找不到译文表：{TABLE}")
    for line in TABLE.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        k, _, v = line.partition("\t")
        if k:
            table[unescape(k)] = unescape(v)
    return table


# --------------------------------------------------------------- 引用索引
def build_lea_index(pe: PE) -> dict[int, list[tuple[int, int]]]:
    """扫描 .text，收集所有 `lea reg,[rip+disp32]`：目标rva -> [(指令rva, disp文件偏移)]"""
    t = pe.sec(".text")
    tva, tra, rsz = t[1], t[3], t[4]
    code = pe.data[tra:tra + rsz]
    idx: dict[int, list[tuple[int, int]]] = {}
    n = len(code) - 7
    i = 0
    while i < n:
        b0 = code[i]
        if 0x40 <= b0 <= 0x4F and code[i + 1] == 0x8D:
            modrm = code[i + 2]
            if modrm & 0xC7 == 0x05:          # mod=00, rm=101 → RIP 相对
                disp = struct.unpack_from("<i", code, i + 3)[0]
                tgt = tva + i + 7 + disp
                idx.setdefault(tgt, []).append((tva + i, tra + i + 3))
                i += 7
                continue
        i += 1
    return idx


# --------------------------------------------------------------- 空闲区
def _textual(blob: bytes) -> bool:
    return all(c == 0 or c in (9, 10, 13) or 32 <= c < 127 for c in blob)


def find_hole(pe: PE, need: int):
    name, va, vsz, ra, rsz = pe.sec(".rdata")
    end = ra + rsz
    best = None
    i = ra
    while i < end:
        if pe.data[i] == 0:
            j = i
            while j < end and pe.data[j] == 0:
                j += 1
            ln = j - i
            if ln >= need and _textual(pe.data[max(ra, i - 16):i]) and \
               _textual(pe.data[j:min(end, j + 16)]):
                if best is None or ln > best[2]:
                    best = (va + (i - ra), va + (j - ra), ln)
            i = j
        else:
            i += 1
    if not best:
        raise SystemExit("在 .rdata 中找不到足够的空闲空间")
    return best


# --------------------------------------------------------------- 字体
# 字体名在 exe 里有两套，必须都换掉：
#   .text  —— `movabs rax, "segoeui."` + `mov [..], ".ttf"`，启动器界面用
#   .rdata —— 独立的 "segoeui.ttf" 等字符串，游戏内按 F1 呼出的覆盖菜单用
# 只改前者的话，游戏内菜单仍是真 Segoe UI（没有中文字形），整屏方块。
FONT_NAMES = (b"segoeui.ttf", b"seguisb.ttf", b"segoeuib.ttf")


def patch_fonts(buf: bytearray):
    nums = patch_font_inline(buf)
    lits = patch_font_literals(buf)
    changed = []
    if nums:
        changed.append(f"内联 {nums} 处")
    if lits:
        changed.append(f"字面量 {lits} 处")
    return changed


def patch_font_inline(buf: bytearray) -> int:
    """改 .text 里 movabs 立即数形式的字体名"""
    n = 0
    for orig in (b"segoeui.", b"seguisb.", b"segoeuib"):
        needle = b"\x48\xb8" + orig
        i = buf.find(needle)
        if i < 0:
            continue
        buf[i + 2:i + 10] = FONT_SLOT[:8]
        j = buf.find(b"\x2e\x74\x74\x66", i + 10, i + 40)
        if j < 0:
            continue
        disp8 = buf[j - 1]
        buf[j:j + 4] = b"ttf\0" if disp8 == 7 else b"tf\0\0"
        n += 1
    return n


def patch_font_literals(buf: bytearray) -> int:
    """改 .rdata 里独立存放的字体名字符串（游戏内覆盖菜单用）"""
    n = 0
    for orig in FONT_NAMES:
        i = 0
        while True:
            i = buf.find(orig + b"\0", i)
            if i < 0:
                break
            if i == 0 or buf[i - 1] == 0:
                buf[i:i + len(orig)] = FONT_SLOT[:len(orig)].ljust(len(orig), b"\0")
                n += 1
            i += 1
    return n


def patch_glyph_range(buf: bytearray, pe: PE):
    """ImGui GetGlyphRangesDefault 的 {0x20,0xFF,0} → {0x20,0x9FFF,0}

    这是 u16 数组：低界 0x20、高界 0xFF、结尾 0。高界是小端 u16，必须整体
    写成 0x9FFF（字节 FF 9F）。曾误写成只改一个字节 9F，得到高界 0x009F，
    中文字形不在范围内 → F1 菜单全是菱形缺字符。
    """
    name, va, vsz, ra, rsz = pe.sec(".rdata")
    i = buf.find(b"\x20\x00\xff\x00\x00\x00", ra, ra + rsz)
    if i < 0:
        return False
    struct.pack_into("<H", buf, i + 2, 0x9FFF)
    return True


# --------------------------------------------------------------- 代码拼接串
# 少数界面文字不是整串放在 .rdata，而是「.rdata 里的前缀 + 代码里的 8 字节立即数」
# 在运行时拼出来的。例如：
#     movups xmm0, [rd + 0]        ; 拷贝前 16 字节
#     movups xmm0, [rd + 16]       ; 再拷 16 字节
#     movabs rcx, "playing."       ; 最后 8 字节来自指令立即数
#     mov    [rax + 0x1f], rcx     ; 写到偏移 31
#     mov    byte [rax + 0x27], 0  ; 结尾
# 这种串如果写满全长，第 31 字节起会被立即数盖掉，界面上就会出现半个汉字
# （"检查游戏文件并开始游" + 乱码 + "aying."）。所以要先算出上限，写不进去就跳过。
SSE_RIP_LOADS = (
    b"\x0f\x10\x05",        # movups xmm, [rip+d]
    b"\x0f\x28\x05",        # movaps xmm, [rip+d]
    b"\x0f\x6f\x05",        # movdqa/movdqu xmm, [rip+d]（带 66/F3/F2 前缀）
    b"\x66\x0f\x6f\x05",
    b"\xf3\x0f\x6f\x05",
    b"\xf2\x0f\x6f\x05",
)


def bulk_copy_sites(pe: PE) -> dict[int, list[int]]:
    """目标rva -> [代码rva]，表示这些代码把该地址起的 16 字节当常量整块拷走"""
    _n, tva, _vs, tra, trsz = pe.sec(".text")
    code = pe.data[tra:tra + trsz]
    sites: dict[int, list[int]] = {}
    for pat in SSE_RIP_LOADS:
        start = 0
        while True:
            i = code.find(pat, start)
            if i < 0:
                break
            dpos = i + len(pat)          # modrm 之后就是 disp32
            if dpos + 4 <= len(code):
                disp = struct.unpack_from("<i", code, dpos)[0]
                tgt = tva + i + len(pat) + 4 + disp
                sites.setdefault(tgt, []).append(tva + i)
            start = i + 1
    return sites


def inline_tail_caps(pe: PE, jobs, sites) -> dict[str, list[tuple[int, int]]]:
    """找出「位置固定由代码拼装」的字符串。

    返回 {英文原文: [(安全上限, 立即数所在文件偏移), ...]}。上限 = 该串前多少个字节
    真正来自 .rdata；剩下的字节由代码里的 8 字节立即数补上，所以要一并改。
    """
    _n, tva, _vs, tra, trsz = pe.sec(".text")
    code = pe.data[tra:tra + trsz]
    caps: dict[str, list[tuple[int, int]]] = {}
    for en, eb, _zb, pos in jobs:
        for off in pos:
            rva = pe.off2rva(off)
            for site in sites.get(rva, ()):
                w0 = site - tva          # 代码点在 .text 里的下标
                window = code[w0:w0 + 128]
                found = False
                for j in range(len(window) - 10):
                    if window[j] != 0x48 or not (0xB8 <= window[j + 1] <= 0xBF):
                        continue
                    imm = window[j + 2:j + 10]
                    k = eb.find(imm, 1)
                    while k > 0:
                        if k <= len(eb) - 8:
                            imm_off = tra + w0 + j + 2
                            caps.setdefault(en, []).append((k, imm_off))
                            found = True
                        k = eb.find(imm, k + 1)
                if found:
                    break
    return caps


# --------------------------------------------------------------- 导入名保护
def import_name_spans(pe: PE) -> list[tuple[int, int]]:
    """导入表里所有 DLL 名/函数名在文件中的 (偏移, 长度)。

    这些名字也是 .rdata 里的普通 C 串，一旦被换成中文，Windows 加载器会以
    STATUS_ENTRYPOINT_NOT_FOUND(0xC0000139) 直接拒绝启动。打完补丁后逐条比对，
    有任何一条变了就整批作废。
    """
    d = pe.data
    e = struct.unpack_from("<I", d, 0x3C)[0]
    opt = e + 24
    imp_rva = struct.unpack_from("<I", d, opt + 112 + 8)[0]
    if not imp_rva:
        return []
    off = pe.rva2off(imp_rva)
    if off is None:
        return []
    spans = []
    k = 0
    while True:
        desc = d[off + k * 20: off + k * 20 + 20]
        if len(desc) < 20 or desc == b"\0" * 20:
            break
        oft, _ts, _fc, name_rva, ft = struct.unpack("<IIIII", desc)
        for rva in (name_rva,):
            o = pe.rva2off(rva)
            if o is not None:
                spans.append((o, d.find(b"\0", o) - o))
        thunk = oft or ft
        to = pe.rva2off(thunk)
        if to is not None:
            n = 0
            while True:
                v = struct.unpack_from("<Q", d, to + n * 8)[0]
                if v == 0:
                    break
                if not (v >> 63):
                    o = pe.rva2off(v & 0xFFFFFFFF)
                    if o is not None:
                        spans.append((o + 2, d.find(b"\0", o + 2) - (o + 2)))
                n += 1
        k += 1
    return spans


# --------------------------------------------------------------- 定点补丁
def patch_special(buf: bytearray, pe: PE) -> list[tuple[str, int]]:
    """处理通用替换覆盖不到的少数界面词。

    这三处都是「定长槽 + 紧邻 NUL」结构，直接就地写，绝不移动任何字节：
      1. Play    —— 前一字节不是 NUL（Generic PnP Moni|Play），通用规则跳过
      2. About   —— 译文 6 字节比原文 5 字节长，通用规则会走迁移分支，
                    但导航块没有 lea 引用，迁移后 UI 仍读旧地址。实际槽后
                    紧邻一个空闲 NUL，借来即可，不移动任何东西。
      3. Russian —— 表里原先没有；小写 russian 是内部语言键不能改，
                    只改紧跟其后的显示名。

    返回 [(英文, 命中处数), ...] 便于输出。
    """
    _n, _va, _vsz, ra, rsz = pe.sec(".rdata")
    end = ra + rsz
    done = []
    for anchor, _zh_anchor, disp, en, zh in SPECIAL_ANCHORS:
        zb = zh.encode()
        hit = 0
        i = ra
        while True:
            i = buf.find(anchor, i, end)
            if i < 0:
                break
            p = i + disp                      # 目标串起点
            slot = len(en) + 1                # 原英文 + NUL 的槽长
            need = len(zb) + 1                # 中文 + NUL
            # 中文比槽长时，借用紧邻的 NUL；若紧邻不是 NUL 则放弃，避免踩坏下一个串
            if need > slot:
                extra = need - slot
                if any(buf[p + slot + k] != 0 for k in range(extra)):
                    i += 1
                    continue
            buf[p:p + need] = zb + b"\0"
            hit += 1
            i += len(anchor)
        if hit:
            done.append((en, hit))
    return done


# --------------------------------------------------------------- 状态检查
def is_patched(data: bytes) -> bool:
    """字体名已被换成 simhei.ttf 就算已汉化（官方原版绝不会出现这个串）"""
    return b"simhei.ttf" in data or b"simhei.t" in data


def do_check(exe: Path, orig: Path, data: bytes, rdra: int, rd_end: int,
             patched: bool, table) -> int:
    same_as_orig = orig.exists() and data == orig.read_bytes()
    print(f"exe        : {exe}")
    print(f"SHA-256    : {sha256(data)}")
    print(f"原版备份   : {orig.name if orig.exists() else '（不存在，首次汉化时会自动生成）'}")
    print()
    if same_as_orig:
        print("当前状态   : 官方原版（未汉化）")
    elif patched:
        print("当前状态   : 已汉化")
    else:
        print("当前状态   : 未知（既不是原版备份，也不像汉化版）")

    print(f"字体名     : simhei {data.count(b'simhei.t')} 处，"
          f"Segoe UI 残留 {data.count(b'segoeui.')} 处")
    rng = "0x20–0xFF（未扩展，中文会是方块）"
    if data.find(b"\x20\x00\xff\x9f\x00\x00", rdra, rd_end) >= 0:
        rng = "0x20–0x9FFF（已扩展）"
    elif data.find(b"\x20\x00\x9f\x00\x00\x00", rdra, rd_end) >= 0:
        rng = "0x20–0x9F（改错了！高界只写了单字节，中文仍是方块）"
    print(f"字形范围   : {rng}")
    # 三处定点补丁：锚点本身含会汉化的词，英文锚点消失即说明补丁已落地
    pe = PE(data)
    _n, _va, _vsz, rdra, rdrsz = pe.sec(".rdata")
    end = rdra + rdrsz
    for anchor, zh_anchor, disp, en, zh in SPECIAL_ANCHORS:
        if data.find(anchor, rdra, end) >= 0:
            state = "未汉化"
        elif data.find(zh_anchor, rdra, end) >= 0:
            state = "已汉化"
        else:
            state = "（两者都找不到，界面结构可能已变）"
        print(f"定点 {en:<8}: {state}")
    print()
    if patched and data.count(b"segoeui.") > 0:
        print("提醒：还有 Segoe UI 字体名没换掉，游戏内 F1 菜单会是方块。")
    elif patched:
        print("启动器界面与游戏内 F1 菜单的字体都已换好。")
    return 0


# --------------------------------------------------------------- 主流程
def do_apply(exe: Path, restore: bool, check: bool):
    orig = exe.with_name(exe.name + BACKUP_SUFFIX)
    if restore:
        if not orig.exists():
            raise SystemExit(f"找不到官方原版备份：{orig}")
        shutil.copy2(orig, exe)
        print(f"已还原官方原版 → {exe}")
        print("SHA-256:", sha256(exe))
        return 0

    data = exe.read_bytes()
    pe = PE(data)
    table = load_table()
    lea_idx = build_lea_index(pe)
    rdn, rdva, rdvsz, rdra, rdrsz = pe.sec(".rdata")
    rd_end = ra_end = rdra + rdrsz
    patched = is_patched(data)
    if check:
        return do_check(exe, orig, data, rdra, ra_end, patched, table)
    if patched:
        print("这个 exe 已经是汉化版，无需重复应用。")
        print("要换回官方原版请用 --restore；官方升级覆盖后直接重跑即可。")
        print("SHA-256:", sha256(exe))
        return 0

    # 逐条收集：字符串副本位置
    jobs = []
    for en, zh in table.items():
        if en.lower() in SKIP_EXACT or en.lower() in SKIP_SPECIAL:
            continue
        eb, zb = en.encode(), zh.encode()
        pos = []
        i = rdra
        while True:
            i = data.find(eb + b"\0", i, rd_end)
            if i < 0:
                break
            # 只认真正的字符串起点：前一个字节必须是 NUL。
            # 否则会撞进更长的串内部，例如导入名 SDL_Quit 里的 Quit、
            # 或 "Cheats: disabled for Online Play" 里的 Play，改坏后 exe 直接加载失败。
            if i == rdra or data[i - 1] == 0:
                pos.append(i)
            i += 1
        if pos:
            jobs.append((en, eb, zb, pos))

    buf = bytearray(data)

    # 0) 定点补丁：三处通用规则处理不了的词（Play / About / Russian）
    special = patch_special(buf, pe)

    # 1) 就地替换；代码拼装的串连立即数一起改
    caps = inline_tail_caps(pe, jobs, bulk_copy_sites(pe))
    n_inplace = 0
    n_tail = 0
    need_reloc = []
    skipped = []
    for en, eb, zb, pos in jobs:
        pairs = caps.get(en)
        if not pairs:
            tail_cap, imm_offs = None, []
        else:
            ks = {k for k, _o in pairs}
            if len(ks) != 1:
                skipped.append((en, zb, min(ks), "同一串有多处拼装点，上限不一致"))
                continue
            tail_cap = ks.pop()
            imm_offs = [o for _k, o in pairs]

        total = len(eb) + 1
        if tail_cap is None:
            if len(zb) <= len(eb):
                for off in pos:
                    buf[off:off + len(zb)] = zb
                    buf[off + len(zb):off + total] = b"\0" * (total - len(zb))
                    n_inplace += 1
            else:
                need_reloc.append((en, zb, pos))
            continue

        # 有代码尾巴：中文长度要么塞进前缀，要么「前缀 + 立即数」两段拼起来
        if len(zb) < tail_cap:
            for off in pos:
                buf[off:off + len(zb)] = zb
                buf[off + len(zb):off + total] = b"\0" * (total - len(zb))
                n_inplace += 1
        elif len(zb) <= tail_cap + 8 and tail_cap >= 8 and len(eb) <= tail_cap + 8:
            payload = zb[tail_cap:].ljust(8, b"\0")[:8]
            for off in pos:
                buf[off:off + tail_cap] = zb[:tail_cap]
            for io in imm_offs:
                buf[io:io + 8] = payload
            n_inplace += len(pos)
            n_tail += 1
        else:
            skipped.append((en, zb, tail_cap, None))

    # 2) 超长的写入空闲区 + 改引用
    total_need = sum(len(zb) + 1 for _e, zb, _p in need_reloc)
    n_reloc = 0
    if need_reloc:
        hole_va, hole_end, hole_len = find_hole(pe, total_need)
        if hole_len < total_need:
            raise SystemExit("空闲区不足")
        base_off = pe.rva2off(hole_va)
        cur = base_off
        for en, zb, pos in need_reloc:
            buf[cur:cur + len(zb)] = zb
            buf[cur + len(zb)] = 0
            new_rva = pe.off2rva(cur)
            cur += len(zb) + 1
            for off in pos:
                rva = pe.off2rva(off)
                for lea_rva, disp_off in lea_idx.get(rva, []):
                    d = new_rva - (lea_rva + 7)
                    buf[disp_off:disp_off + 4] = struct.pack("<i", d)
                    n_reloc += 1

    # 3) 字体与字形范围
    fonts = patch_fonts(buf)
    glyph = patch_glyph_range(buf, pe)

    # 4) 保险：导入名一个字节都不许变
    broken = [(o, data[o:o + n], bytes(buf[o:o + n]))
              for o, n in import_name_spans(pe)
              if n > 0 and buf[o:o + n] != data[o:o + n]]
    if broken:
        for o, a, b in broken[:10]:
            print(f"  导入名被误改 @{o:#x}: {a!r} -> {b!r}", file=sys.stderr)
        raise SystemExit(f"中止：{len(broken)} 个导入名会被改坏，exe 将无法启动。")

    if not orig.exists():
        shutil.copy2(exe, orig)
        print(f"已备份官方原版 → {orig.name}")

    exe.write_bytes(buf)
    if special:
        print(f"定点补丁 : " + ", ".join(f"{en}×{n}" for en, n in special))
    print(f"就地替换 : {n_inplace} 处")
    if n_tail:
        print(f"拼接改写 : {n_tail} 条（文字由代码拼装，已连同指令立即数一起改）")
    print(f"迁移改写 : {n_reloc} 处引用（{len(need_reloc)} 条长文本）")
    if skipped:
        print(f"未汉化   : {len(skipped)} 条（改动会超出代码拼装点，保留英文更安全）")
        for en, zb, cap, why in skipped:
            print(f"           上限 {cap:>3} 字节 / 需要 {len(zb):>3}：{en}"
                  + (f"  [{why}]" if why else ""))
    print(f"字体替换 : {', '.join(fonts) if fonts else '已处理'}")
    print(f"字形范围 : {'0x20–0x9FFF' if glyph else '未找到（可能已处理）'}")
    print(f"新 SHA-256: {sha256(exe)}")
    return 0


def sha256(p):
    raw = p if isinstance(p, (bytes, bytearray)) else Path(p).read_bytes()
    return hashlib.sha256(raw).hexdigest()


def main():
    ap = argparse.ArgumentParser(description="龙之皇冠 Pro PC 启动器界面汉化")
    ap.add_argument("--exe")
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    exe = Path(a.exe) if a.exe else HERE.parent / "dragons_crown_pro.exe"
    if not exe.exists():
        for c in (HERE / "dragons_crown_pro.exe", Path.cwd() / "dragons_crown_pro.exe"):
            if c.exists():
                exe = c
                break
    if not exe.exists():
        raise SystemExit("找不到 dragons_crown_pro.exe，请用 --exe 指定")
    return do_apply(exe, a.restore, a.check)


if __name__ == "__main__":
    sys.exit(main())
