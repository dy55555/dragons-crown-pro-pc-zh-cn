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
import re
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
        self.root.minsize(900, 560)   # 再小控件会挤在一起被裁，限制最小尺寸
        self.path = tk.StringVar(value=str(se.DEFAULT_SAVE))
        self.data = None
        self.names = se.load_names(se.ITEMS_TSV)
        self.mods = se.mod_names(zh=True)
        self.cons = se.consumable_names()
        self.item_list = sorted(self.names.items())
        self.item_choices = [f"{v:04X}  {n}" for v, n in self.item_list]
        self.choice_to_id = {f"{v:04X}  {n}": v for v, n in self.item_list}
        # 道具单列一项：+0x00 恒为 0x0070（"可堆叠道具"这一个基础条目），
        # 具体是哪件看词条槽0 的效果ID
        self.consumable_key = f"{se.CONSUMABLE_ID:04X}  道具（种类看效果ID，见下方）"
        self.item_choices.append(self.consumable_key)
        self.choice_to_id[self.consumable_key] = se.CONSUMABLE_ID
        self.edits = {}          # (byte_offset) -> new_value
        self._build()
        self.open_save(initial=True)

    # ---------- UI ----------
    def _build(self):
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)     # 路径输入框占中间，随窗口伸缩
        ttk.Label(top, text="存档文件:").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.path).grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Button(top, text="打开", command=self.open_save).grid(row=0, column=2, padx=2)
        ttk.Button(top, text="重新载入", command=lambda: self.open_save(initial=True)).grid(row=0, column=3, padx=2)

        bar = ttk.Frame(self.root, padding=(8, 0))
        bar.pack(fill="x")
        ttk.Button(bar, text="还原备份 (.editor-bak)", command=self.restore).pack(side="right")
        self.status = ttk.Label(bar, text="", foreground="#0a6")
        self.status.pack(side="left", fill="x", expand=True)

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
        # 画布自身尺寸变化（窗口缩放）也要重算内框宽度，否则内框会卡在旧宽度上，
        # 窗口一缩右侧按钮就被挤出可视区。
        self.detail_canvas.bind("<Configure>", self._on_detail_configure)
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
            # 说明文字随宽度动态换行，避免窄窗口溢出
            wrap = max(200, w - 30)

            def apply_wrap():
                def walk(widget):
                    for ch in widget.winfo_children():
                        if isinstance(ch, ttk.Label):
                            try:
                                if ch.cget("wraplength") and ch.cget("wraplength") != wrap:
                                    ch.configure(wraplength=wrap)
                            except tk.TclError:
                                pass
                        walk(ch)
                walk(self.detail_inner)
            self.root.after_idle(apply_wrap)

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
        """重建左侧树。节点用固定 iid（s0 / s0c2b1i3 …），刷新后能原样恢复选中与展开。"""
        sel = self.tree.selection()
        keep = sel[0] if sel else None
        opened = [i for i in self._iter_nodes() if self.tree.item(i, "open")]
        self.tree.delete(*self.tree.get_children())
        self.node_tag = {}   # iid -> ("slot"|"char"|"bag"|"item"|"box"|"pouch", ...)
        for slot in range(se.DATA_SLOT_COUNT):
            gold = self.effective_u32(slot * se.DATA_SLOT_STRIDE + se.GOLD_OFFSET)
            snode = f"s{slot}"
            self.tree.insert("", "end", iid=snode, text=f"DATA {slot+1}   金币 {gold}",
                             open=(slot == 0))
            self.node_tag[snode] = ("slot", slot)
            for ci in range(se.CHAR_COUNT):
                o = se.char_offset(slot, ci)
                if not se.present(self.data, o):
                    continue
                nm = se.cname(self.data, o)
                lvl = self.effective_u8(o + se.F_LEVEL) + 1
                cls = se.CLASSES.get(self.effective_u8(o + se.F_CLASS), "?")
                cnode = f"s{slot}c{ci}"
                self.tree.insert(snode, "end", iid=cnode, text=f"  {nm}  {cls}  Lv{lvl}")
                self.node_tag[cnode] = ("char", slot, ci)
                for bag in range(se.BAG_CONFIG_COUNT):
                    cnt = sum(1 for si in range(se.BAG_SLOT_COUNT)
                              if self.effective_u16(se.bag_offset(o, bag, si)))
                    bnode = f"s{slot}c{ci}b{bag}"
                    self.tree.insert(cnode, "end", iid=bnode,
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
                        inode = f"s{slot}c{ci}b{bag}i{si}"
                        self.tree.insert(bnode, "end", iid=inode,
                                         text=f"      [{si}] {iid:#05x} {txt}")
                        self.node_tag[inode] = ("item", slot, ci, bag, si)
            # 通用道具箱节点（该 DATA slot 所有角色共用）
            boxcnt = sum(1 for bi in range(se.BOX_SLOT_COUNT)
                         if se.box_used(self.data, se.box_offset(slot, bi)))
            xnode = f"s{slot}x"
            self.tree.insert(snode, "end", iid=xnode,
                             text=f"  [通用道具箱]  {boxcnt}/{se.BOX_SLOT_COUNT}格（所有角色共用）")
            self.node_tag[xnode] = ("box", slot)
            # 尾部 18 格（用途待证实）
            pcnt = sum(1 for pi in range(se.POUCH_SLOT_COUNT)
                       if se.pouch_used(self.data, se.pouch_offset(slot, pi)))
            pnode = f"s{slot}p"
            self.tree.insert(snode, "end", iid=pnode,
                             text=f"  [尾部18格]  {pcnt}/{se.POUCH_SLOT_COUNT}格（用途待证实）")
            self.node_tag[pnode] = ("pouch", slot)
        for i in opened:
            if self.tree.exists(i):
                self.tree.item(i, open=True)
        if keep and self.tree.exists(keep):
            self.tree.selection_set(keep)
            self.tree.focus(keep)

    def _iter_nodes(self, parent=""):
        for i in self.tree.get_children(parent):
            yield i
            yield from self._iter_nodes(i)

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
        # 树刷新后会自动恢复选中并再触发一次本事件；视图没变就别重绘，
        # 否则正在编辑的输入框、展开的候选列表、滚动位置全被冲掉。
        if tag == getattr(self, "_rendered_tag", None) and self.detail_inner.winfo_children():
            return
        self._sel_node = sel[0]
        self._render_view(lambda t=tag: self._dispatch(t), tag=tag)

    def _dispatch(self, tag):
        kind = tag[0]
        if kind == "item":
            self.show_item(*tag[1:])
        elif kind == "char":
            self.show_char(*tag[1:])
        elif kind == "slot":
            self.show_slot(tag[1])
        elif kind == "bag":
            self.show_bag(*tag[1:])
        elif kind == "box":
            self.show_box(tag[1])
        elif kind == "pouch":
            self.show_pouch(tag[1])
        else:
            ttk.Label(self.detail_inner, text="选择左侧任一角色或装备格进行编辑",
                      foreground="#666").pack(anchor="w")

    def _render_view(self, fn, tag=None, to_top=True):
        """清空详情面板并渲染；记住重绘函数与对应节点，供「还原」后刷新。"""
        self._rendered_tag = tag
        self._view_fn = fn
        self._hit_closer = None      # 旧面板的候选框全部作废，别再引用
        pos = 0.0
        if not to_top:
            try:
                pos = self.detail_canvas.yview()[0]
            except (tk.TclError, IndexError):
                pos = 0.0
        for w in list(self.detail_inner.winfo_children()):
            w.destroy()
        fn()
        self.detail_canvas.yview_moveto(pos)
        self._on_detail_configure()

    def rerender(self, to_top=False):
        fn = getattr(self, "_view_fn", None)
        if fn:
            self._render_view(fn, tag=getattr(self, "_rendered_tag", None), to_top=to_top)

    def revert_field(self, *offs):
        """撤销若干字段的未保存改动并重绘当前视图（还原按钮统一走这里）。"""
        n = 0
        for off in offs:
            if self.edits.pop(off, None) is not None:
                n += 1
        self.refresh_tree()
        self.update_changes()
        self.rerender()
        self.status.config(text=f"已还原 {n} 处未保存改动（其他改动仍保留）" if n else "该字段没有未保存的改动")

    def _render_mod_rows(self, o, only_first=False, hint=True):
        """渲染词条 1-8 的编辑行。only_first=True 时只画词条1（道具的效果ID就是它）。"""
        ttk.Label(self.detail_inner,
                  text="附加词条 1-8（填 FFFF 即清空该条）" if not only_first
                  else "效果ID（就是词条1那个槽，换道具改这里）",
                  font=("Tahoma", 10, "bold")).pack(anchor="w", pady=(12, 4))
        if hint:
            ttk.Label(self.detail_inner,
                      text="两种填法：填ID（如 0021），或输中文关键字（如 攻击）从下方候选点选。",
                      foreground="#666", wraplength=760, justify="left").pack(anchor="w", pady=(0, 6))
        for idx, off in enumerate(se.MOD_OFFSETS):
            if only_first and idx > 0:
                break
            mo = o + off
            v = self.effective_u16(mo)
            mframe = ttk.Frame(self.detail_inner)
            mframe.pack(anchor="w", pady=2, fill="x")
            row = ttk.Frame(mframe)
            row.pack(anchor="w", fill="x")
            ttk.Label(row, text=f"词条{idx+1}", width=9).pack(side="left")
            var = tk.StringVar(value=f"{v:04X}")
            ttk.Entry(row, textvariable=var, width=10).pack(side="left")
            nametext = tk.StringVar()
            ttk.Label(row, textvariable=nametext, foreground="#555",
                      width=26, anchor="w").pack(side="left", padx=6)
            ttk.Label(row, text=f"@{mo:#x}", foreground="#aaa").pack(side="left")
            self._wire_mod_row(mframe, row, var, nametext, mo)

    def _revert_btn(self, row, offs):
        """在 apply 按钮旁边生成【还原】按钮。offs 可给多个偏移（如消耗品的 iid+类型）。"""
        return ttk.Button(row, text="还原", width=5,
                          command=lambda: self.revert_field(*offs))

    def _item_key(self, iid):
        """物品ID -> 下拉显示文本（消耗品固定显示为消耗品项）。"""
        if iid == se.CONSUMABLE_ID:
            return self.consumable_key
        return f"{iid:04X}  {se.item_name(iid, self.names)}"

    def _fix_type_for(self, o, old_id, new_id, equip_type):
        """换物品时同步类型标记(+0x02)：消耗品与装备互转必须改，否则游戏不认这格。"""
        if old_id == new_id:
            return
        if new_id == se.CONSUMABLE_ID:
            self.edits[o + 2] = (se.ITEM_TYPE_CONSUMABLE, 2)
        elif old_id == se.CONSUMABLE_ID:
            self.edits[o + 2] = (equip_type, 2)

    def _scroll_to(self, widget, margin=48):
        """把详情滚动面板滚到某控件附近（展开候选列表后保证看得见）。"""
        try:
            self.detail_canvas.update_idletasks()
            y = widget.winfo_rooty() - self.detail_inner.winfo_rooty()
            bb = self.detail_canvas.bbox("all")
            total = max(1, (bb[3] - bb[1]) if bb else 1)
            self.detail_canvas.yview_moveto(max(0.0, (y - margin) / total))
        except tk.TclError:
            pass

    def _wire_mod_row(self, host, row, var, nametext, mo):
        """词条输入框：实时显示名字；输入中文关键字时在下方列出候选，选中自动填 ID。

        host 是这一词条的外层竖排容器，候选列表就展开在输入行下方。
        """
        hits_box = ttk.Frame(host)

        def close_hits():
            # 面板重绘后 hits_box 可能已被销毁，这里必须容错：
            # 一旦抛 TclError，trace 回调就断了，之后所有格子都不再出候选。
            if getattr(self, "_hit_closer", None) is close_hits:
                self._hit_closer = None
            try:
                for w in hits_box.winfo_children():
                    w.destroy()
                hits_box.pack_forget()
            except tk.TclError:
                pass

        def sync_name(*_):
            t = var.get().strip()
            if not t:
                nametext.set("")
                return
            try:
                nv = int(t, 16)
            except ValueError:
                nametext.set("↓ 从下面候选里点一条")
                return
            if nv == se.MOD_EMPTY:
                nametext.set("（空槽）")
            else:
                nametext.set(self.mods.get(nv) or "（未收录词条）")

        def pick(k):
            var.set(f"{k:04X}")
            close_hits()
            self._on_detail_configure()

        def show_hits(kw):
            other = getattr(self, "_hit_closer", None)
            if other and other is not close_hits:
                other()
            close_hits()                  # 清掉自己上一次的内容（已容错）
            hits = se.search_mods(kw, self.mods, limit=40)
            if not hits:
                nametext.set("（没有匹配的词条）")
                return
            box = tk.Frame(hits_box, bd=1, relief="solid")
            box.pack(fill="x")
            lb = tk.Listbox(box, height=min(7, len(hits)), width=54,
                            font=("Consolas", 10), exportselection=False)
            for k, n in hits:
                lb.insert("end", f" {k:04X}  {n}")
            lb.pack(side="left", fill="both", expand=True)
            sb = ttk.Scrollbar(box, orient="vertical", command=lb.yview)
            sb.pack(side="right", fill="y")
            lb.configure(yscrollcommand=sb.set)
            ttk.Label(hits_box, text=f"匹配 {len(hits)} 条 · 单击即填入词条ID",
                      foreground="#888").pack(anchor="w")

            def on_pick(_e=None):
                cs = lb.curselection()
                if cs:
                    pick(hits[cs[0]][0])

            lb.bind("<<ListboxSelect>>", on_pick)
            hits_box.pack(anchor="w", fill="x", pady=(2, 4))
            self._hit_closer = close_hits
            self._scroll_to(hits_box)

        def on_write(*_):
            t = var.get().strip()
            if not t:
                close_hits()
                sync_name()
                return
            if re.fullmatch(r"[0-9A-Fa-f]{0,4}", t):
                close_hits()          # 纯十六进制，当ID处理
                sync_name()
                return
            sync_name()
            show_hits(t)              # 含中文/非hex字符 -> 模糊匹配

        var.trace_add("write", on_write)

        def apply_mod(_e=None):
            try:
                nv = int(var.get().strip(), 16) & 0xFFFF
            except ValueError:
                messagebox.showwarning(
                    "格式不对",
                    "词条格要填十六进制ID（如 0021）；\n"
                    "也可以直接输入中文关键字，从下方候选里点一条。")
                return
            self.edits[mo] = (nv, 2)
            self.refresh_tree()
            self.update_changes()
            self.rerender()

        ttk.Button(row, text="应用", command=apply_mod).pack(side="left", padx=2)
        ttk.Button(row, text="还原", width=5,
                   command=lambda: self.revert_field(mo)).pack(side="left")
        sync_name()

    def select_item(self, node):
        self.tree.selection_set(node)
        self.tree.focus(node)
        self.tree.see(node)
        self.on_select(None)

    def show_bag(self, slot, ci, bag):
        ttk.Label(self.detail_inner,
                  text=f"DATA {slot+1} / 角色{ci} / {se.BAG_NAMES[bag]} —— 整栏一览",
                  font=("Tahoma", 11, "bold")).pack(anchor="w", pady=(0, 6))
        ttk.Label(self.detail_inner,
                  text="点每行的【编辑】即可选中该格并修改；也可直接双击左侧具体格子。",
                  foreground="#666", wraplength=740, justify="left").pack(anchor="w", pady=(0, 10))
        for si in range(se.BAG_SLOT_COUNT):
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
            node = f"s{slot}c{ci}b{bag}i{si}"
            row = ttk.Frame(self.detail_inner)
            row.pack(anchor="w", pady=2, fill="x")
            ttk.Label(row, text=f"[{si}] {iid:#05x}  {lab}", width=34).pack(side="left")
            ttk.Button(row, text="编辑", command=lambda n=node: self.select_item(n)).pack(side="left")

    def show_item(self, slot, ci, bag, si):
        o = se.bag_offset(se.char_offset(slot, ci), bag, si)
        cur = self.effective_u16(o)
        ttk.Label(self.detail_inner, text=f"DATA {slot+1} / 角色{ci} / {se.BAG_NAMES[bag]} / 格{si}",
                  font=("Tahoma", 11, "bold")).pack(anchor="w", pady=(0, 8))
        ttk.Label(self.detail_inner, text=f"文件偏移: {o:#x}    字段: u16 小端 = 物品ID").pack(anchor="w")
        row = ttk.Frame(self.detail_inner)
        row.pack(anchor="w", pady=10, fill="x")
        ttk.Label(row, text="物品:").pack(side="left", padx=(0, 6))
        combo = ttk.Combobox(row, values=self.item_choices, width=44, state="readonly")
        key = self._item_key(cur)
        if key in self.choice_to_id:
            combo.set(key)
        combo.pack(side="left")
        ttk.Button(row, text="还原", width=5,
                   command=lambda: self.revert_field(o, o + 2)).pack(side="left", padx=(6, 0))
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
                self._fix_type_for(o, cur, v, se.ITEM_TYPE_BAG_WEAPON)
            hint.config(text=f"将改成 {v:#06x}  {self.names.get(v, '')}")
            self.refresh_tree()
            self.update_changes()

        combo.bind("<<ComboboxSelected>>", on_combo)

        # 装备名搜索 + 手填任意ID（把可改范围放到整张表以外也行）
        srow = ttk.Frame(self.detail_inner)
        srow.pack(anchor="w", pady=(0, 2), fill="x")
        ttk.Label(srow, text="搜装备:").pack(side="left")
        svar = tk.StringVar()
        sent = ttk.Entry(srow, textvariable=svar, width=18)
        sent.pack(side="left", padx=4)
        ttk.Label(srow, text="  手填ID(16进制):").pack(side="left", padx=(10, 0))
        mvar = tk.StringVar()
        ttk.Entry(srow, textvariable=mvar, width=8).pack(side="left", padx=4)

        ibox = ttk.Frame(self.detail_inner)
        hitlab = ttk.Label(ibox, foreground="#888")
        hitlab.pack(anchor="w")
        iresults = ttk.Frame(ibox)     # 结果行都挂这里，close 时只清它
        iresults.pack(fill="x")

        def set_item(nv):
            """换物品：写物品ID，必要时同步类型标记，然后原地重绘。"""
            self.edits[o] = (nv, 2)
            self._fix_type_for(o, cur, nv, se.ITEM_TYPE_BAG_WEAPON)
            self.refresh_tree()
            self.update_changes()
            self.rerender()
            self.status.config(
                text=f"已暂存：这格换成 {nv:#06x} {self.names.get(nv, '')}，点【保存改动】才写入")

        def close_ibox():
            # 只清结果行，保留 hitlab——它要留着显示下一次的提示文字。
            # 若连 hitlab 一起销毁，后面 hitlab.config() 会抛 TclError，整个搜索就失效了。
            try:
                for w in iresults.winfo_children():
                    w.destroy()
                ibox.pack_forget()
            except tk.TclError:
                pass

        def do_item_search(_e=None):
            kw = svar.get().strip()
            try:
                close_ibox()
                if not kw:
                    return
                hits = se.search_items(kw, self.names, limit=60)
                if not hits:
                    hitlab.config(text=f"没有名字匹配「{kw}」的装备")
                else:
                    hitlab.config(text=f"匹配 {len(hits)} 件 · 单击即换成该装备")
                    # 固定高度 + 滚动条，别让几十个结果把整个面板撑爆
                    lb = tk.Listbox(iresults, height=min(9, len(hits)), width=36,
                                    font=("Consolas", 10), exportselection=False)
                    for k, n in hits:
                        lb.insert("end", f" {k:04X}   {n}")
                    lb.pack(side="left", fill="both", expand=True)
                    sb = ttk.Scrollbar(iresults, orient="vertical", command=lb.yview)
                    sb.pack(side="right", fill="y")
                    lb.configure(yscrollcommand=sb.set)

                    def on_pick(_e=None):
                        cs = lb.curselection()
                        if cs:
                            set_item(hits[cs[0]][0])

                    lb.bind("<<ListboxSelect>>", on_pick)
                # after=srow：让它出现在搜索框正下方，而不是面板末尾（那会滚出可视区）
                ibox.pack(anchor="w", fill="x", pady=(2, 4), after=srow)
                self._on_detail_configure()
                self._scroll_to(ibox)
            except tk.TclError as e:
                # 不静默吞掉，否则界面看着像"点了没反应"
                self.status.config(text=f"搜装备出错: {e}")

        sent.bind("<Return>", do_item_search)
        ttk.Button(srow, text="查", width=4, command=do_item_search).pack(side="left")

        def apply_manual():
            raw = mvar.get().strip().replace("0x", "").replace("0X", "")
            try:
                nv = int(raw, 16)
            except ValueError:
                messagebox.showwarning("格式不对", "手填ID 用十六进制，例如 105 或 0x105")
                return
            if not (0 <= nv <= 0xFFFF):
                messagebox.showwarning("超出范围", "物品ID 必须在 0x0000 – 0xFFFF")
                return
            set_item(nv)

        ttk.Button(srow, text="用此ID", command=apply_manual).pack(side="left")

        if cur == se.CONSUMABLE_ID:
            kind = se.consumable_kind(self.data, o)
            sec = se.prop_section(kind)
            head = f"道具：{se.consumable_label(self.data, o, self.cons)}　效果ID {kind:#06x}"
            if sec:
                head += f"（{sec}）"
            ttk.Label(self.detail_inner, text=head,
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
            ttk.Button(qrow, text="还原", width=5,
                       command=lambda: self.revert_field(qo)).pack(side="left")
            ttk.Label(self.detail_inner,
                      text="数量改低字节即可。要换道具就改下面这个词条1——\n"
                           "存档里所有道具的物品ID(+0x00) 都是 0070，那只是「可堆叠道具」这个条目；"
                           "真正决定是哪件道具的，就是这个效果ID。",
                      foreground="#666", wraplength=760, justify="left").pack(anchor="w", pady=4)
            self._render_mod_rows(o, only_first=True)
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
                self._revert_btn(row, (off,)).pack(side="left")

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

            self._render_mod_rows(o)
            ttk.Label(self.detail_inner,
                      text="说明：中文名按 xlsx/日式游戏惯用译法直译，意思对得上就行；"
                           "发现差太多再逐条改。词条ID决定属性种类，"
                           "存档里另有一个 float 参数决定幅度，目前不在此处开放。",
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
            self._revert_btn(row, (off,)).pack(side="left")

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
                  text="技能是装备到行囊里的道具（效果ID 0x1D1 起，界面标 [技能]），"
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
                  text="占用判定 = 物品ID(+0x00)≠0。箱里有 2 件装备的类型标记也写成 0x2000，"
                       "所以不能按类型判。点【编辑】改该格，编辑完点【← 返回道具箱】回到此列表。",
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
                tag = "[技能]" if se.is_skill_kind(kind) else (
                    f"[{se.prop_section(kind)}]" if se.prop_section(kind) else "")
                name = (f"{tag}{se.consumable_label(self.data, o, self.cons)}"
                        f"{se.consumable_qty_text(self.data, o)}")
            elif 0xBB <= iid <= 0x1E8:
                ms = se.active_mods(self.data, o)
                name = f"{se.grade(len(ms))}级 {se.item_name(iid, self.names)} " \
                       f"LV{se.item_level(self.data, o)}"
            else:
                name = f"未鉴定? iid={iid:#06x} type={typ:#06x}"
            row = ttk.Frame(self.detail_inner)
            row.pack(anchor="w", pady=1, fill="x")
            # 先摆右侧按钮再摆左侧文本，否则长名字会把按钮挤出可视区
            ttk.Button(row, text="编辑", command=lambda b=bi: self.select_box_item(slot, b)).pack(
                side="right", padx=4)
            ttk.Label(row, text=f"[{bi:3}] @{o:#x}  {name}", anchor="w").pack(
                side="left", fill="x", expand=True)

        # 翻页控件：两行 grid，窄窗口也不挤掉按钮
        nav = ttk.Frame(self.detail_inner)
        nav.pack(anchor="w", pady=(10, 2), fill="x")
        for c in range(5):
            nav.columnconfigure(c, weight=1)
        ttk.Button(nav, text="« 首页", command=lambda: self.show_box(slot, 0)).grid(row=0, column=0, sticky="ew", padx=2)
        ttk.Button(nav, text="‹ 上一页", command=lambda: self.show_box(slot, page - 1)).grid(row=0, column=1, sticky="ew", padx=2)
        ttk.Label(nav, text=f"第 {page + 1} / {total_pages} 页 (第 {start + 1}-{start + len(page_items)} 条)",
                  foreground="#333", anchor="center").grid(row=0, column=2, sticky="ew", padx=4)
        ttk.Button(nav, text="下一页 ›", command=lambda: self.show_box(slot, page + 1)).grid(row=0, column=3, sticky="ew", padx=2)
        ttk.Button(nav, text="末页 »", command=lambda: self.show_box(slot, total_pages - 1)).grid(row=0, column=4, sticky="ew", padx=2)
        # 直接跳页（第二行）
        jump = tk.StringVar(value=str(page + 1))
        ttk.Label(nav, text="跳至第", foreground="#888").grid(row=1, column=0, sticky="e", padx=2)
        jent = ttk.Entry(nav, textvariable=jump, width=5)
        jent.grid(row=1, column=1, sticky="w", padx=2)
        def do_jump():
            try:
                self.show_box(slot, int(jump.get()) - 1)
            except ValueError:
                pass
        jent.bind("<Return>", lambda _e: do_jump())
        ttk.Button(nav, text="页 转到", command=do_jump).grid(row=1, column=2, sticky="w", padx=2)
        self._on_detail_configure()   # 直接调用（翻页）时也要把内框宽度对齐画布

    def select_box_item(self, slot, bi):
        """选中道具箱某格，用统一详情面板编辑（复用 show_item 的装备/消耗品逻辑）。"""
        def view():
            o = se.box_offset(slot, bi)
            for w in list(self.detail_inner.winfo_children()):
                w.destroy()
            # 返回上一级：回到道具箱列表并停在原页
            back = ttk.Frame(self.detail_inner)
            back.pack(anchor="w", pady=(0, 6), fill="x")
            ttk.Button(back, text="← 返回道具箱列表",
                       command=lambda: self.show_box(slot, getattr(self, "_box_page", 0))).pack(side="left")
            if not se.box_writable(bi):
                ttk.Label(self.detail_inner,
                          text=f"道具箱[{bi}] 不可编辑  @{o:#x}",
                          font=("Tahoma", 11, "bold")).pack(anchor="w", pady=(0, 8))
                ttk.Label(self.detail_inner,
                          text=f"第 {bi} 格的 0x{se.BAG_STRIDE:02X} 字节与角色0的记录头部物理重叠"
                               f"（0xA4BC+0x20 就是角色0的血量字段），写这一格会改坏角色数据。"
                               f"可编辑范围是 0-{se.BOX_WRITABLE_MAX}。",
                          foreground="#666", wraplength=760, justify="left").pack(anchor="w")
                return
            iid = self.effective_u16(o)
            if iid == se.CONSUMABLE_ID:
                self._render_item_editor(o, consumable=True)
            elif se.ITEM_ITEMID_MIN <= iid <= se.ITEM_ITEMID_MAX:
                self._render_item_editor(o, consumable=False)
            else:
                ttk.Label(self.detail_inner,
                          text=f"道具箱[{bi}] 未鉴定/异常装备  @{o:#x}",
                          font=("Tahoma", 11, "bold")).pack(anchor="w", pady=(0, 8))
                ttk.Label(self.detail_inner,
                          text=f"物品ID={iid:#06x}  类型标记={se.read_u16(self.data, o + 2):#06x}。"
                               "这类格子数据没标定（多半是未鉴定装备），"
                               "建议不要在这里改；真要处理请用命令行 set-box。",
                          foreground="#666", wraplength=760, justify="left").pack(anchor="w")
                self._render_item_picker(o, iid, se.ITEM_TYPE_BOX_EQUIP)
        self._box_mode = (slot, bi)
        self._render_view(view, tag=("box", slot))

    def _render_item_picker(self, o, iid, equip_type):
        """物品ID 下拉 + 还原：道具箱/尾部18格里换装备或换成消耗品都走这里。"""
        irow = ttk.Frame(self.detail_inner)
        irow.pack(anchor="w", pady=(6, 4), fill="x")
        ttk.Label(irow, text="物品:", width=10).pack(side="left")
        combo = ttk.Combobox(irow, values=self.item_choices, width=40, state="readonly")
        key = self._item_key(iid)
        if key in self.choice_to_id:
            combo.set(key)
        combo.pack(side="left")
        ttk.Button(irow, text="还原", width=5,
                   command=lambda: self.revert_field(o, o + 2)).pack(side="left", padx=4)

        def on_combo(_e):
            v = self.choice_to_id.get(combo.get())
            if v is None or v == iid:
                return
            self.edits[o] = (v, 2)
            self._fix_type_for(o, iid, v, equip_type)
            self.refresh_tree()
            self.update_changes()
            self.rerender()

        combo.bind("<<ComboboxSelected>>", on_combo)

    def _render_item_editor(self, o, consumable, where="道具箱格", equip_type=None):
        """道具箱/尾部18格格子的装备/消耗品编辑器（与行囊 show_item 相同的字段）。"""
        iid = self.effective_u16(o)
        if equip_type is None:
            equip_type = se.ITEM_TYPE_BOX_EQUIP
        ttk.Label(self.detail_inner, text=f"{where} @{o:#x}",
                  font=("Tahoma", 11, "bold")).pack(anchor="w", pady=(0, 8))
        self._render_item_picker(o, iid, equip_type)
        if consumable:
            kind = se.consumable_kind(self.data, o)
            sec = se.prop_section(kind)
            q = self.effective_u16(o + se.QTY_OFFSET) & 0xFF
            head = f"道具：{se.consumable_label(self.data, o, self.cons)}　效果ID {kind:#06x}"
            if sec:
                head += f"（{sec}）"
            ttk.Label(self.detail_inner, text=f"{head}　数量 {q}",
                      font=("Tahoma", 10, "bold")).pack(anchor="w", pady=(6, 4))
            ttk.Label(self.detail_inner,
                      text="所有道具的物品ID(+0x00) 都是 0070，真正区分是哪件道具的是「词条1」"
                           "里的效果ID，下面可以输中文搜；数量改低字节。",
                      foreground="#666", wraplength=760, justify="left").pack(anchor="w", pady=(0, 4))
            row = ttk.Frame(self.detail_inner)
            row.pack(anchor="w", pady=4)
            ttk.Label(row, text="数量 (0-255)", width=14).pack(side="left")
            qv = tk.StringVar(value=str(q))
            ttk.Entry(row, textvariable=qv, width=8).pack(side="left")

            def apply_q():
                try:
                    v = int(qv.get())
                except ValueError:
                    return
                if not (0 <= v <= 255):
                    messagebox.showwarning("超出范围", "数量必须在 0-255 之间")
                    return
                self.edits[o + se.QTY_OFFSET] = (
                    (se.read_u16(self.data, o + se.QTY_OFFSET) & 0xFF00) | v, 2)
                self.update_changes()
                messagebox.showinfo("已暂存", "数量已暂存，点底部【保存改动】写入。")

            ttk.Button(row, text="应用", command=apply_q).pack(side="left", padx=4)
            ttk.Button(row, text="还原", width=5,
                       command=lambda: self.revert_field(o + se.QTY_OFFSET)).pack(side="left")
            self._render_mod_rows(o, only_first=True)
        else:
            ms = se.active_mods(self.data, o)
            lo, hi = se.item_atk(self.data, o)
            dcur, dmax = se.durability(self.data, o)
            ttk.Label(self.detail_inner,
                      text=f"{se.grade(len(ms))}级 {se.item_name(iid, self.names)}  "
                           f"LV{se.item_level(self.data, o)}  "
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
                self._revert_btn(row, (off,)).pack(side="left")

            numf("装备等级", o + se.F_ITEM_LEVEL, 1, lambda: se.item_level(self.data, o),
                 lambda v: max(0, min(254, v - 1)), "存值+1")
            numf("攻击下限", o + se.F_ITEM_ATK_MIN, 1, lambda: se.item_atk(self.data, o)[0],
                 lambda v: max(0, min(255, v)))
            numf("攻击上限", o + se.F_ITEM_ATK_MAX, 1, lambda: se.item_atk(self.data, o)[1],
                 lambda v: max(0, min(255, v)))
            numf("耐久当前", o + se.F_DUR_CUR, 4, lambda: int(se.durability(self.data, o)[0]),
                 lambda v: v * se.DUR_SCALE + se.read_u32(self.data, o + se.F_DUR_CUR) % se.DUR_SCALE)
            numf("耐久最大", o + se.F_DUR_MAX, 4, lambda: int(se.durability(self.data, o)[1]),
                 lambda v: v * se.DUR_SCALE + se.read_u32(self.data, o + se.F_DUR_MAX) % se.DUR_SCALE)
            ttk.Label(self.detail_inner, text="词条 1-8（FFFF=清空；可输中文关键字模糊匹配）",
                      font=("Tahoma", 10, "bold")).pack(anchor="w", pady=(10, 4))
            for idx, off in enumerate(se.MOD_OFFSETS):
                mo = o + off
                v = self.effective_u16(mo)
                mframe = ttk.Frame(self.detail_inner)
                mframe.pack(anchor="w", pady=1, fill="x")
                row = ttk.Frame(mframe)
                row.pack(anchor="w", fill="x")
                ttk.Label(row, text=f"词条{idx+1}", width=8).pack(side="left")
                var = tk.StringVar(value=f"{v:04X}")
                ttk.Entry(row, textvariable=var, width=10).pack(side="left")
                nametext = tk.StringVar()
                ttk.Label(row, textvariable=nametext, foreground="#555",
                          width=28, anchor="w").pack(side="left", padx=6)
                ttk.Label(row, text=f"@{mo:#x}", foreground="#aaa").pack(side="left")
                self._wire_mod_row(mframe, row, var, nametext, mo)
        ttk.Label(self.detail_inner, text="改动已在底部统计，点【保存改动】统一写入并重算 CRC。",
                  foreground="#666", wraplength=760, justify="left").pack(anchor="w", pady=(10, 0))

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
        self._revert_btn(row, (o,)).pack(side="left")
        ttk.Label(self.detail_inner, text=f"@{o:#x}  u32 小端", foreground="#888").pack(anchor="w", pady=6)

    # ---------- 尾部 18 格（用途待证实）----------
    POUCH_PAGE_SIZE = 15

    def show_pouch(self, slot, page=None):
        """DATA slot 末尾的 18 格消耗品。用途未证实，见说明文字。"""
        used = [pi for pi in range(se.POUCH_SLOT_COUNT)
                if se.pouch_used(self.data, se.pouch_offset(slot, pi))]
        total_pages = max(1, (len(used) + self.POUCH_PAGE_SIZE - 1) // self.POUCH_PAGE_SIZE)
        if page is None:
            page = getattr(self, "_pouch_page", 0)
        page = max(0, min(page, total_pages - 1))
        self._pouch_page = page
        ttk.Label(self.detail_inner,
                  text=f"DATA {slot+1} / 尾部 18 格（用途待证实）—— "
                       f"{len(used)}/{se.POUCH_SLOT_COUNT} 格占用",
                  font=("Tahoma", 11, "bold")).pack(anchor="w", pady=(0, 6))
        ttk.Label(self.detail_inner,
                  text=f"起点 @{se.pouch_offset(slot, 0):#x}，18 格 × 0x54，紧挨着共享金币字段后面。"
                       "这 18 格装的都是消耗品，但里面的东西在通用道具箱里几乎都找不到，"
                       "游戏人物界面也翻不到对应位置，所以它到底是干嘛的还没证实。"
                       "【别当仓库用】只建议看，不建议改。",
                  foreground="#666", wraplength=760, justify="left").pack(anchor="w", pady=(0, 8))
        start = page * self.POUCH_PAGE_SIZE
        page_items = used[start:start + self.POUCH_PAGE_SIZE]
        for pi in page_items:
            o = se.pouch_offset(slot, pi)
            iid = self.effective_u16(o)
            if iid == se.CONSUMABLE_ID:
                kind = se.consumable_kind(self.data, o)
                q = self.effective_u16(o + se.QTY_OFFSET) & 0xFF
                sec = se.prop_section(kind)
                tag = "[技能]" if se.is_skill_kind(kind) else (f"[{sec}]" if sec else "")
                name = (f"{tag}{se.consumable_label(self.data, o, self.cons)}"
                        f"{se.consumable_qty_text(self.data, o)}")
            else:
                name = f"iid={iid:#06x} type={se.read_u16(self.data, o + 2):#06x}"
            row = ttk.Frame(self.detail_inner)
            row.pack(anchor="w", pady=1, fill="x")
            # 先摆右侧按钮再摆左侧文本，否则长名字会把按钮挤出可视区
            ttk.Button(row, text="编辑",
                       command=lambda p=pi: self.select_pouch_item(slot, p)).pack(side="right", padx=4)
            ttk.Label(row, text=f"[{pi:2}] @{o:#x}  {name}", anchor="w").pack(
                side="left", fill="x", expand=True)
        if total_pages > 1:
            nav = ttk.Frame(self.detail_inner)
            nav.pack(anchor="w", pady=(8, 2), fill="x")
            for c in range(3):
                nav.columnconfigure(c, weight=1)
            ttk.Button(nav, text="‹ 上一页",
                       command=lambda: self.show_pouch(slot, page - 1)).grid(
                row=0, column=0, sticky="ew", padx=2)
            ttk.Label(nav, text=f"第 {page + 1} / {total_pages} 页", anchor="center").grid(
                row=0, column=1, sticky="ew", padx=4)
            ttk.Button(nav, text="下一页 ›",
                       command=lambda: self.show_pouch(slot, page + 1)).grid(
                row=0, column=2, sticky="ew", padx=2)
        self._on_detail_configure()   # 直接调用（翻页）时也要把内框宽度对齐画布

    def select_pouch_item(self, slot, pi):
        o = se.pouch_offset(slot, pi)
        self._pouch_mode = (slot, pi)

        def view():
            for w in list(self.detail_inner.winfo_children()):
                w.destroy()
            back = ttk.Frame(self.detail_inner)
            back.pack(anchor="w", pady=(0, 6), fill="x")
            ttk.Button(back, text="← 返回尾部18格列表",
                       command=lambda: self.show_pouch(slot, getattr(self, "_pouch_page", 0))).pack(side="left")
            self._render_item_editor(o, consumable=True, where="尾部18格",
                                     equip_type=se.ITEM_TYPE_BAG_WEAPON)

        self._render_view(view, tag=("pouch", slot))

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
