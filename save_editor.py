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
# 允许写入的物品ID范围（比名称表宽）：名称表 0xBB-0x1E8 是游戏全部 302 件装备，
# 且实测连续无空洞；这里再放宽到 0x0001-0x01FF，方便试表外 ID（会显示「未收录」）。
# 0x0070 单独表示道具（＝「可堆叠道具」这一个条目，具体是哪件看词条槽0 的效果ID）。
ITEM_ID_ALLOW_MIN = 0x0001
ITEM_ID_ALLOW_MAX = 0x01FF

# ---- 道具/消耗品的两层结构（别把 +0x00 当成种类）------------------------
# 物品记录永远是这个形状，装备和道具共用同一套 0x54 字节：
#   +0x00  基础物品ID（同一张物品表里的一行）
#          装备：每件一个（0xBB–0x1E8，逐号连续）
#          道具：全部固定 0x0070 —— 0x70 是"可堆叠道具"这一个基础条目，
#                不是具体种类。所以存档里几百个道具的 +0x00 看着都一样，正常。
#   词条槽0 才是真正的种类：装备放词条ID（0x20–0x11C），
#          道具放效果ID（0x17F 起：戒指→卷轴→药水→技能）。
# 实测两段永不重叠：本机存档里装备词条最大 0x11C，道具效果最小 0x17F。
CONSUMABLE_ID = 0x0070     # 基础物品ID：代表"一个可堆叠道具"
QTY_OFFSET = 0x0C          # u16 低字节 = 数量（实测 x20/x8/x4/x7 全部吻合）
KIND_OFFSET = 0x00         # 仅作说明用：种类不在这里，在词条槽0
# 道具效果ID 段位（供界面显示"这是戒指还是药水"）
# 依据：游戏文本表 CustomItem 顺序 = 效果ID + 0x749，逐条核对得来。
PROP_EFFECT_RING = (0x017F, 0x018D)     # 戒指：火/冰/雷/毒/治疗 各三档
PROP_EFFECT_SCROLL = (0x0190, 0x0194)   # 卷轴
PROP_EFFECT_POTION = (0x0197, 0x01B4)   # 药水：治愈/万能药/抗性/活力/强效/防御/坚毅/食人魔/幸运/炸药
PROP_EFFECT_RUNESTONE = (0x01B6, 0x01CF)  # 符文石 A–Z
PROP_EFFECT_SKILL = (0x01D1, 0x01F8)    # 技能道具：各职业技能 + 法术
PROP_EFFECT_SPECIAL = ((0x01FA, 0x01FB), (0x0209, 0x020A))  # 特殊/任务道具（两小段）
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
# 起点 = slot*DATA_SLOT_STRIDE + 0xAD8（DATA1 实测 0xAD8，DATA2=0xB03AC，DATA3=0x15FC80）
# 466 格 × 0x54（见 BOX_SLOT_COUNT 注释），结构与行囊装备完全相同：物品ID在槽首+0x00、词条在 -0x40、
# 类型标记 +0x02 = 0x2A00(箱中装备)/0x2200(消耗品)/0x2800(法杖)/0x2000(空格)。
# 游戏界面显示 343/500：装备按件计、消耗品种类按堆叠计。
# 注意：之前误当成道具箱的 0xAF2D4 起 20 格其实是「随身携带道具/快捷栏」，不是仓库。
BOX_OFFSET = 0xAD8
# 格数由下面的硬边界决定，不能按游戏界面显示的「500」取：
#   道具箱区 = 0xAD8 起，角色记录从 0xA400 开始，
#   (0xA400 - 0xAD8) // 0x54 = 466，所以只有 466 格（索引 0..465）。
# 按 500 格算，第 467 格起点 0xA414 已经进角色记录，第 473 格会读到角色名的
# 中间字节（读出 iid=0x696C，其实就是 "li"）。写这种格子会改坏角色数据。
BOX_SLOT_COUNT = 466
BOX_SLOT_COUNT_UI = 500   # 游戏界面显示的上限，仅用于提示，不参与寻址
# 占用判定：类型标记(+0x02) != 0x2000 即算占用（含未鉴定装备，其 iid 可能为 0）。
# 实测 typ!=0x2000 的格数 = 343，与游戏界面「343/500」完全一致。
BOX_EMPTY_TYPE = 0x2000

