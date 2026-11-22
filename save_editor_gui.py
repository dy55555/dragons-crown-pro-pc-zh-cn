#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""龙之皇冠 Pro PC 存档编辑器 —— 图形界面（tkinter，纯标准库）

双击「存档编辑器.bat」启动。功能：
  * 列出 3 个 DATA slot 的角色、金币、两套装备栏（20 格）
  * 下拉选物品ID 即可换装备；改金币/等级/HP/技能点
  * 保存时自动备份原档、重算 CRC32、校验后再写盘
  * 只改对应字段的字节，其余字节（耐久/词条/外观）原样保留

只针对 CUSA10209 1.00 Asia/Hong Kong 的 SAVE0.DAT。
"""
from __future__ import annotations

import hashlib
import struct
import sys
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path

import save_editor as se


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("龙之皇冠 Pro PC · 存档编辑器 (SAVE0.DAT)")
        self.root.geometry("1180x720")
        self.path = tk.StringVar(value=str(se.DEFAULT_SAVE))
        self.data = None
        self.names = se.load_names(se.ITEMS_TSV)
        self.mods = se.mod_names(zh=True)
        self.cons = se.consumable_names()
        self.item_list = sorted(self.names.items())
        self.item_choices = [f"{v:04X}  {n}" for v, n in self.item_list]
        self.choice_to_id = {f"{v:04X}  {n}": v for v, n in self.item_list}
        self.edits = {}          # (byte_offset) -> new_value
        self._build()
        self.open_save(initial=True)

    # ---------- UI ----------
    def _build(self):
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="存档文件:").pack(side="left")
        ttk.Entry(top, textvariable=self.path, width=88).pack(side="left", padx=5)
        ttk.Button(top, text="打开", command=self.open_save).pack(side="left", padx=2)
        ttk.Button(top, text="重新载入", command=lambda: self.open_save(initial=True)).pack(side="left", padx=2)

        bar = ttk.Frame(self.root, padding=(8, 0))
        bar.pack(fill="x")
        self.status = ttk.Label(bar, text="", foreground="#0a6")
        self.status.pack(side="left")
        ttk.Button(bar, text="还原备份 (.editor-bak)", command=self.restore).pack(side="right")

        # 主体：左侧角色树 + 右侧详情
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        left = ttk.Frame(main)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ttk.Label(left, text="存档结构").pack(anchor="w")
        self.tree = ttk.Treeview(left, show="tree", height=28)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        right = ttk.Frame(main)
        right.grid(row=0, column=1, sticky="nsew")
        self.detail = ttk.LabelFrame(right, text="选中项详情", padding=10)
        self.detail.pack(fill="both", expand=True)
        # 可滚动容器：内容多（如道具箱列表）时可上下滚动，翻页栏不会被挤出视野
        self.detail_canvas = tk.Canvas(self.detail, highlightthickness=0, bd=0)
        self.detail_scroll = ttk.Scrollbar(self.detail, orient="vertical",
                                           command=self.detail_canvas.yview)
        self.detail_canvas.pack(side="left", fill="both", expand=True)
        self.detail_scroll.pack(side="right", fill="y")
        self.detail_canvas.configure(yscrollcommand=self.detail_scroll.set)
        self.detail_inner = ttk.Frame(self.detail_canvas)
        self._inner_win = self.detail_canvas.create_window((0, 0), window=self.detail_inner,
                                                           anchor="nw")
        self.detail_inner.bind("<Configure>", self._on_detail_configure)
        # 鼠标滚轮（Windows / Linux）
        self.detail_canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self.detail_canvas.bind_all("<Button-4>", lambda e: self.detail_canvas.yview_scroll(-1, "units"))
        self.detail_canvas.bind_all("<Button-5>", lambda e: self.detail_canvas.yview_scroll(1, "units"))

        bot = ttk.Frame(self.root, padding=8)
        bot.pack(fill="x")
        self.changes = ttk.Label(bot, text="改动: 0", foreground="#b30")
        self.changes.pack(side="left")
        ttk.Button(bot, text="保存改动", command=self.save).pack(side="right")

    def _on_detail_configure(self, _evt=None):
        # 内容尺寸变化时更新滚动范围，并让内部框架宽度跟随画布
        self.detail_canvas.update_idletasks()
        self.detail_canvas.configure(scrollregion=self.detail_canvas.bbox("all"))
        w = self.detail_canvas.winfo_width()
        if w > 1:
            self.detail_canvas.itemconfigure(self._inner_win, width=w)

    def _on_mousewheel(self, event):
        self.detail_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    # ---------- 数据 ----------
    def open_save(self, initial=False):
        p = Path(self.path.get())
        if not p.exists():
            messagebox.showerror("错误", f"找不到存档:\n{p}")
            return
        d = p.read_bytes()
        errs = se.validate(d)
        if errs:
            messagebox.showerror("存档校验失败", "\n".join(errs) + "\n\n已拒绝载入，原文件未改动。")
            return
        self.data = d
        self.edits.clear()
        self.refresh_tree()
        self.status.config(text=f"已载入  {len(d):#x} 字节  CRC {se.read_u32(d,0):#010x}  [校验通过]")
        self.update_changes()

    def refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.node_tag = {}   # node id -> ("item"|"char"|"bag"|"slot", ...)
        for slot in range(se.DATA_SLOT_COUNT):
            gold = self.effective_u32(slot * se.DATA_SLOT_STRIDE + se.GOLD_OFFSET)
            snode = self.tree.insert("", "end", text=f"DATA {slot+1}   金币 {gold}",
                                     open=(slot == 0))
            self.node_tag[snode] = ("slot", slot)
            for ci in range(se.CHAR_COUNT):
                o = se.char_offset(slot, ci)
                if not se.present(self.data, o):
                    continue
                nm = se.cname(self.data, o)
                lvl = self.effective_u8(o + se.F_LEVEL) + 1
                cls = se.CLASSES.get(self.effective_u8(o + se.F_CLASS), "?")
                cnode = self.tree.insert(snode, "end", text=f"  {nm}  {cls}  Lv{lvl}")
                self.node_tag[cnode] = ("char", slot, ci)
                for bag in range(se.BAG_CONFIG_COUNT):
                    cnt = sum(1 for si in range(se.BAG_SLOT_COUNT)
                              if self.effective_u16(se.bag_offset(o, bag, si)))
                    bnode = self.tree.insert(cnode, "end",
                                             text=f"    {se.BAG_NAMES[bag]}  ({cnt}件)",
                                             open=(bag == 0))
                    self.node_tag[bnode] = ("bag", slot, ci, bag)
                    for si in range(se.BAG_SLOT_COUNT):
                        bo = se.bag_offset(o, bag, si)
                        iid = self.effective_u16(bo)
                        if iid == se.CONSUMABLE_ID:
                            q = self.effective_u16(bo + se.QTY_OFFSET) & 0xFF
                            base = se.consumable_label(self.data, bo, self.cons)
                            txt = base if q == 0xFF else f"{base} x{q}"
                            if se.is_skill_item(self.data, bo):
                                txt = "[技能] " + txt
                        else:
                            ms = se.active_mods(self.data, bo)
                            txt = se.item_name(iid, self.names) or ("" if iid == 0 else "?")
                            if iid:
                                txt = f"{se.grade(len(ms))}级 {txt}"
                        inode = self.tree.insert(bnode, "end",
                                                 text=f"      [{si}] {iid:#05x} {txt}")
                        self.node_tag[inode] = ("item", slot, ci, bag, si)
            # 通用道具箱节点（该 DATA slot 所有角色共用）
            boxcnt = sum(1 for bi in range(se.BOX_SLOT_COUNT)
                         if se.box_used(self.data, se.box_offset(slot, bi)))
            xnode = self.tree.insert(snode, "end",
                                     text=f"  [通用道具箱]  {boxcnt}/{se.BOX_SLOT_COUNT}格（所有角色共用）")
            self.node_tag[xnode] = ("box", slot)

    # ---------- 有效值（含未保存改动）----------
    # edits[offset] = (value, width)，width ∈ {1,2,4}
    def effective(self, o, default):
        e = self.edits.get(o)
        return e[0] if e else default

    def effective_u16(self, o):
        return self.effective(o, se.read_u16(self.data, o))

    def effective_u8(self, o):
        return self.effective(o, self.data[o])

    def effective_u32(self, o):
        return self.effective(o, se.read_u32(self.data, o))

    def update_changes(self):
        n = len(self.edits)
        self.changes.config(text=f"改动: {n} 处字段" + ("（保存时自动重算 CRC）" if n else ""))

    # ---------- 详情面板 ----------
    def on_select(self, _evt):
        sel = self.tree.selection()
        if not sel:
            return
        tag = self.node_tag.get(sel[0])
        if not tag:
            return
        for w in list(self.detail_inner.winfo_children()):
            w.destroy()
        if tag[0] == "item":
            self.show_item(*tag[1:])
        elif tag[0] == "char":
            self.show_char(*tag[1:])
        elif tag[0] == "slot":
            self.show_slot(tag[1])
        elif tag[0] == "bag":
            self.show_bag(sel[0], *tag[1:])
        elif tag[0] == "box":
            self.show_box(tag[1])
        else:
            ttk.Label(self.detail_inner, text="选择左侧任一角色或装备格进行编辑",
                      foreground="#666").pack(anchor="w")
        self.detail_canvas.yview_moveto(0)   # 切换项后滚回顶部
        self._on_detail_configure()

    def select_item(self, node):
        self.tree.selection_set(node)
        self.tree.focus(node)
        self.tree.see(node)
        self.on_select(None)

    def show_bag(self, node, slot, ci, bag):
        ttk.Label(self.detail_inner,
                  text=f"DATA {slot+1} / 角色{ci} / {se.BAG_NAMES[bag]} —— 整栏一览",
                  font=("Tahoma", 11, "bold")).pack(anchor="w", pady=(0, 6))
        ttk.Label(self.detail_inner,
                  text="点每行的【编辑】即可选中该格并修改；也可直接双击左侧具体格子。",
                  foreground="#666", wraplength=740, justify="left").pack(anchor="w", pady=(0, 10))
        for si, child in enumerate(self.tree.get_children(node)):
            bo = se.bag_offset(se.char_offset(slot, ci), bag, si)
            iid = self.effective_u16(bo)
            if iid == se.CONSUMABLE_ID:
                q = self.effective_u16(bo + se.QTY_OFFSET) & 0xFF
                base = se.consumable_label(self.data, bo, self.cons)
                lab = base if q == 0xFF else f"{base} x{q}"
            else:
                ms = se.active_mods(self.data, bo)
                lab = se.item_name(iid, self.names) or ("(空)" if iid == 0 else "?")
                if iid:
                    lab = f"{se.grade(len(ms))}级 {lab}"
            row = ttk.Frame(self.detail_inner)
            row.pack(anchor="w", pady=2, fill="x")
            ttk.Label(row, text=f"[{si}] {iid:#05x}  {lab}", width=34).pack(side="left")
            ttk.Button(row, text="编辑", command=lambda c=child: self.select_item(c)).pack(side="left")

    def show_item(self, slot, ci, bag, si):
        o = se.bag_offset(se.char_offset(slot, ci), bag, si)
        cur = self.effective_u16(o)
        ttk.Label(self.detail_inner, text=f"DATA {slot+1} / 角色{ci} / {se.BAG_NAMES[bag]} / 格{si}",
                  font=("Tahoma", 11, "bold")).pack(anchor="w", pady=(0, 8))
        ttk.Label(self.detail_inner, text=f"文件偏移: {o:#x}    字段: u16 小端 = 物品ID").pack(anchor="w")
        row = ttk.Frame(self.detail_inner)
        row.pack(anchor="w", pady=10)
        ttk.Label(row, text="物品:").pack(side="left", padx=(0, 6))
        combo = ttk.Combobox(row, values=self.item_choices, width=58, state="readonly")
        key = f"{cur:04X}  {se.item_name(cur, self.names)}"
        if key in self.choice_to_id:
            combo.set(key)
        combo.pack(side="left")
        hint = ttk.Label(self.detail_inner, text="", foreground="#666", wraplength=760, justify="left")
        hint.pack(anchor="w", pady=4)

        def on_combo(_e):
            v = self.choice_to_id.get(combo.get())
            if v is None:
                return
            if v == 0:
                self.edits.pop(o, None)
            else:
                self.edits[o] = (v, 2)   # 物品ID = u16
            hint.config(text=f"将改成 {v:#06x}  {self.names.get(v, '')}")
            self.refresh_tree()
            self.update_changes()

        combo.bind("<<ComboboxSelected>>", on_combo)

        if cur == se.CONSUMABLE_ID:
            ttk.Label(self.detail_inner,
                      text=f"消耗品：{se.consumable_label(self.data, o, self.cons)}  "
                           f"（种类ID {se.consumable_kind(self.data, o):#06x}）",
                      font=("Tahoma", 10, "bold")).pack(anchor="w", pady=(10, 4))
            qo = o + se.QTY_OFFSET
            rawq = self.effective_u16(qo)
            qrow = ttk.Frame(self.detail_inner)
            qrow.pack(anchor="w")
            ttk.Label(qrow, text="数量 (0-255)", width=16).pack(side="left")
            qvar = tk.StringVar(value=str(rawq & 0xFF))
            ttk.Entry(qrow, textvariable=qvar, width=8).pack(side="left")
            ttk.Label(qrow, text=f"@{qo:#x}  u16 低字节", foreground="#888").pack(side="left", padx=6)

            def apply_qty():
                try:
                    q = int(qvar.get())
                except ValueError:
                    return
                if not (0 <= q <= 255):
                    messagebox.showwarning("超出范围", "数量必须在 0-255 之间")
                    return
                self.edits[qo] = ((rawq & 0xFF00) | q, 2)
                self.refresh_tree()
                self.update_changes()
            ttk.Button(qrow, text="应用", command=apply_qty).pack(side="left", padx=4)
            ttk.Label(self.detail_inner,
                      text="种类由词条1决定，已按实机截图命名；数量改低字节即可。",
                      foreground="#666", wraplength=760, justify="left").pack(anchor="w", pady=4)
        else:
            ms = se.active_mods(self.data, o)
            lo, hi = se.item_atk(self.data, o)
            dcur, dmax = se.durability(self.data, o)
            ttk.Label(self.detail_inner,
                      text=f"品质 {se.grade(len(ms))} —— 由 {len(ms)} 条生效词条决定（E0 D1 C2 B3 A4 S5）",
                      font=("Tahoma", 10, "bold")).pack(anchor="w", pady=(8, 2))
            ttk.Label(self.detail_inner,
                      text=f"{'攻击力' if se.is_weapon(self.data, o) else '数值'} {lo}-{hi}    "
                           f"耐久 {int(dcur)}/{int(dmax)}    "
                           f"装备等级 LV{se.item_level(self.data, o)}",
                      foreground="#444").pack(anchor="w")

            def num_field(label, off, width, get, store, note=""):
                row = ttk.Frame(self.detail_inner)
                row.pack(anchor="w", pady=2, fill="x")
                ttk.Label(row, text=label, width=14).pack(side="left")
                var = tk.StringVar(value=str(get()))
                ttk.Entry(row, textvariable=var, width=8).pack(side="left")
                ttk.Label(row, text=note or f"@{off:#x}", foreground="#888").pack(side="left", padx=6)

                def apply(_e=None, off=off, width=width, var=var, store=store):
                    try:
                        v = int(var.get())
                    except ValueError:
                        return
                    self.edits[off] = (store(v), width)
                    self.refresh_tree()
                    self.update_changes()
                ttk.Button(row, text="应用", command=apply).pack(side="left", padx=4)

            num_field("装备等级", o + se.F_ITEM_LEVEL, 1,
                      lambda: se.item_level(self.data, o),
                      lambda v: max(0, min(254, v - 1)), "显示等级=存值+1")
            num_field("攻击下限", o + se.F_ITEM_ATK_MIN, 1,
                      lambda: se.item_atk(self.data, o)[0],
                      lambda v: max(0, min(255, v)))
            num_field("攻击上限", o + se.F_ITEM_ATK_MAX, 1,
                      lambda: se.item_atk(self.data, o)[1],
                      lambda v: max(0, min(255, v)))
            num_field("耐久当前", o + se.F_DUR_CUR, 4,
                      lambda: int(se.durability(self.data, o)[0]),
                      lambda v: v * se.DUR_SCALE + se.read_u32(self.data, o + se.F_DUR_CUR) % se.DUR_SCALE,
                      "存值=显示×100")
            num_field("耐久最大", o + se.F_DUR_MAX, 4,
                      lambda: int(se.durability(self.data, o)[1]),
                      lambda v: v * se.DUR_SCALE + se.read_u32(self.data, o + se.F_DUR_MAX) % se.DUR_SCALE,
                      "存值=显示×100")

            ttk.Label(self.detail_inner, text="附加词条 1-8（填 FFFF 即清空该条）",
                      font=("Tahoma", 10, "bold")).pack(anchor="w", pady=(12, 4))
            for idx, off in enumerate(se.MOD_OFFSETS):
                mo = o + off
                v = self.effective_u16(mo)
                mrow = ttk.Frame(self.detail_inner)
                mrow.pack(anchor="w", pady=2, fill="x")
                ttk.Label(mrow, text=f"词条{idx+1}", width=9).pack(side="left")
                var = tk.StringVar(value=f"{v:04X}")
                ttk.Entry(mrow, textvariable=var, width=8).pack(side="left")
                ttk.Label(mrow, text=self.mods.get(v, ""), width=42,
                          foreground="#666").pack(side="left", padx=6)
                ttk.Label(mrow, text=f"@{mo:#x}", foreground="#aaa").pack(side="left")

                def apply_mod(_e=None, mo=mo, var=var):
                    try:
                        nv = int(var.get(), 16) & 0xFFFF
                    except ValueError:
                        return
                    self.edits[mo] = (nv, 2)
                    self.refresh_tree()
                    self.update_changes()
                ttk.Button(mrow, text="应用", command=apply_mod).pack(side="left", padx=4)
            ttk.Label(self.detail_inner,
                      text="说明：中文名按 xlsx/日式游戏惯用译法直译，数值可能与本版本略有出入，"
                           "意思对得上就行；以后发现差太多再逐条改。",
                      foreground="#666", wraplength=760, justify="left").pack(anchor="w", pady=(10, 0))

    def show_char(self, slot, ci):
        o = se.char_offset(slot, ci)
        ttk.Label(self.detail_inner, text=f"DATA {slot+1} / 角色{ci}  {se.cname(self.data, o)}",
                  font=("Tahoma", 11, "bold")).pack(anchor="w", pady=(0, 10))
        fields = [
            ("等级 (1-255)", o + se.F_LEVEL, "u8", lambda v: v + 1, lambda v: max(0, min(254, v - 1))),
            ("HP 当前", o + se.F_HP_CUR, "u16", None, None),
            ("HP 最大", o + se.F_HP_MAX, "u16", None, None),
            ("技能点", o + se.F_SKILL, "u16", None, None),
        ]
        for label, off, kind, show, store in fields:
            row = ttk.Frame(self.detail_inner)
            row.pack(anchor="w", pady=4)
            ttk.Label(row, text=label, width=16).pack(side="left")
            raw = self.effective_u16(off) if kind == "u16" else self.effective_u8(off)
            val = show(raw) if show else raw
            var = tk.StringVar(value=str(val))
            ent = ttk.Entry(row, textvariable=var, width=10)
            ent.pack(side="left")
            ttk.Label(row, text=f"@{off:#x}", foreground="#888").pack(side="left", padx=6)

            def apply(_e=None, off=off, kind=kind, var=var, store=store):
                try:
                    v = int(var.get())
                except ValueError:
                    return
                if kind == "u16":
                    v = max(0, min(65535, v))
                    self.edits[off] = (v, 2)
                else:
                    v = max(0, min(255, store(v) if store else v))
                    self.edits[off] = (v, 1)   # 等级 = u8
                self.refresh_tree()
                self.update_changes()
            ent.bind("<Return>", apply)
            ttk.Button(row, text="应用", command=apply).pack(side="left", padx=4)

        # 人物状态候选：只读观察，未标定
        ttk.Label(self.detail_inner, text="人物状态候选值（只读）",
                  font=("Tahoma", 10, "bold")).pack(anchor="w", pady=(12, 4))
        ttk.Label(self.detail_inner,
                  text="游戏属性由「职业+等级」查表重算，存档内没有独立的 力量/体质/智力/幸运 字段。"
                       "下面 8 个 u16 是观察到的候选值，含义未最终确认，故不开放修改。",
                  foreground="#666", wraplength=740, justify="left").pack(anchor="w")
        vals = se.stat_candidates(self.data, o)
        srow = ttk.Frame(self.detail_inner)
        srow.pack(anchor="w", pady=4, fill="x")
        ttk.Label(srow, text="  偏移:").pack(side="left")
        ttk.Label(srow, text=" ".join(f"+{off:#x}" for off in se.STAT_CANDIDATES),
                  foreground="#888", width=54).pack(side="left")
        vrow = ttk.Frame(self.detail_inner)
        vrow.pack(anchor="w", pady=2, fill="x")
        ttk.Label(vrow, text="  数值:").pack(side="left")
        ttk.Label(vrow, text=" ".join(f"{v:>4}" for v in vals), foreground="#333",
                  font=("Consolas", 10), width=54).pack(side="left")

        # 技能：只读提示
        ttk.Label(self.detail_inner, text="技能列表",
                  font=("Tahoma", 10, "bold")).pack(anchor="w", pady=(12, 4))
        ttk.Label(self.detail_inner,
                  text="技能是装备到行囊里的道具（种类ID 0x1D1–0x1F8，界面标 [技能]），"
                       "受已有技能加点上限约束，随意改写会冲突，因此只读取显示、不提供修改。",
                  foreground="#666", wraplength=740, justify="left").pack(anchor="w")

    BOX_PAGE_SIZE = 15   # 道具箱每页显示条数（一屏可见，翻页栏无需滚动即达）

    def show_box(self, slot, page=None):
        """通用道具箱：500 格，装备+消耗品+未鉴定。分页显示，可翻页。"""
        for w in list(self.detail_inner.winfo_children()):
            w.destroy()
        self.detail_canvas.yview_moveto(0)
        used = [bi for bi in range(se.BOX_SLOT_COUNT)
                if se.box_used(self.data, se.box_offset(slot, bi))]
        total_pages = max(1, (len(used) + self.BOX_PAGE_SIZE - 1) // self.BOX_PAGE_SIZE)
        if page is None:
            page = getattr(self, "_box_page", 0)
        page = max(0, min(page, total_pages - 1))
        self._box_page = page      # 记住当前页，编辑后返回用
        self._box_slot = slot

        ttk.Label(self.detail_inner,
                  text=f"DATA {slot+1} / 通用道具箱 —— 该存档所有角色共用"
                       f"（{len(used)}/{se.BOX_SLOT_COUNT} 格占用）",
                  font=("Tahoma", 11, "bold")).pack(anchor="w", pady=(0, 6))
        ttk.Label(self.detail_inner,
                  text="占用判定 = 类型标记(+0x02)≠0x2000，未鉴定装备也算。"
                       "点【编辑】改该格，编辑完点【← 返回道具箱】回到此列表。",
                  foreground="#666", wraplength=760, justify="left").pack(anchor="w", pady=(0, 8))

        # 本页条目
        start = page * self.BOX_PAGE_SIZE
        page_items = used[start:start + self.BOX_PAGE_SIZE]
        for bi in page_items:
            o = se.box_offset(slot, bi)
            iid = self.effective_u16(o)
            typ = se.read_u16(self.data, o + 2)
            if iid == se.CONSUMABLE_ID:
                kind = se.consumable_kind(self.data, o)
                q = self.effective_u16(o + se.QTY_OFFSET) & 0xFF
                tag = "[技能]" if se.is_skill_kind(kind) else ""
                name = f"{tag}{self.cons.get(kind, f'种类{kind:#06x}')} x{q}"
            elif 0xBB <= iid <= 0x1E8:
                ms = se.active_mods(self.data, o)
                name = f"{se.grade(len(ms))}级 {se.item_name(iid, self.names)} " \
                       f"LV{se.item_level(self.data, o)}"
            else:
                name = f"未鉴定? iid={iid:#06x} type={typ:#06x}"
            row = ttk.Frame(self.detail_inner)
            row.pack(anchor="w", pady=1, fill="x")
            ttk.Label(row, text=f"[{bi:3}] @{o:#x}  {name}", width=58).pack(side="left")
            ttk.Button(row, text="编辑", command=lambda b=bi: self.select_box_item(slot, b)).pack(side="left")

        # 翻页控件
        nav = ttk.Frame(self.detail_inner)
        nav.pack(anchor="w", pady=(10, 2), fill="x")
        ttk.Button(nav, text="« 首页", command=lambda: self.show_box(slot, 0)).pack(side="left", padx=2)
        ttk.Button(nav, text="‹ 上一页", command=lambda: self.show_box(slot, page - 1)).pack(side="left", padx=2)
        ttk.Label(nav, text=f"  第 {page + 1} / {total_pages} 页  "
                            f"(第 {start + 1}-{start + len(page_items)} 条)  ",
                  foreground="#333").pack(side="left", padx=6)
        ttk.Button(nav, text="下一页 ›", command=lambda: self.show_box(slot, page + 1)).pack(side="left", padx=2)
        ttk.Button(nav, text="末页 »", command=lambda: self.show_box(slot, total_pages - 1)).pack(side="left", padx=2)
        # 直接跳页
        ttk.Label(nav, text="跳至:", foreground="#888").pack(side="left", padx=(12, 2))
        jump = tk.StringVar(value=str(page + 1))
        jent = ttk.Entry(nav, textvariable=jump, width=5)
        jent.pack(side="left")
        def do_jump():
            try:
                self.show_box(slot, int(jump.get()) - 1)
            except ValueError:
                pass
        jent.bind("<Return>", lambda _e: do_jump())
        ttk.Button(nav, text="转到", command=do_jump).pack(side="left", padx=2)

    def select_box_item(self, slot, bi):
        """选中道具箱某格，用统一详情面板编辑（复用 show_item 的装备/消耗品逻辑）。"""
        o = se.box_offset(slot, bi)
        self._box_mode = (slot, bi)
        for w in list(self.detail_inner.winfo_children()):
            w.destroy()
        self.detail_canvas.yview_moveto(0)
        # 返回上一级：回到道具箱列表并停在原页
        back = ttk.Frame(self.detail_inner)
        back.pack(anchor="w", pady=(0, 6), fill="x")
        ttk.Button(back, text="← 返回道具箱列表",
                   command=lambda: self.show_box(slot, getattr(self, "_box_page", 0))).pack(side="left")
        iid = self.effective_u16(o)
        if iid == se.CONSUMABLE_ID:
            self._render_item_editor(o, consumable=True)
        elif 0xBB <= iid <= 0x1E8:
            self._render_item_editor(o, consumable=False)
        else:
            ttk.Label(self.detail_inner,
                      text=f"道具箱[{bi}] 未鉴定/异常装备  @{o:#x}",
                      font=("Tahoma", 11, "bold")).pack(anchor="w", pady=(0, 8))
            ttk.Label(self.detail_inner,
                      text=f"物品ID={iid:#06x}  类型标记={se.read_u16(self.data, o+2):#06x}。"
                           "此类格子数据未标定，建议不要在界面改；如需处理请用命令行 set-box。",
                      foreground="#666", wraplength=760, justify="left").pack(anchor="w")

    def _render_item_editor(self, o, consumable):
        """道具箱格子的装备/消耗品编辑器（与行囊 show_item 相同的字段）。"""
        iid = self.effective_u16(o)
        ttk.Label(self.detail_inner, text=f"道具箱格 @{o:#x}",
                  font=("Tahoma", 11, "bold")).pack(anchor="w", pady=(0, 8))
        if consumable:
            kind = se.consumable_kind(self.data, o)
            q = self.effective_u16(o + se.QTY_OFFSET) & 0xFF
            ttk.Label(self.detail_inner,
                      text=f"消耗品：{self.cons.get(kind, f'种类{kind:#06x}')}  "
                           f"种类ID {kind:#06x}  数量 {q}",
                      font=("Tahoma", 10, "bold")).pack(anchor="w", pady=(6, 4))
            row = ttk.Frame(self.detail_inner)
            row.pack(anchor="w", pady=4)
            ttk.Label(row, text="数量(0-255)", width=14).pack(side="left")
            qv = tk.StringVar(value=str(q))
            ttk.Entry(row, textvariable=qv, width=8).pack(side="left")
            def apply_q():
                try:
                    v = int(qv.get())
                except ValueError:
                    return
                if 0 <= v <= 255:
                    self.edits[o + se.QTY_OFFSET] = ((se.read_u16(self.data, o+se.QTY_OFFSET) & 0xFF00) | v, 2)
                    self.update_changes()
                    messagebox.showinfo("已暂存", "数量已暂存，点底部【保存改动】写入。")
            ttk.Button(row, text="应用", command=apply_q).pack(side="left", padx=4)
        else:
            ms = se.active_mods(self.data, o)
            lo, hi = se.item_atk(self.data, o)
            dcur, dmax = se.durability(self.data, o)
            ttk.Label(self.detail_inner,
                      text=f"{se.grade(len(ms))}级 {se.item_name(iid, self.names)}  "
                           f"LV{se.item_level(self.data,o)}  "
                           f"ATK {lo}-{hi}  耐久 {int(dcur)}/{int(dmax)}",
                      font=("Tahoma", 10, "bold")).pack(anchor="w", pady=(8, 2))
            def numf(label, off, width, get, store, note=""):
                row = ttk.Frame(self.detail_inner)
                row.pack(anchor="w", pady=2, fill="x")
                ttk.Label(row, text=label, width=14).pack(side="left")
                var = tk.StringVar(value=str(get()))
                ttk.Entry(row, textvariable=var, width=8).pack(side="left")
                ttk.Label(row, text=note or f"@{off:#x}", foreground="#888").pack(side="left", padx=6)
                def apply(_e=None, off=off, var=var, store=store):
                    try:
                        self.edits[off] = (store(int(var.get())), width)
                        self.update_changes()
                    except ValueError:
                        pass
                ttk.Button(row, text="应用", command=apply).pack(side="left", padx=4)
            numf("装备等级", o+se.F_ITEM_LEVEL, 1, lambda: se.item_level(self.data,o),
                 lambda v: max(0, min(254, v-1)), "存值+1")
            numf("攻击下限", o+se.F_ITEM_ATK_MIN, 1, lambda: se.item_atk(self.data,o)[0], lambda v: max(0,min(255,v)))
            numf("攻击上限", o+se.F_ITEM_ATK_MAX, 1, lambda: se.item_atk(self.data,o)[1], lambda v: max(0,min(255,v)))
            numf("耐久当前", o+se.F_DUR_CUR, 4, lambda: int(se.durability(self.data,o)[0]),
                 lambda v: v*se.DUR_SCALE + se.read_u32(self.data,o+se.F_DUR_CUR)%se.DUR_SCALE)
            numf("耐久最大", o+se.F_DUR_MAX, 4, lambda: int(se.durability(self.data,o)[1]),
                 lambda v: v*se.DUR_SCALE + se.read_u32(self.data,o+se.F_DUR_MAX)%se.DUR_SCALE)
            ttk.Label(self.detail_inner, text="词条 1-8（FFFF=清空）",
                      font=("Tahoma",10,"bold")).pack(anchor="w", pady=(10,4))
            for idx, off in enumerate(se.MOD_OFFSETS):
                mo = o + off
                v = self.effective_u16(mo)
                row = ttk.Frame(self.detail_inner)
                row.pack(anchor="w", pady=1, fill="x")
                ttk.Label(row, text=f"词条{idx+1}", width=8).pack(side="left")
                var = tk.StringVar(value=f"{v:04X}")
                ttk.Entry(row, textvariable=var, width=8).pack(side="left")
                ttk.Label(row, text=self.mods.get(v, ""), width=40, foreground="#666").pack(side="left", padx=6)
                def apply_mod(_e=None, mo=mo, var=var):
                    try:
                        self.edits[mo] = (int(var.get(),16)&0xFFFF, 2)
                        self.update_changes()
                    except ValueError:
                        pass
                ttk.Button(row, text="应用", command=apply_mod).pack(side="left", padx=4)
        ttk.Label(self.detail_inner, text="改动已在底部统计，点【保存改动】统一写入并重算 CRC。",
                  foreground="#666", wraplength=760, justify="left").pack(anchor="w", pady=(10,0))

    def show_slot(self, slot):
        o = slot * se.DATA_SLOT_STRIDE + se.GOLD_OFFSET
        ttk.Label(self.detail_inner, text=f"DATA {slot+1}", font=("Tahoma", 11, "bold")).pack(anchor="w", pady=(0, 10))
        row = ttk.Frame(self.detail_inner)
        row.pack(anchor="w")
        ttk.Label(row, text="金币 (0-99999999)", width=20).pack(side="left")
        var = tk.StringVar(value=str(self.effective_u32(o)))
        ent = ttk.Entry(row, textvariable=var, width=14)
        ent.pack(side="left")

        def apply(_e=None):
            try:
                v = int(var.get())
            except ValueError:
                return
            if not (0 <= v <= se.MAX_GOLD):
                messagebox.showwarning("超出范围", f"金币必须在 0 – {se.MAX_GOLD} 之间")
                return
            self.edits[o] = (v, 4)   # 金币 = u32
            self.refresh_tree()
            self.update_changes()
        ent.bind("<Return>", apply)
        ttk.Button(row, text="应用", command=apply).pack(side="left", padx=4)
        ttk.Label(self.detail_inner, text=f"@{o:#x}  u32 小端", foreground="#888").pack(anchor="w", pady=6)

    # ---------- 保存 ----------
    def save(self):
        if not self.edits:
            messagebox.showinfo("无改动", "还没有任何改动。")
            return
        buf = bytearray(self.data)
        for o, (v, width) in self.edits.items():
            if width == 1:
                buf[o] = v & 0xFF
            elif width == 2:
                se.write_u16(buf, o, v)
            elif width == 4:
                se.write_u32(buf, o, v)
        se.seal(buf)  # 重算 CRC
        errs = se.validate(buf)
        if errs:
            messagebox.showerror("校验失败，未写入", "\n".join(errs))
            return
        n_edits = len(self.edits)
        p = Path(self.path.get())
        bak = p.with_name(p.name + ".editor-bak")
        if not bak.exists():
            import shutil
            shutil.copy2(p, bak)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(buf)
        import os
        os.replace(tmp, p)
        self.data = bytes(buf)
        self.edits.clear()
        self.refresh_tree()
        self.status.config(text=f"已保存  CRC {se.read_u32(buf,0):#010x}  [校验通过]  备份: {bak.name}")
        self.update_changes()
        messagebox.showinfo("完成", f"已保存 {n_edits} 处改动，CRC 已重算并校验通过。\n\n"
                                    f"原存档备份在:\n{bak}\n\n"
                                    f"请先完全退出游戏，再重新启动读取新存档。")

    def restore(self):
        p = Path(self.path.get())
        bak = p.with_name(p.name + ".editor-bak")
        if not bak.exists():
            messagebox.showinfo("无备份", f"没有找到 {bak.name}")
            return
        if not messagebox.askyesno("确认", f"用备份覆盖当前存档？\n{bak}\n\n当前存档会被替换。"):
            return
        import shutil
        shutil.copy2(bak, p)
        self.open_save(initial=True)
        messagebox.showinfo("完成", "已从备份还原。")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
