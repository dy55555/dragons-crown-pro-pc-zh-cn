#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dragon's Crown Pro PC (CUSA10209 1.00 Asia/HK) SAVE0.DAT 存档编辑器

能力
----
* 列出 3 个 DATA slot 的角色、金币、9 套行囊、通用道具箱（物品ID + 名称）
* 修改行囊里任意一格的物品ID/词条/等级/攻击/耐久（只改对应字节 + 重算 CRC32）
* 修改通用道具箱任意格的种类/数量（该 slot 所有角色共用）
* 技能道具只读标注（游戏按词条判定，受加点限制，不开放修改）
* 修改金币 / 等级 / HP / 技能点 / 名字（复用上游 save_data.h 已验证的偏移）
* 全程只读源文件，改写到新文件或原子替换；自动备份；CRC 校验保证游戏能加载

重要：只改「物品ID」这一字段，装备块里的耐久、属性等其余字节原样保留。
      游戏按物品ID查表取名/属性，因此换 ID 即可换装备。

用法
----
    python save_editor.py list                        # 列出全部角色与装备
    python save_editor.py list --slot 0               # 只看 DATA 1
    python save_editor.py set-item --slot 0 --char 0 --bag 1 --slot-idx 0 --id 0x105
    python save_editor.py set-gold --slot 0 --value 99999999
    python save_editor.py --save PATH ...             # 指定存档路径（默认本机正式存档）
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import os
import shutil
import struct
import sys
import zlib
from pathlib import Path

# ---- 上游 save_data.h (v1.4.0) 已验证常量 --------------------------------
SAVE_SIZE = 0x20EB18
CRC_OFFSET = 0x00
MAGIC_OFFSET = 0x04
SAVE_MAGIC = b"DCSD"
DATA_SLOT_COUNT = 3
DATA_SLOT_STRIDE = 0xAF8D4
GOLD_OFFSET = 0xAF288
CHAR_COUNT = 16
CHAR_STRIDE = 0x21D0
CHAR_OFFSET = 0xA400
F_HP_CUR = 0xDC
F_HP_MAX = 0xE0
F_SKILL = 0xF4
F_LEVEL = 0xF6
F_CLASS = 0xF7
F_COLOR = 0xF8
F_NAME = 0x208
F_NAME2 = 0x210
NAME_BYTES = 8
EMPTY_FILL = 0xCCCCCCCC
MAX_GOLD = 99999999

# ---- 本工具逆向出的装备栏常量 -------------------------------------------
BAG_STRIDE = 0x54          # 与上游 kBagItemSize 一致，实测槽间距
BAG_SLOT_COUNT = 10
BAG_OFFSET_MAIN = 0x288    # 角色记录内第一套行囊起点
# 实测：不是「主装备/备用装备」两套，而是最多 9 套行囊配置。
# 每套起点 = 角色基址 + 0x288 + bagIndex*0x354 (bagIndex 0..8)，每套 10 格 × 0x54。
# 0x354 = 10*0x54 + 0x14。旧的 0x5DC 其实只是第 2 套起点 (0x288+0x354)。
BAG_CONFIG_COUNT = 9
BAG_CONFIG_STRIDE = 0x354
ITEM_ID_OFFSET = 0x00      # 槽首 u16 LE = 物品ID
# 实测块首之后 2 字节是「类型标记」(0x0068/0x006a/0x0022/0x0020)，不动
ITEM_ITEMID_MIN = 0x00BB
ITEM_ITEMID_MAX = 0x01E8