# 物品头 +0x02 的类型标记（换物品时需同步，否则游戏不认这一格）
ITEM_TYPE_BAG_WEAPON = 0x6800    # 行囊里的武器
ITEM_TYPE_BAG_ARMOR = 0x6A00     # 行囊里的防具/饰品
ITEM_TYPE_BOX_EQUIP = 0x2A00     # 道具箱里的装备
ITEM_TYPE_CONSUMABLE = 0x2200    # 消耗品 / 道具
ITEM_TYPE_EMPTY = 0x2000         # 空格

# ---- 随身携带道具 / 快捷栏（DATA slot 末尾，18 格）---------------------
# 起点 = slot*DATA_SLOT_STRIDE + 0xAF2D4，18 格 × 0x54，实测全部 iid=0x0070、
# 类型标记 0x2200（消耗品），三个 DATA 结构完全一致（DATA1 满、DATA2 19格、DATA3 空）。
# 边界依据：0xAF2D4 + 18*0x54 = 0xAF8BC，距 slot 末尾 0xAF8D4 只差 0x18；
# 若按 20 格取，第 19/20 格起点 0xAF910 已跨入下一 DATA，会改坏别的位置，故只取 18 格。
POUCH_OFFSET = 0xAF2D4
POUCH_SLOT_COUNT = 18


def box_used(data, o: int) -> bool:
    return read_u16(data, o + 0x02) != BOX_EMPTY_TYPE

# ---- 技能道具判定（上游 dcp_cheats.h IsSkillItem 官方掩码）--------------
# 第一个词条(效果)ID 落在 0x1D1..0x1F8 且命中掩码 => 该道具是「技能」
# 技能是装备上去使用的，受技能点上限约束，本编辑器只读显示、不提供修改。
SKILL_FIRST_EFFECT = 0x1D1
SKILL_EFFECT_COUNT = 0x28
SKILL_EFFECT_MASK = 0xE362E5F7FF
# 掩码覆盖 0x1D1–0x1F8 共 40 位，实测这一段全部是技能/法术道具，无需补漏。
# 注意 0x1FA（魔眼灵魂捕捉瓮）和 0x1FB（亚马逊仪式护甲）是任务道具，不在技能段。

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
    for line in path.read_text(encoding="utf-8-sig").splitlines():
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


