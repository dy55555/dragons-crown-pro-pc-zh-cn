#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dragon's Crown Pro PC (CUSA10209 1.00 Asia/HK) SAVE0.DAT 存档编辑器

能力
----
* 列出 3 个 DATA slot 的角色、金币、装备栏（物品ID + 名称）
* 修改装备栏里任意一格的物品ID（只改 2 字节 + 重算 CRC32）
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
BAG_OFFSET_MAIN = 0x288    # 角色记录内第一组装备栏起点
BAG_OFFSET_ALT = 0x5DC     # 第二组（备用/套装）起点：0x630 是旧的错误值，会漏掉第一格
ITEM_ID_OFFSET = 0x00      # 槽首 u16 LE = 物品ID
# 实测块首之后 2 字节是「类型标记」(0x0068/0x006a/0x0022/0x0020)，不动
ITEM_ITEMID_MIN = 0x00BB
ITEM_ITEMID_MAX = 0x01E8

# 消耗品槽：物品ID 固定 0x0070，真实种类和数量在下面两个字段
CONSUMABLE_ID = 0x0070
QTY_OFFSET = 0x0C          # u16 低字节 = 数量（实测 x20/x8/x4/x7 全部吻合）
KIND_OFFSET = 0x14         # u16 = 上游 CustomItem 索引，区分是哪一种消耗品
# 装备块里 5 个附加ID（上游 CustomItem 索引），游戏按这些显示前缀/附加效果
MOD_OFFSETS = (0x14, 0x1C, 0x24, 0x2C, 0x34)

CLASSES = {0: "Fighter/战士", 1: "Amazon/亚马逊", 2: "Elf/精灵",
           3: "Dwarf/矮人", 4: "Sorceress/女巫", 5: "Wizard/法师"}
BAG_NAMES = {0: "主装备", 1: "备用装备"}

DEFAULT_SAVE = Path("D:/GAMES/DragonsCrownPro/user/savedata/1/CUSA10209/SAVEDATA00/SAVE0.DAT")
HERE = Path(__file__).resolve().parent
ITEMS_TSV = HERE / "save_items.tsv"
AFFIX_TSV = HERE / "save_affixes.tsv"
MODS_TSV = HERE / "save_mods.tsv"


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


def mod_names() -> dict[int, str]:
    """上游 CustomItem 表：附加ID -> 英文原名（中文名需游戏内核实）。"""
    return load_names(MODS_TSV)


def consumable_qty(data, o: int) -> int:
    """消耗品数量：u16 的低字节。实测 x20/x8/x4/x7 与游戏显示一致。"""
    return read_u16(data, o + QTY_OFFSET) & 0xFF


def consumable_kind(data, o: int) -> int:
    return read_u16(data, o + KIND_OFFSET)


def item_label(data, o: int, names: dict[int, str]) -> str:
    iid = read_u16(data, o)
    if iid == CONSUMABLE_ID:
        q = consumable_qty(data, o)
        return "特殊物品(非堆叠)" if q == 0xFF else f"消耗品 x{q}"
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
    base = BAG_OFFSET_MAIN if bag == 0 else BAG_OFFSET_ALT
    return char_base + base + slot_idx * BAG_STRIDE


def sha256(b) -> str:
    return hashlib.sha256(b).hexdigest()


def cmd_list(args, data, names):
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
            for bag in (0, 1):
                items = []
                for si in range(BAG_SLOT_COUNT):
                    bo = bag_offset(o, bag, si)
                    iid = read_u16(data, bo)
                    if iid:
                        lab = item_label(data, bo, names)
                        if iid != CONSUMABLE_ID:
                            mv = [v for v in mod_values(data, bo) if v != 0xFFFF]
                            if mv:
                                lab += "  附加:" + "/".join(f"{v:#05x}" for v in mv)
                        items.append(f"[{si}]{iid:#05x} {lab}")
                print(f"      {BAG_NAMES[bag]}: " + " | ".join(items) if items else f"      {BAG_NAMES[bag]}: (空)")
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


def cmd_set_mod(args, data, names):
    o = bag_offset(char_offset(args.slot, args.char), args.bag, args.slot_idx)
    if not (0 <= args.index < len(MOD_OFFSETS)):
        raise SystemExit(f"附加字段序号必须是 0-{len(MOD_OFFSETS)-1}")
    mo = o + MOD_OFFSETS[args.index]
    old = read_u16(data, mo)
    mods = mod_names()
    print(f"改附加ID: DATA{args.slot+1} 角色{args.char} {BAG_NAMES[args.bag]}[{args.slot_idx}] "
          f"第{args.index+1}项 @{mo:#x}  {old:#06x} {mods.get(old,'')} -> "
          f"{args.id:#06x} {mods.get(args.id,'')}")
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
    p.add_argument("--bag", type=int, required=True, choices=[0, 1], help="0=主装备 1=备用")
    p.add_argument("--slot-idx", type=int, required=True, help="装备格 0-9")
    p.add_argument("--id", type=lambda s: int(s, 0), required=True, help="新物品ID, 如 0x105")
    p.add_argument("--out", help="演练输出路径")
    p.add_argument("--dry-run", action="store_true", help="只写副本，不改原存档")
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_set_item)

    p = sub.add_parser("set-qty", help="改消耗品数量（该格必须是 0x0070）")
    p.add_argument("--slot", type=int, required=True)
    p.add_argument("--char", type=int, required=True)
    p.add_argument("--bag", type=int, required=True, choices=[0, 1])
    p.add_argument("--slot-idx", type=int, required=True)
    p.add_argument("--qty", type=int, required=True, help="0-255")
    p.add_argument("--out")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_set_consumable)

    p = sub.add_parser("set-mod", help="改装备第 N 个附加ID（0-4）")
    p.add_argument("--slot", type=int, required=True)
    p.add_argument("--char", type=int, required=True)
    p.add_argument("--bag", type=int, required=True, choices=[0, 1])
    p.add_argument("--slot-idx", type=int, required=True)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--id", type=lambda s: int(s, 0), required=True)
    p.add_argument("--out")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_set_mod)

    p = sub.add_parser("set-gold", help="改金币")
    p.add_argument("--slot", type=int, required=True)
    p.add_argument("--value", type=int, required=True)
    p.add_argument("--out", help="演练输出路径")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_set_gold)

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