# 消耗品槽：物品ID 固定 0x0070，真实种类和数量在下面两个字段
CONSUMABLE_ID = 0x0070
QTY_OFFSET = 0x0C          # u16 低字节 = 数量（实测 x20/x8/x4/x7 全部吻合）
KIND_OFFSET = 0x00         # 消耗品种类ID就是"附加字段1"，见下
# 附加字段（前缀/词条）实际位于物品ID往前 0x40 处，共 8 个 8 字节槽。
# 旧版误读成物品ID往后 0x14，正好读到下一件装备的附加字段，才会两件都显示 5 条。
MOD_BASE_SHIFT = 0x40
MOD_STRIDE = 8
MOD_SLOT_COUNT = 8
MOD_EMPTY = 0xFFFF
MOD_OFFSETS = tuple(-MOD_BASE_SHIFT + i * MOD_STRIDE for i in range(MOD_SLOT_COUNT))
# 物品自身字段（相对物品ID）
F_ITEM_LEVEL = 0x0E        # u8，显示等级 = 存值 + 1（实测 0x22 → LV35）
F_ITEM_ATK_MIN = 0x10      # u8，武器攻击力下限（实测 0x56 = 86）
F_ITEM_ATK_MAX = 0x11      # u8，武器攻击力上限（实测 0x60 = 96）
F_DUR_CUR = 0x04           # u32，耐久 = 存值 / 100（195889 → 1958）
F_DUR_MAX = 0x08
DUR_SCALE = 100
GRADES = "EDCBAS"

# ---- 通用道具箱（每个 DATA slot 一套，该 slot 内所有角色共用）-----------
# 起点 = slot*DATA_SLOT_STRIDE + GOLD_OFFSET + 0x4C = slot*stride + 0xAF2D4
# 20 格 × 0x54，结构与行囊消耗品完全相同：iid=0x70，种类=词条0，数量=+0x0C 低字节
BOX_OFFSET = 0xAF2D4
BOX_SLOT_COUNT = 20

# ---- 技能道具判定（上游 dcp_cheats.h IsSkillItem 官方掩码）--------------
# 第一个词条(种类)ID 落在 0x1D1..0x1F8 且命中掩码 => 该消耗品是「技能」
# 技能是装备上去使用的，受技能点上限约束，本编辑器只读显示、不提供修改。
SKILL_FIRST_EFFECT = 0x1D1
SKILL_EFFECT_COUNT = 0x28
SKILL_EFFECT_MASK = 0xE362E5F7FF

# ---- 人物状态候选字段（只读；上游无静态存档定义，未标定，禁止写入）------
# 职业+等级由游戏查表重算属性(write_stats)，SAVE0.DAT 内没有独立的 STR/VIT/INT/LUC 字段。
# 下面 3 对 u16 随等级增长、含义未最终确认，仅作观察展示，不开放编辑。
STAT_CANDIDATES = (0xE4, 0xE6, 0xE8, 0xEA, 0xEC, 0xEE, 0xF0, 0xF2)

CLASSES = {0: "Fighter/战士", 1: "Amazon/亚马逊", 2: "Elf/精灵",
           3: "Dwarf/矮人", 4: "Sorceress/女巫", 5: "Wizard/法师"}
BAG_NAMES = {i: f"行囊{i + 1}" for i in range(BAG_CONFIG_COUNT)}

DEFAULT_SAVE = Path("D:/GAMES/DragonsCrownPro/user/savedata/1/CUSA10209/SAVEDATA00/SAVE0.DAT")
HERE = Path(__file__).resolve().parent
ITEMS_TSV = HERE / "save_items.tsv"
AFFIX_TSV = HERE / "save_affixes.tsv"
MODS_TSV = HERE / "save_mods.tsv"
MODS_ZH_TSV = HERE / "save_mods.zh.tsv"
CONSUMABLES_TSV = HERE / "save_consumables.tsv"


def load_names(path: Path) -> dict[int, str]:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        try:
            out[int(parts[0], 16)] = parts[1]
        except ValueError:
            pass
    return out


def item_name(item_id: int, names: dict[int, str]) -> str:
    n = names.get(item_id)
    if n:
        return n
    if ITEM_ITEMID_MIN <= item_id <= ITEM_ITEMID_MAX:
        return "(未收录装备)"
    if item_id == 0:
        return "(空)"
    return ""


def mod_names(zh: bool = False) -> dict[int, str]:
    """附加ID -> 名称。默认中文（xlsx 优先，其余按惯用译法直译）。"""
    return load_names(MODS_ZH_TSV if zh else MODS_TSV)


def consumable_names() -> dict[int, str]:
    return load_names(CONSUMABLES_TSV)