# ---- 简繁归一化（词条表里简繁混排：既有「攻擊」也有「伤害」）-------------
# 搜索/比对前把两边都折成简体，用户输简体或繁体都能命中。只收游戏词条用到的字。
_T2S_PAIRS = (
    "並并 亂乱 來来 個个 備备 傳传 傷伤 凍冻 擊击 創创 劍剑 効效 動动 喚唤 噴喷 圍围 場场 "
    "夠够 審审 寶宝 將将 專专 對对 屬属 巻卷 幣币 帶带 幾几 強强 彈弹 弾弹 後后 從从 復复 "
    "惡恶 態态 憑凭 懲惩 戰战 捲卷 換换 擋挡 擲掷 擺摆 敵敌 數数 於于 昇升 時时 暈晕 暫暂 "
    "書书 會会 極极 標标 機机 殺杀 毀毁 氷冰 減减 滅灭 滿满 潰溃 瀕濒 灑洒 為为 無无 燒烧 "
    "爲为 狀状 獄狱 獲获 獸兽 環环 產产 異异 當当 療疗 発发 發发 監监 確确 禦御 竜龙 範范 "
    "紋纹 級级 結结 緩缓 縮缩 續续 膚肤 與与 藥药 蠻蛮 術术 衝冲 裏里 裝装 製制 視视 計计 "
    "詛诅 詠咏 話话 認认 識识 護护 變变 讓让 貫贯 賦赋 質质 軸轴 輕轻 輪轮 轉转 迴回 這这 "
    "連连 進进 邊边 釋释 銳锐 鋒锋 錘锤 錢钱 鐵铁 長长 閃闪 開开 閒闲 間间 隊队 隕陨 雲云 "
    "電电 霧雾 靈灵 預预 頭头 頼赖 顆颗 題题 額额 風风 颶飓 飛飞 餘余 館馆 騎骑 骷骷 髏髅 "
    "體体 鷹鹰 齒齿 龍龙 呪咒 聖圣 專专 傳传 傷伤 價价 儀仪 餘余 備备 養养 獸兽 減减 擊击 "
    "彈弹 準准 濕湿 獨独 獎奖 現现 產产 畢毕 盡尽 確确 種种 積积 稱称 穩稳 窮穷 竪竖 競竞 "
    "筆笔 簡简 範范 簽签 籃篮 糾纠 紀纪 約约 紅红 純纯 紙纸 素素 紫紫 細细 終终 組组 絕绝 "
    "統统 絲丝 緊紧 練练 織织 繩绳 繼继 續续 罷罢 羅罗 義义 習习 聯联 聲声 聽听 肅肃 脅胁 "
    "腦脑 膽胆 臉脸 臨临 舉举 舊旧 艱艰 節节 範范 築筑 簡简 簽签 籌筹 籃篮 類类 顧顾 願愿 "
    "覺觉 觀观 規规 視视 親亲 戰战 鬥斗 魯鲁 鮮鲜 鳥鸟 鳳凤 鳴鸣 鴻鸿 鵰雕 麗丽 黃黄 黑黑 "
    "黨党 齊齐 齒齿 龜龟 備备 傷伤 億亿 優优 儲储 兒儿 兩两 冊册 冪幂 劃划 勞劳 勢势 勳勋 "
    "區区 協协 單单 嚴严 彈弹 徑径 從从 徹彻 恆恒 悅悦 慘惨 慶庆 憂忧 應应 懷怀 戀恋 戶户 "
    "執执 掃扫 掛挂 採采 揀拣 損损 搖摇 擔担 據据 擬拟 擴扩 擺摆 敗败 斬斩 斷断 曆历 曉晓 "
    "術术 條条 楊杨 槓杠 槍枪 樓楼 樣样 樹树 橋桥 檢检 檔档 欄栏 權权 歐欧 殘残 氣气 決决 "
    "沒没 淚泪 淨净 涼凉 測测 湯汤 溝沟 滅灭 溫温 滄沧 滾滚 漁渔 漲涨 漸渐 漿浆 潔洁 潛潜 "
    "潤润 潭潭 濃浓 濤涛 濫滥 濕湿 濟济 濱滨 濾滤 灘滩 點点 煉炼 煙烟 熱热 燈灯 營营 燦灿 "
    "燭烛 爐炉 爭争 爺爷 牆墙 狀状 狹狭 獅狮 獎奖 獲获 獵猎 獻献 現现 環环 璽玺 瓊琼 畫画 "
    "當当 療疗 瘋疯 瘡疮 癒愈 發发 盃杯 盜盗 盤盘 盪荡 睜睁 矚瞩 碼码 磚砖 礎础 禦御 稱称 "
    "穀谷 窩窝 窮穷 竄窜 筆笔 筍笋 節节 範范 築筑 簡简 簽签 籌筹 籠笼 粵粤 糧粮 糾纠 紀纪 "
    "紅红 純纯 紙纸 級级 素素 紫紫 細细 終终 組组 結结 絕绝 給给 統统 絲丝 經经 綠绿 維维 "
    "網网 緊紧 緒绪 線线 練练 縛缚 縣县 縫缝 總总 織织 繩绳 繪绘 繼继 續续 罰罚 罷罢 義义 "
    "習习 聯联 聲声 聰明 膽胆 臉脸 臨临 舉举 舊旧 艱艰 蘭兰 虎虎 處处 號号 蟲虫 蠻蛮 術术 "
    "衝冲 補补 裝装 製制 複复 褲裤 襪袜 見见 規规 視视 親亲 覺觉 觀观 計计 訂订 認认 討讨 "
    "訓训 記记 講讲 許许 論论 設设 訪访 証证 評评 試试 詩诗 話话 該该 詳详 誇夸 誌志 認认 "
    "語语 誠诚 誤误 說说 誰谁 課课 調调 談谈 請请 諸诸 諾诺 謀谋 謂谓 謎谜 講讲 謝謝 證证 "
    "識识 譜谱 警警 議议 護护 讀读 變变 讓让 讚赞 谷谷 豐丰 豬猪 貝贝 負负 財财 貢贡 貧贫 "
    "貨货 販贩 貪贪 責责 貴贵 買买 貸贷 費费 貼贴 貿贸 費费 賀贺 貴贵 買买 賣卖 質质 賬账 "
    "賭赌 購购 賺赚 賽赛 贊赞 贈赠 贊赞 赤赤 趙赵 足足 跌跌 跡迹 踐践 蹤踪 車车 軌轨 軍军 "
    "軒轩 軟软 較较 載载 輔辅 輕轻 輛辆 輝辉 輩辈 輪轮 輯辑 輸输 轄辖 轉转 轟轰 辦办 農农 "
    "迴回 迷迷 迸迸 逕径 連连 週周 進进 遊游 運运 過过 達达 違违 遙遥 遠远 遜逊 適适 選选 "
    "遷迁 選选 遺遗 遼辽 邁迈 還还 邊边 邏逻 郵邮 鄉乡 鄰邻 醜丑 醫医 釀酿 釋释 里里 重重 "
    "野野 量量 金金 針针 釘钉 釣钓 鈴铃 銀银 銅铜 銘铭 銷销 鋒锋 鋤锄 鋪铺 鋼钢 錄录 錘锤 "
    "錢钱 錦锦 錫锡 錬炼 鍵键 鎖锁 鎧铠 鏈链 鏡镜 鐘钟 鐵铁 鑄铸 鑑鉴 鑰钥 镶镶 長长 門门 "
    "閉闭 開开 閏闰 閑闲 間间 閱阅 闆板 關关 闘斗 阻阻 阿阿 附附 降降 限限 陛陛 院院 陣阵 "
    "除除 陥陷 院院 陰阴 陳陈 陸陆 陽阳 隊队 階阶 隨随 險险 隱隐 隸隶 隻只 雅雅 集集 雜杂 "
    "雞鸡 離离 難难 雨雨 雪雪 雲云 電电 需需 靜静 非非 靠靠 面面 靭韧 靴靴 靶靶 音音 頁页 "
    "頂顶 項项 順顺 須须 預预 頌颂 領领 頭头 頸颈 頻频 顆颗 題题 顏颜 願愿 類类 顧顾 顯显 "
    "風风 飛飞 食食 飯饭 飲饮 飽饱 餅饼 養养 餐餐 餘余 館馆 饅馒 首首 香香 馬马 駐驻 騎骑 "
    "騰腾 驅驱 驗验 驚惊 骨骨 骯肮 體体 高高 髮发 鬥斗 魅魅 魔魔 魚鱼 鮮鲜 鳥鸟 鳳凤 鳴鸣 "
    "鴻鸿 鵬鹏 鷹鹰 鹿鹿 麗丽 麥麦 黃黄 黎黎 黑黑 鼓鼓 鼠鼠 鼻鼻 齊齐 齒齿 龍龙 龜龟"
)
_T2S = {}
for _pair in _T2S_PAIRS.split():
    if len(_pair) == 2 and _pair[0] != _pair[1]:
        _T2S[ord(_pair[0])] = _pair[1]


