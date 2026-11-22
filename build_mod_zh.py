#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 save_mods.zh.tsv：附加ID -> 中文名。

优先用参考 xlsx 提取的中文效果名；没有的用上游英文表按日式游戏常用译法直译。
译法先求"意思对"，数值差太多以后再逐条改。可重复运行，纯标准库。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
EN_JSON = HERE / "_upstream_en_CustomItem.json"
AFFIX_TSV = HERE / "save_affixes.tsv"
OUT = HERE / "save_mods.zh.tsv"

# 常见前缀/后缀形容词：日式游戏惯用译法
ADJ = {
    "Pristine": "澄澈的", "Valkyrie's": "女武神的", "Wyrmsbane": "屠龙的",
    "Sage's": "贤者的", "Branding": "灼印的", "Auspicious": "吉祥的",
    "Convective": "对流的", "Resistant": "抗性的", "Knave's": "盗贼的",
    "Smothering": "窒息的", "Dependable": "可靠的", "Butcher's": "屠夫的",
    "Stout": "坚固的", "Fool's": "愚者的", "Purifying": "净化的",
    "Empowering": "强化的", "Astute": "敏锐的", "Covering": "掩护的",
    "Trapper's": "陷阱师的", "Hunter's": "猎人的", "Blessed": "祝福的",
    "Fortunate": "幸运的", "Serpent-eyed": "蛇眼的", "Cerebral": "洞察的",
    "Ordained": "神圣的", "Crushing": "碾压的", "Augurer": "占卜师的",
    "Lonely": "孤独的", "Despairing": "绝望的", "Unassuming": "朴素的",
    "Sapper's": "工兵的", "Illegal": "无效的", "Unbroken": "完好的",
    "Booster": "增幅的", "Mighty": "强力的", "Bomber's": "爆破手的",
    "Protective": "保护的", "Robust": "强健的", "Warrior's": "战士的",
    "Dragon's": "龙之", "Jotun's": "巨人的", "Devil's": "恶魔的",
    "Smothering's": "窒息的", "Stone Wall": "铁壁的", "Vanguard's": "先锋的",
    "Sprightly": "轻快的", "Cannon": "炮击的", "Ionic": "离子的",
    "Knave": "盗贼的", "Wyrm": "龙之", "Simple": "朴素的",
}

# 效果描述常用词（从上到下替换，长的在前）
TERMS = [
    ("Item degradation reduced by", "耐久度消耗率降低"),
    ("Damage dealt", "造成的伤害"), ("Damage taken", "受到的伤害"),
    ("Max HP", "HP最大值"), ("Critical rate", "暴击率"),
    ("Magic resistance", "魔法抗性"), ("magic resistance", "魔法抗性"),
    ("Fire resistance", "火焰抗性"), ("Ice resistance", "冰霜抗性"),
    ("Lightning resistance", "雷电抗性"),
    ("counter with fire", "以火焰弹反击"), ("counter with ice", "以冰霜弹反击"),
    ("counter with lightning", "以雷电弹反击"), ("counter with magic", "以魔法弹反击"),
    ("Poison enemy", "使敌人中毒"), ("Ignite enemy", "使敌人燃烧"),
    ("Freeze enemy", "使敌人冻结"), ("Petrify enemy", "使敌人石化"),
    ("immune to poison", "免疫中毒"), ("Immune to poison", "免疫中毒"),
    ("immune to burn", "免疫燃烧"), ("Immune to burn", "免疫燃烧"),
    ("immune to freeze", "免疫冻结"), ("Immune to freeze", "免疫冻结"),
    ("enemies", "敌人"), ("enemy", "敌人"), ("chance", "概率"),
    ("arrows on stage transition", "变换场景时箭矢数量"),
    ("arrow", "箭矢"), ("damage", "伤害"), ("Speed", "速度"),
    ("item", "物品"), ("gold", "金币"), ("HP", "HP"),
    ("MP", "MP"), ("STR", "STR"), ("INT", "INT"), ("LUC", "LUC"),
    ("AGI", "AGI"), ("VIT", "VIT"),
]


def cleanup(s: str) -> str:
    s = re.sub(r"@c\(\d+\)", "", s)
    s = s.replace("@/c", "").replace("%d％", "%").replace("%d", "")
    s = s.replace("％", "%").replace("@", "").strip()
    return re.sub(r"\s+", " ", s)


def translate(en: str) -> str:
    s = cleanup(en)
    if s in ADJ:
        return ADJ[s]
    if s.endswith("'s") and s[:-2] in ADJ:
        return ADJ[s[:-2]] + "的"
    out = s
    for a, b in TERMS:
        out = re.sub(re.escape(a), b, out, flags=re.IGNORECASE)
    # 仍未译出的纯英文，标一下来源，方便以后逐条修
    if re.fullmatch(r"[A-Za-z0-9 '%.,()+\-/]+", out) and out:
        return out + "（待校）"
    return out or s


def main():
    import sys
    src = HERE.parent / "_upstream_src/localization/en/sheets/CustomItem.json"
    if not EN_JSON.exists():
        data = json.loads(src.read_text(encoding="utf-8"))
        (HERE / "_upstream_en_CustomItem.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
        print(f"已缓存英文表 -> {EN_JSON.name}")
    else:
        data = json.loads(EN_JSON.read_text(encoding="utf-8"))

    zh = {}
    if AFFIX_TSV.exists():
        for line in AFFIX_TSV.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            p = line.split("\t", 1)
            if len(p) == 2:
                try:
                    zh[int(p[0], 16)] = p[1].strip()
                except ValueError:
                    pass

    with OUT.open("w", encoding="utf-8", newline="\n") as f:
        f.write("# 附加ID -> 中文名（xlsx优先，其余按日式游戏惯用译法直译）\n")
        for e in data["entries"]:
            i = e["index"]
            f.write(f"{i:04X}\t{zh.get(i) or translate(e['en'])}\n")
    print(f"已写出 {OUT.name}，共 {len(data['entries'])} 条")


if __name__ == "__main__":
    main()