def consumable_qty(data, o: int) -> int:
    """消耗品数量：u16 的低字节。实测 x20/x8/x4/x7 与游戏显示一致。"""
    return read_u16(data, o + QTY_OFFSET) & 0xFF


def consumable_kind(data, o: int) -> int:
    """消耗品种类ID就是第一个附加字段。"""
    return read_u16(data, o + MOD_OFFSETS[0])


def active_mods(data, o: int) -> list[tuple[int, int, int, int, float]]:
    """返回 [(槽号, 绝对偏移, 附加ID, flags, 参数float)]，0xFFFF 表示空槽。"""
    out = []
    for idx, off in enumerate(MOD_OFFSETS):
        mo = o + off
        v = read_u16(data, mo)
        if v == MOD_EMPTY:
            continue
        out.append((idx, mo, v, read_u16(data, mo + 2),
                    struct.unpack_from("<f", data, mo + 4)[0]))
    return out


def grade(count: int) -> str:
    """游戏品质由生效附加词条数决定：E=0 D=1 C=2 B=3 A=4 S=5。"""
    return GRADES[min(count, len(GRADES) - 1)]


def item_level(data, o: int) -> int:
    return data[o + F_ITEM_LEVEL] + 1


def item_atk(data, o: int) -> tuple[int, int]:
    return data[o + F_ITEM_ATK_MIN], data[o + F_ITEM_ATK_MAX]


def is_weapon(data, o: int) -> bool:
    """类型标记 0x6800 = 武器，0x6A00 = 防具/饰品。"""
    return read_u16(data, o + 0x02) == 0x6800


def durability(data, o: int) -> tuple[float, float]:
    return (read_u32(data, o + F_DUR_CUR) / DUR_SCALE,
            read_u32(data, o + F_DUR_MAX) / DUR_SCALE)


def consumable_label(data, o: int, cons: dict[int, str] | None = None) -> str:
    kind = consumable_kind(data, o)
    if cons is None:
        cons = consumable_names()
    return cons.get(kind) or f"消耗品(种类{kind:#05x})"


def item_label(data, o: int, names: dict[int, str], cons=None) -> str:
    iid = read_u16(data, o)
    if iid == CONSUMABLE_ID:
        q = consumable_qty(data, o)
        base = consumable_label(data, o, cons)
        return base if q == 0xFF else f"{base} x{q}"
    return item_name(iid, names) or ("" if iid == 0 else "?")


def mod_values(data, o: int) -> list[int]:
    return [read_u16(data, o + off) for off in MOD_OFFSETS]


def crc32(data: bytes) -> int:
    """标准 CRC-32 (IEEE 802.3)，覆盖 [4:EOF)，与上游 SaveCrc32 一致。"""
    return zlib.crc32(data[MAGIC_OFFSET:]) & 0xFFFFFFFF


def read_u16(d, o):
    return d[o] | d[o + 1] << 8


def read_u32(d, o):
    return struct.unpack_from("<I", d, o)[0]


def write_u16(d, o, v):
    d[o] = v & 0xFF
    d[o + 1] = (v >> 8) & 0xFF


def write_u32(d, o, v):
    struct.pack_into("<I", d, o, v & 0xFFFFFFFF)


def validate(data: bytes) -> list[str]:
    errs = []
    if len(data) != SAVE_SIZE:
        errs.append(f"大小 {len(data)} != {SAVE_SIZE}")
    if data[MAGIC_OFFSET:MAGIC_OFFSET + 4] != SAVE_MAGIC:
        errs.append("缺少 DCSD 魔数")
    stored = read_u32(data, CRC_OFFSET)
    computed = crc32(data)
    if stored != computed:
        errs.append(f"CRC 不符: 存 {stored:#010x} 算 {computed:#010x}")
    return errs


def seal(data: bytearray):
    """重算 CRC 写回 0x00。"""
    write_u32(data, CRC_OFFSET, crc32(data))


def char_offset(slot: int, char: int) -> int:
    return slot * DATA_SLOT_STRIDE + CHAR_OFFSET + char * CHAR_STRIDE


def present(data, o) -> bool:
    return (read_u32(data, o) != EMPTY_FILL and data[o + F_NAME] != 0)