def to_simple(text: str) -> str:
    """繁体折成简体，用于搜索比对（词条表简繁混排）。"""
    return text.translate(_T2S)


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
    """道具数量：u16 的低字节。实测 x20/x8/x4/x7 与游戏显示一致。

    只对道具有效：装备在同一位置放的是别的字段（+0x0C 恒 0x0100），
    所以先确认 +0x00 是道具再读，免得把装备的字段当数量显示。
    """
    if read_u16(data, o) != CONSUMABLE_ID:
        return 0
    return read_u16(data, o + QTY_OFFSET) & 0xFF


def consumable_kind(data, o: int) -> int:
    """道具的效果ID（存在词条槽0）。

    注意这不是「物品ID」：所有道具的 +0x00 都是 0x0070，真正区分是哪件道具
    靠这个效果ID。它和装备词条共用一套编号空间，但取值范围不重叠：
    装备词条 0x20–0x11C，道具效果 0x17F 起。
    """
    return read_u16(data, o + MOD_OFFSETS[0])


def consumable_qty_text(data, o: int) -> str:
    """数量后缀。0xFF 表示「不可堆叠/不适用」（任务道具就存这个值），不显示数量。"""
    q = consumable_qty(data, o)
    return "" if q == 0xFF else f" x{q}"


def prop_section(kind: int) -> str:
    """效果ID 落在哪一段，用于界面提示「戒指/卷轴/药水/符文石/技能/特殊」。"""
    if PROP_EFFECT_RING[0] <= kind <= PROP_EFFECT_RING[1]:
        return "戒指"
    if PROP_EFFECT_SCROLL[0] <= kind <= PROP_EFFECT_SCROLL[1]:
        return "卷轴"
    if PROP_EFFECT_POTION[0] <= kind <= PROP_EFFECT_POTION[1]:
        return "药水"
    if PROP_EFFECT_RUNESTONE[0] <= kind <= PROP_EFFECT_RUNESTONE[1]:
        return "符文石"
    if PROP_EFFECT_SKILL[0] <= kind <= PROP_EFFECT_SKILL[1]:
        return "技能"
    for lo, hi in PROP_EFFECT_SPECIAL:
        if lo <= kind <= hi:
            return "特殊"
    return ""


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


def mod_param(data, o: int, idx: int) -> float:
    """第 idx 个词条槽存档里的 f32 参数（游戏据此换算真实显示数值）。"""
    return struct.unpack_from("<f", data, o + MOD_OFFSETS[idx] + 4)[0]


def mod_display(data, o: int, idx: int, zh: dict) -> str:
    """词条显示文本：直接给词条中文名（取不到就显示 ID，不当成空）。"""
    v = read_u16(data, o + MOD_OFFSETS[idx])
    if v == MOD_EMPTY:
        return ""
    return zh.get(v) or f"（未收录词条 {v:#06x}）"


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


_MOD_ZH_CACHE: dict[int, str] | None = None


def mod_names_cached() -> dict[int, str]:
    """中文词条表（带缓存，供消耗品名兜底用，避免每次重读 5000 行）。"""
    global _MOD_ZH_CACHE
    if _MOD_ZH_CACHE is None:
        _MOD_ZH_CACHE = mod_names(zh=True)
    return _MOD_ZH_CACHE


def consumable_label(data, o: int, cons: dict[int, str] | None = None) -> str:
    """道具显示名。

    名字表（save_consumables.tsv）有就用名字，如 0x190 -> 「陨石卷轴」。
    没有就退回词条表的效果描述，但要加「效果#」前缀——词条表写的是"这东西干什么"
    （0x190 -> 「從天空降下大量火球雨」），不是道具名，不加前缀会让人误以为是名字。
    以「的」结尾的是前缀形容词（如 0x197「朴素的」），当名字没意义，直接跳过。
    """
    kind = consumable_kind(data, o)
    if cons is None:
        cons = consumable_names()
    name = cons.get(kind)
    if name:
        return name
    eff = mod_names_cached().get(kind, "")
    if eff and not eff.endswith("的"):
        return f"效果#{kind:04x} {eff}"
    return f"未收录效果#{kind:04x}"


def item_label(data, o: int, names: dict[int, str], cons=None) -> str:
    iid = read_u16(data, o)
    if iid == CONSUMABLE_ID:
        q = consumable_qty(data, o)
        base = consumable_label(data, o, cons)
        return base + ("" if q == 0xFF else f" x{q}")
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


def pouch_offset(slot: int, idx: int) -> int:
    """DATA slot 的随身携带道具(快捷栏)第 idx 格绝对偏移，共 18 格。"""
    return slot * DATA_SLOT_STRIDE + POUCH_OFFSET + idx * BAG_STRIDE


def pouch_used(data, o: int) -> bool:
    return read_u16(data, o + 0x02) != BOX_EMPTY_TYPE