def cname(data, o) -> str:
    raw = data[o + F_NAME:o + F_NAME + NAME_BYTES]
    return raw.split(b"\0", 1)[0].decode("ascii", errors="replace")


def bag_offset(char_base: int, bag: int, slot_idx: int) -> int:
    """第 bag 套行囊(bagIndex 0..8) 的第 slot_idx 格绝对偏移。"""
    return char_base + BAG_OFFSET_MAIN + bag * BAG_CONFIG_STRIDE + slot_idx * BAG_STRIDE


def box_offset(slot: int, idx: int) -> int:
    """DATA slot 的通用道具箱第 idx 格绝对偏移。"""
    return slot * DATA_SLOT_STRIDE + BOX_OFFSET + idx * BAG_STRIDE


def is_skill_kind(kind: int) -> bool:
    """种类ID是否为技能道具（上游 IsSkillItem 的判定核心）。"""
    index = kind - SKILL_FIRST_EFFECT
    return 0 <= index < SKILL_EFFECT_COUNT and ((SKILL_EFFECT_MASK >> index) & 1) != 0


def is_skill_item(data, o: int) -> bool:
    return read_u16(data, o) == CONSUMABLE_ID and is_skill_kind(consumable_kind(data, o))


def stat_candidates(data, char_base: int) -> list[int]:
    return [read_u16(data, char_base + off) for off in STAT_CANDIDATES]


def sha256(b) -> str:
    return hashlib.sha256(b).hexdigest()


def cmd_list(args, data, names):
    zh = mod_names(zh=True)
    cons = consumable_names()
    print(f"存档: {args.save}")
    print(f"大小: {len(data):#x}  CRC: {read_u32(data, 0):#010x}  校验: {'OK' if not validate(data) else 'FAIL'}")
    print()
    for slot in range(DATA_SLOT_COUNT):
        if args.slot is not None and slot != args.slot:
            continue
        gold = read_u32(data, slot * DATA_SLOT_STRIDE + GOLD_OFFSET)
        print(f"===== DATA {slot + 1}  金币 {gold} =====")
        for ci in range(CHAR_COUNT):
            o = char_offset(slot, ci)
            if not present(data, o):
                continue
            nm = cname(data, o)
            lvl = data[o + F_LEVEL] + 1
            cls = CLASSES.get(data[o + F_CLASS], str(data[o + F_CLASS]))
            hp = read_u16(data, o + F_HP_CUR)
            maxhp = read_u16(data, o + F_HP_MAX)
            sp = read_u16(data, o + F_SKILL)
            print(f"  角色{ci} {nm:<9} {cls:<16} Lv{lvl:<4} HP {hp}/{maxhp}  技能点 {sp}  @{o:#x}")
            print(f"      状态候选(只读,未标定) {stat_candidates(data, o)}")
            for bag in range(BAG_CONFIG_COUNT):
                items = []
                for si in range(BAG_SLOT_COUNT):
                    bo = bag_offset(o, bag, si)
                    iid = read_u16(data, bo)
                    if not iid:
                        continue
                    if iid == CONSUMABLE_ID:
                        tag = "[技能]" if is_skill_item(data, bo) else ""
                        items.append(f"[{si}]{tag} {item_label(data, bo, names, cons)}@{bo:#x}")
                    else:
                        ms = active_mods(data, bo)
                        items.append(f"[{si}]{grade(len(ms))}级 {item_name(iid, names)}")
                if items:
                    print(f"      === {BAG_NAMES[bag]} ===  " + " | ".join(items))
        # 通用道具箱
        box_items = []
        for bi in range(BOX_SLOT_COUNT):
            bo = box_offset(slot, bi)
            if read_u16(data, bo) != CONSUMABLE_ID:
                continue
            kind = consumable_kind(data, bo)
            tag = "[技能]" if is_skill_kind(kind) else ""
            box_items.append(f"[{bi}]{tag} {consumable_label(data, bo, cons)} x{consumable_qty(data, bo)}")
        print(f"  --- 通用道具箱（该存档所有角色共用，{BOX_SLOT_COUNT}格）---")
        for bi in range(0, BOX_SLOT_COUNT, 2):
            line = "  ".join(box_items[j] for j in (bi, bi + 1) if j < len(box_items))
            if line:
                print("      " + line)
        print()