def search_mods(keyword: str, names: dict[int, str], limit: int = 80) -> list[tuple[int, str]]:
    """按关键字模糊匹配词条：中文名包含关键字即命中，也支持十六进制 ID 片段。

    例：搜「攻击」→ 所有名字里带「攻击/攻擊」的词条（自动简繁归一）；
    搜「0021」→ 精确那条；空格分隔多个词时要求全部命中。
    返回 [(ID, 名称)]，按 ID 升序，最多 limit 条。
    """
    raw = (keyword or "").strip()
    if not raw:
        return []
    parts = [p for p in raw.split() if p]
    hexes = []
    words = []
    for p in parts:
        up = p.upper()
        if all(c in "0123456789ABCDEF" for c in up) and len(up) >= 2:
            hexes.append(up)
        else:
            words.append(to_simple(p.lower()))
    hits = []
    for k, name in names.items():
        sid = f"{k:04X}"
        nname = to_simple(name).lower()
        if hexes and not all(h in sid for h in hexes):
            continue
        if words and not all(w in nname for w in words):
            continue
        if not hexes and not words:
            continue
        hits.append((k, name))
    hits.sort(key=lambda x: x[0])
    return hits[:limit]


def search_items(keyword: str, names: dict[int, str], limit: int = 200) -> list[tuple[int, str]]:
    """按关键字模糊匹配装备名（简繁归一），用于物品下拉的过滤。"""
    raw = (keyword or "").strip().lower()
    if not raw:
        return []
    kw = to_simple(raw)
    hits = []
    for k, name in names.items():
        if kw in to_simple(name).lower() or raw in f"{k:04x}":
            hits.append((k, name))
    hits.sort(key=lambda x: x[0])
    return hits[:limit]


def is_skill_kind(kind: int) -> bool:
    """效果ID是否为技能道具（上游 IsSkillItem 的判定核心）。"""
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
        # 通用道具箱（500 格，装装备 + 消耗品）
        box_items = []
        for bi in range(BOX_SLOT_COUNT):
            bo = box_offset(slot, bi)
            iid = read_u16(data, bo)
            if not box_used(data, bo):
                continue
            if iid == CONSUMABLE_ID:
                kind = consumable_kind(data, bo)
                tag = "[技能]" if is_skill_kind(kind) else ""
                box_items.append(f"[{bi}]{tag} {consumable_label(data, bo, cons)}"
                                 f"{consumable_qty_text(data, bo)}")
            elif 0xBB <= iid <= 0x1E8:
                ms = active_mods(data, bo)
                lo, hi = item_atk(data, bo)
                box_items.append(f"[{bi}]{grade(len(ms))}级 {item_name(iid, names)} "
                                 f"LV{item_level(data, bo)} {lo}-{hi}")
            else:
                # 未鉴定/异常态：iid 为 0 或其它，类型标记非空格
                box_items.append(f"[{bi}]未鉴定? iid={iid:#06x} type={read_u16(data, bo + 2):#06x}")
        print(f"  --- 通用道具箱（该存档所有角色共用，{len(box_items)}/{BOX_SLOT_COUNT} 格占用）---")
        for bi in range(0, len(box_items), 2):
            print("      " + "  ".join(box_items[bi:bi + 2]))
        # 随身携带道具/快捷栏（18 格）
        pouch_items = []
        for pi in range(POUCH_SLOT_COUNT):
            po = pouch_offset(slot, pi)
            if not pouch_used(data, po):
                continue
            iid = read_u16(data, po)
            if iid == CONSUMABLE_ID:
                pouch_items.append(f"[{pi}] {consumable_label(data, po, cons)}"
                                  f"{consumable_qty_text(data, po)}")
            else:
                pouch_items.append(f"[{pi}] iid={iid:#06x} type={read_u16(data, po + 2):#06x}")
        print(f"  --- 随身携带道具/快捷栏（{len(pouch_items)}/{POUCH_SLOT_COUNT} 格占用，@"
              f"{pouch_offset(slot, 0):#x}）---")
        for pi in range(0, len(pouch_items), 3):
            print("      " + "  ".join(pouch_items[pi:pi + 3]))
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
    if args.id != CONSUMABLE_ID and not (ITEM_ID_ALLOW_MIN <= args.id <= ITEM_ID_ALLOW_MAX):
        raise SystemExit(f"物品ID {args.id:#x} 超出可写范围 "
                         f"{ITEM_ID_ALLOW_MIN:#x}-{ITEM_ID_ALLOW_MAX:#x}（或 0x70 消耗品）")
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
    """修改通用道具箱某格：装备可改物品ID/词条/等级/攻击/耐久，道具改效果ID/数量。"""
    if not (0 <= args.index < BOX_SLOT_COUNT):
        raise SystemExit(f"道具箱格号必须是 0-{BOX_SLOT_COUNT - 1}")
    o = box_offset(args.slot, args.index)
    iid = read_u16(data, o)
    buf = bytearray(data)

    if args.item is not None:
        if not (ITEM_ID_ALLOW_MIN <= args.item <= ITEM_ID_ALLOW_MAX) and args.item != CONSUMABLE_ID:
            raise SystemExit(f"物品ID {args.item:#x} 超出可写范围")
        if iid == CONSUMABLE_ID and args.item != CONSUMABLE_ID:
            # 从消耗品换成装备：类型标记 0x2200 -> 0x2A00
            write_u16(buf, o + 0x02, 0x2A00)
        print(f"改道具箱物品ID: [{args.index}] @{o:#x}  {iid:#06x} -> {args.item:#06x}")
        write_u16(buf, o, args.item)
        iid = args.item

    if iid == CONSUMABLE_ID:
        if args.kind is not None:
            mo = o + MOD_OFFSETS[0]      # 道具效果ID = 词条槽0
            print(f"改道具效果ID: [{args.index}] @{mo:#x}  {read_u16(data,mo):#06x} -> {args.kind:#06x}")
            write_u16(buf, mo, args.kind)
        if args.qty is not None:
            if not (0 <= args.qty <= 255):
                raise SystemExit("数量必须在 0-255")
            qo = o + QTY_OFFSET
            print(f"改数量: [{args.index}] @{qo:#x}  {read_u16(data,qo)&0xFF} -> {args.qty}")
            write_u16(buf, qo, (read_u16(data, qo) & 0xFF00) | args.qty)
    else:
        # 装备：复用 set-stat 的字段表
        fields = {
            "level": (o + F_ITEM_LEVEL, 1), "atkmin": (o + F_ITEM_ATK_MIN, 1),
            "atkmax": (o + F_ITEM_ATK_MAX, 1), "durcur": (o + F_DUR_CUR, 4),
            "durmax": (o + F_DUR_MAX, 4),
        }
        if args.field:
            fo, width = fields[args.field]
            v = args.value
            if args.field == "level":
                if not (1 <= v <= 255):
                    raise SystemExit("等级必须 1-255")
                v -= 1
            elif args.field in ("atkmin", "atkmax"):
                if not (0 <= v <= 255):
                    raise SystemExit("攻击力必须 0-255")
            else:
                frac = read_u32(data, fo) % DUR_SCALE
                v = v * DUR_SCALE + frac
            print(f"改装备{args.field}: [{args.index}] @{fo:#x} -> {args.value}")
            if width == 1:
                buf[fo] = v & 0xFF
            else:
                write_u32(buf, fo, v)
        if args.mod_index is not None:
            mo = o + MOD_OFFSETS[args.mod_index]
            print(f"改词条{args.mod_index+1}: [{args.index}] @{mo:#x}  "
                  f"{read_u16(data,mo):#06x} -> {args.mod_id:#06x}")
            write_u16(buf, mo, args.mod_id)

    if not any((args.item is not None, args.kind is not None, args.qty is not None,
                args.field, args.mod_index is not None)):
        raise SystemExit("至少给一个修改参数（--item/--kind/--qty/--field/--mod-index）")
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