def atomic_write(path: Path, data: bytes, backup: bool = True):
    errs = validate(data)
    if errs:
        raise SystemExit("拒绝写入，存档校验失败: " + "; ".join(errs))
    if backup and path.exists():
        bak = path.with_name(path.name + ".editor-bak")
        shutil.copy2(path, bak)
        print(f"已备份原存档 -> {bak.name}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    print(f"已写入 {path}")


def cmd_set_item(args, data, names):
    o = bag_offset(char_offset(args.slot, args.char), args.bag, args.slot_idx)
    old = read_u16(data, o)
    if args.id != CONSUMABLE_ID and not (ITEM_ITEMID_MIN <= args.id <= ITEM_ITEMID_MAX):
        raise SystemExit(f"物品ID {args.id:#x} 超出装备范围 {ITEM_ITEMID_MIN:#x}-{ITEM_ITEMID_MAX:#x}")
    print(f"改装备: DATA{args.slot+1} 角色{args.char} {BAG_NAMES[args.bag]}[{args.slot_idx}] @{o:#x}")
    print(f"  {old:#05x} {item_name(old, names)}  ->  {args.id:#05x} {item_name(args.id, names)}")
    buf = bytearray(data)
    write_u16(buf, o, args.id)
    seal(buf)
    if args.dry_run:
        out = Path(args.out) if args.out else Path("SAVE0.modified.DAT")
        out.write_bytes(buf)
        print(f"[演练] 已写出到 {out}（原存档未动） SHA {sha256(buf)}")
    else:
        atomic_write(args.save, bytes(buf), backup=not args.no_backup)
    return 0


def cmd_set_consumable(args, data, names):
    o = bag_offset(char_offset(args.slot, args.char), args.bag, args.slot_idx)
    if read_u16(data, o) != CONSUMABLE_ID:
        raise SystemExit(f"该格不是消耗品（物品ID {read_u16(data, o):#x}）")
    if not (0 <= args.qty <= 255):
        raise SystemExit("数量必须在 0-255")
    qo = o + QTY_OFFSET
    old = read_u16(data, qo)
    new = (old & 0xFF00) | args.qty
    print(f"改数量: DATA{args.slot+1} 角色{args.char} {BAG_NAMES[args.bag]}[{args.slot_idx}] "
          f"@{qo:#x}  {old & 0xFF} -> {args.qty}")
    buf = bytearray(data)
    write_u16(buf, qo, new)
    seal(buf)
    if args.dry_run:
        out = Path(args.out) if args.out else Path("SAVE0.modified.DAT")
        out.write_bytes(buf)
        print(f"[演练] 已写出到 {out}（原存档未动）")
    else:
        atomic_write(args.save, bytes(buf), backup=not args.no_backup)
    return 0


def cmd_set_box(args, data, names):
    """修改通用道具箱某格的种类和/或数量（一次 commit，统一 CRC）。"""
    if not (0 <= args.index < BOX_SLOT_COUNT):
        raise SystemExit(f"道具箱格号必须是 0-{BOX_SLOT_COUNT - 1}")
    o = box_offset(args.slot, args.index)
    buf = bytearray(data)
    if args.kind is not None:
        mo = o + MOD_OFFSETS[0]      # 种类 = 词条槽0
        print(f"改道具箱种类: DATA{args.slot+1}[{args.index}] @{mo:#x}  "
              f"{read_u16(data, mo):#06x} -> {args.kind:#06x}")
        write_u16(buf, mo, args.kind)
    if args.qty is not None:
        if not (0 <= args.qty <= 255):
            raise SystemExit("数量必须在 0-255")
        qo = o + QTY_OFFSET
        oldq = read_u16(data, qo) & 0xFF
        print(f"改道具箱数量: DATA{args.slot+1}[{args.index}] @{qo:#x}  {oldq} -> {args.qty}")
        write_u16(buf, qo, (read_u16(data, qo) & 0xFF00) | args.qty)
    if args.kind is None and args.qty is None:
        raise SystemExit("至少要给 --kind 或 --qty 之一")
    seal(buf)
    if args.dry_run:
        out = Path(args.out) if args.out else Path("SAVE0.modified.DAT")
        out.write_bytes(buf)
        print(f"[演练] 已写出到 {out}（原存档未动）")
    else:
        atomic_write(args.save, bytes(buf), backup=not args.no_backup)
    return 0


def cmd_set_mod(args, data, names):
    o = bag_offset(char_offset(args.slot, args.char), args.bag, args.slot_idx)
    if not (0 <= args.index < MOD_SLOT_COUNT):
        raise SystemExit(f"附加字段序号必须是 0-{MOD_SLOT_COUNT-1}")
    mo = o + MOD_OFFSETS[args.index]
    old = read_u16(data, mo)
    mods = mod_names(zh=True)
    before = grade(len(active_mods(data, o)))
    after_n = len(active_mods(data, o))
    if old == MOD_EMPTY and args.id != MOD_EMPTY:
        after_n += 1
    elif old != MOD_EMPTY and args.id == MOD_EMPTY:
        after_n -= 1
    print(f"改附加ID: DATA{args.slot+1} 角色{args.char} {BAG_NAMES[args.bag]}[{args.slot_idx}] "
          f"第{args.index+1}项 @{mo:#x}  {old:#06x} {mods.get(old,'')} -> "
          f"{args.id:#06x} {mods.get(args.id,'')}  品质 {before} -> {grade(after_n)}")
    buf = bytearray(data)
    write_u16(buf, mo, args.id)
    seal(buf)
    if args.dry_run:
        out = Path(args.out) if args.out else Path("SAVE0.modified.DAT")
        out.write_bytes(buf)
        print(f"[演练] 已写出到 {out}（原存档未动）")
    else:
        atomic_write(args.save, bytes(buf), backup=not args.no_backup)
    return 0


def cmd_set_stat(args, data, names):
    o = bag_offset(char_offset(args.slot, args.char), args.bag, args.slot_idx)
    if read_u16(data, o) == CONSUMABLE_ID:
        raise SystemExit("消耗品没有等级/攻击力/耐久字段")
    kind = args.field
    fields = {
        "level": (o + F_ITEM_LEVEL, 1), "atkmin": (o + F_ITEM_ATK_MIN, 1),
        "atkmax": (o + F_ITEM_ATK_MAX, 1), "durcur": (o + F_DUR_CUR, 4),
        "durmax": (o + F_DUR_MAX, 4),
    }
    fo, width = fields[kind]
    v = args.value
    if kind == "level":
        if not (1 <= v <= 255):
            raise SystemExit("等级必须 1-255")
        v -= 1
    elif kind in ("atkmin", "atkmax"):
        if not (0 <= v <= 255):
            raise SystemExit("攻击力必须 0-255")
    else:
        if v < 0:
            raise SystemExit("耐久不能为负")
        frac = read_u32(data, fo) % DUR_SCALE
        v = v * DUR_SCALE + frac
    print(f"改 {kind}: DATA{args.slot+1} 角色{args.char} {BAG_NAMES[args.bag]}[{args.slot_idx}] "
          f"@{fo:#x} -> {args.value}")
    buf = bytearray(data)
    if width == 1:
        buf[fo] = v & 0xFF
    else:
        write_u32(buf, fo, v)
    seal(buf)
    if args.dry_run:
        out = Path(args.out) if args.out else Path("SAVE0.modified.DAT")
        out.write_bytes(buf)
        print(f"[演练] 已写出到 {out}（原存档未动）")
    else:
        atomic_write(args.save, bytes(buf), backup=not args.no_backup)
    return 0


def cmd_set_gold(args, data, names):
    if not (0 <= args.value <= MAX_GOLD):
        raise SystemExit(f"金币 {args.value} 超出 0-{MAX_GOLD}")
    o = args.slot * DATA_SLOT_STRIDE + GOLD_OFFSET
    old = read_u32(data, o)
    print(f"改金币: DATA{args.slot+1} @{o:#x}  {old} -> {args.value}")
    buf = bytearray(data)
    write_u32(buf, o, args.value)
    seal(buf)
    if args.dry_run:
        out = Path(args.out) if args.out else Path("SAVE0.modified.DAT")
        out.write_bytes(buf)
        print(f"[演练] 已写出到 {out}（原存档未动）")
    else:
        atomic_write(args.save, bytes(buf), backup=not args.no_backup)
    return 0


def main():
    ap = argparse.ArgumentParser(description="龙之皇冠 Pro PC 存档编辑器 (SAVE0.DAT)")
    ap.add_argument("--save", default=str(DEFAULT_SAVE), help="存档路径")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="列出角色与装备")
    p.add_argument("--slot", type=int, help="只看某个 DATA slot (0-based)")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("set-item", help="改装备栏物品ID")
    p.add_argument("--slot", type=int, required=True, help="DATA slot 0-2")
    p.add_argument("--char", type=int, required=True, help="角色 0-15")
    p.add_argument("--bag", type=int, required=True, choices=list(range(BAG_CONFIG_COUNT)), help="行囊 0-8（对应行囊1-9）")
    p.add_argument("--slot-idx", type=int, required=True, help="装备格 0-9")
    p.add_argument("--id", type=lambda s: int(s, 0), required=True, help="新物品ID, 如 0x105")
    p.add_argument("--out", help="演练输出路径")
    p.add_argument("--dry-run", action="store_true", help="只写副本，不改原存档")
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_set_item)

    p = sub.add_parser("set-qty", help="改消耗品数量（该格必须是 0x0070）")
    p.add_argument("--slot", type=int, required=True)
    p.add_argument("--char", type=int, required=True)
    p.add_argument("--bag", type=int, required=True, choices=list(range(BAG_CONFIG_COUNT)), help="行囊 0-8")
    p.add_argument("--slot-idx", type=int, required=True)
    p.add_argument("--qty", type=int, required=True, help="0-255")
    p.add_argument("--out")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_set_consumable)

    p = sub.add_parser("set-mod", help="改装备第 N 个附加ID（0-4）")
    p.add_argument("--slot", type=int, required=True)
    p.add_argument("--char", type=int, required=True)
    p.add_argument("--bag", type=int, required=True, choices=list(range(BAG_CONFIG_COUNT)), help="行囊 0-8")
    p.add_argument("--slot-idx", type=int, required=True)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--id", type=lambda s: int(s, 0), required=True)
    p.add_argument("--out")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_set_mod)

    p = sub.add_parser("set-stat", help="改装备等级/攻击力/耐久")
    p.add_argument("--slot", type=int, required=True)
    p.add_argument("--char", type=int, required=True)
    p.add_argument("--bag", type=int, required=True, choices=list(range(BAG_CONFIG_COUNT)), help="行囊 0-8")
    p.add_argument("--slot-idx", type=int, required=True)
    p.add_argument("--field", required=True,
                   choices=["level", "atkmin", "atkmax", "durcur", "durmax"])
    p.add_argument("--value", type=int, required=True)
    p.add_argument("--out")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_set_stat)

    p = sub.add_parser("set-gold", help="改金币")
    p.add_argument("--slot", type=int, required=True)
    p.add_argument("--value", type=int, required=True)
    p.add_argument("--out", help="演练输出路径")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_set_gold)

    p = sub.add_parser("set-box", help="改通用道具箱某格（种类ID/数量）")
    p.add_argument("--slot", type=int, required=True, help="DATA slot 0-2")
    p.add_argument("--index", type=int, required=True, help=f"道具箱格 0-{BOX_SLOT_COUNT-1}")
    p.add_argument("--kind", type=lambda s: int(s, 0), help="消耗品种类ID, 如 0x190")
    p.add_argument("--qty", type=int, help="数量 0-255")
    p.add_argument("--out")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_set_box)

    args = ap.parse_args()
    save = Path(args.save)
    if not save.exists():
        raise SystemExit(f"找不到存档: {save}")
    data = save.read_bytes()
    errs = validate(data)
    if errs:
        raise SystemExit("存档校验失败，拒绝操作: " + "; ".join(errs))
    names = load_names(ITEMS_TSV)
    return args.func(args, data, names) or 0


if __name__ == "__main__":
    sys.exit(main())