def cmd_set_pouch(args, data, names):
    """改随身携带道具(快捷栏)某格。这块 18 格实测全是消耗品。"""
    if not (0 <= args.index < POUCH_SLOT_COUNT):
        raise SystemExit(f"快捷栏格号必须是 0-{POUCH_SLOT_COUNT - 1}")
    if args.kind is None and args.qty is None:
        raise SystemExit("至少给一个修改参数（--kind / --qty）")
    o = pouch_offset(args.slot, args.index)
    iid = read_u16(data, o)
    buf = bytearray(data)
    if args.kind is not None:
        if iid != CONSUMABLE_ID:
            # 空格/其他类型改成消耗品：补上物品ID与类型标记
            write_u16(buf, o, CONSUMABLE_ID)
            write_u16(buf, o + 0x02, 0x2200)
            print(f"  该格原为 {iid:#06x}，已补成消耗品格（iid=0x70, 类型=0x2200）")
            iid = CONSUMABLE_ID
        mo = o + MOD_OFFSETS[0]
        print(f"改快捷栏效果ID: DATA{args.slot+1} [{args.index}] @{mo:#x}  "
              f"{read_u16(data, mo):#06x} -> {args.kind:#06x}")
        write_u16(buf, mo, args.kind)
    if args.qty is not None:
        if not (0 <= args.qty <= 255):
            raise SystemExit("数量必须在 0-255")
        qo = o + QTY_OFFSET
        print(f"改快捷栏数量: DATA{args.slot+1} [{args.index}] @{qo:#x}  "
              f"{read_u16(data, qo) & 0xFF} -> {args.qty}")
        write_u16(buf, qo, (read_u16(data, qo) & 0xFF00) | args.qty)
    seal(buf)
    if args.dry_run:
        out = Path(args.out) if args.out else Path("SAVE0.modified.DAT")
        out.write_bytes(buf)
        print(f"[演练] 已写出到 {out}（原存档未动）")
    else:
        atomic_write(args.save, bytes(buf), backup=not args.no_backup)
    return 0


def cmd_search_mod(args, data, names):
    """按关键字模糊搜索词条 ID，列出 ID + 中文名（方便手填词条时查号）。"""
    zh = mod_names(zh=True)
    hits = search_mods(args.keyword, zh, limit=args.limit)
    if not hits:
        print(f"没有匹配「{args.keyword}」的词条。")
        return 0
    print(f"匹配「{args.keyword}」共 {len(hits)} 条（按ID升序，最多 {args.limit} 条）：")
    for k, n in hits:
        print(f"  {k:04X}  {n}")
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

    p = sub.add_parser("set-box", help="改通用道具箱某格（装备/消耗品）")
    p.add_argument("--slot", type=int, required=True, help="DATA slot 0-2")
    p.add_argument("--index", type=int, required=True, help=f"道具箱格 0-{BOX_SLOT_COUNT-1}")
    p.add_argument("--item", type=lambda s: int(s, 0), help="换成装备：物品ID, 如 0x105")
    p.add_argument("--kind", type=lambda s: int(s, 0), help="道具效果ID, 如 0x190=陨石卷轴")
    p.add_argument("--qty", type=int, help="消耗品数量 0-255")
    p.add_argument("--field", choices=["level", "atkmin", "atkmax", "durcur", "durmax"],
                   help="装备字段")
    p.add_argument("--value", type=int, help="配合 --field")
    p.add_argument("--mod-index", type=int, help="装备词条序号 0-7")
    p.add_argument("--mod-id", type=lambda s: int(s, 0), help="配合 --mod-index, FFFF=清空")
    p.add_argument("--out")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_set_box)

    p = sub.add_parser("set-pouch", help=f"改随身携带道具/快捷栏某格（0-{POUCH_SLOT_COUNT-1}）")
    p.add_argument("--slot", type=int, required=True, help="DATA slot 0-2")
    p.add_argument("--index", type=int, required=True, help=f"格号 0-{POUCH_SLOT_COUNT-1}")
    p.add_argument("--kind", type=lambda s: int(s, 0), help="道具效果ID, 如 0x190=陨石卷轴")
    p.add_argument("--qty", type=int, help="数量 0-255")
    p.add_argument("--out")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_set_pouch)

    p = sub.add_parser("search-mod", help="按关键字模糊查词条ID（如 攻击 / 幸运 / 0021）")
    p.add_argument("keyword", help="关键字，支持中文或十六进制ID片段")
    p.add_argument("--limit", type=int, default=80, help="最多列出多少条，默认 80")
    p.set_defaults(func=cmd_search_mod)

    args = ap.parse_args()
    if args.cmd == "search-mod":
        # 纯查表命令，不依赖存档
        return args.func(args, None, load_names(ITEMS_TSV)) or 0
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
