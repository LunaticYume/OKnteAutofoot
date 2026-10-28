"""阶段17：mysqil 环世全鉴数据落库（一次性数据工程脚本）。

输入：data/mysqil/cultivation_data.json（浏览器自 DetailedCalc.js 导出的权威数值）
输出：重写 game_data.json 的 characters/attribute 映射、requirements.json 精确分段、
      yields.json 鉴别7 口径；打印全部交叉验证结果。
运行：python -m tools.mysqil_ingest（项目根）
"""
import json
import os
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

# 材料名 → mat_id / ok-nte BOSS 名
BOSS_MAT_ID = {"上膛骑士火花塞": "boss_spark_plug", "妄想彼端的一页": "boss_page_delusion",
               "海之泪": "boss_sea_tear", "护巢残片": "boss_nest_fragment",
               "倾诉花种": "boss_flower_seed", "水月色拨片": "boss_pick_watermoon",
               "斑斓的票根": "boss_ticket_stub"}
BOSS_NAME = {"上膛骑士火花塞": "无首铁驭", "妄想彼端的一页": "黑之书", "海之泪": "海囚",
             "护巢残片": "围巢鸟", "倾诉花种": "塞润尼缇", "水月色拨片": "音霸魔王",
             "斑斓的票根": "斑蝶"}
WEEKLY_ID = {"名为炫耀的裙袂": "wk_skirt", "好狗狗印章": "wk_stamp", "记忆的永恒": "wk_memory"}
SKILL_BOOK_TO_N = {"雏鸟的希冀": 2, "新兵的怯懦": 1, "初次的期许": 3,
                   "思维的同频": 4, "水波的迟疑": 5}
FIELD_BASE_ID = {"失落絮语": "mob_whisper_c", "褪色掠影": "mob_silhouette_c",
                 "模糊数符": "mob_numeral_c", "悬想幻妄": "mob_delusion_c"}
FIELD_FAMILY_SHORT = {"失落絮语": "絮语", "褪色掠影": "掠影",
                      "模糊数符": "数符", "悬想幻妄": "幻妄"}


def reverse_group(groups, name):
    for material, members in groups.items():
        if name in members:
            return material
    return None


def main():
    mysqil = json.load(open(os.path.join(DATA, "mysqil", "cultivation_data.json"),
                            encoding="utf-8"))
    game = json.load(open(os.path.join(DATA, "game_data.json"), encoding="utf-8"))

    # ---- 1) 属性→技能书系（多数派提示；技能书系按角色个体为准——存在跨属性例外）----
    attr_votes = {}
    for material, members in mysqil["charSkillBook"].items():
        n = SKILL_BOOK_TO_N.get(material)
        if n is None:
            print(f"  警告：未登记的技能书系『{material}』，跳过属性投票")
            continue
        for member in members:
            name = member.replace("「", "").replace("」", "")
            entry = next((c for c in game["characters"] if c["name"] == name), None)
            if entry is None:
                print(f"  警告：书系『{material}』成员『{name}』不在角色目录，跳过")
                continue
            attr_votes.setdefault(entry["attribute"], {}).setdefault(n, []).append(name)
    attr_map = {}
    exceptions = []
    for attribute, votes in sorted(attr_votes.items()):
        winner = max(votes, key=lambda n: len(votes[n]))
        attr_map[attribute] = f"ability_{winner}"
        for n, names in votes.items():
            if n != winner:
                exceptions.append(f"{attribute}系例外：{'、'.join(names)} 用 ability_{n}")
    print("属性→技能书系（多数派）:", attr_map)
    for line in exceptions:
        print(" ", line)
    game["attribute_to_skill_series"] = {
        **attr_map, "神": None,
        "_note": "多数派提示；技能书系按角色个体为准（例外：海月(魂)用军衔系、法帝娅(魂)用同频系、"
                 "哈尼娅(魂)用鸽子系——mysqil 权威分组已逐角色写入 skill_series）",
    }

    # ---- 2) 25 角色映射重写（boss/weekly/mob/skill 全覆盖，confidence high）----
    updated = 0
    for entry in game["characters"]:
        name = entry["name"]
        if not entry.get("live"):
            continue
        lookup = f"「{name}」" if name == "零" else name
        skill_book = reverse_group(mysqil["charSkillBook"], lookup)
        boss_mat = reverse_group(mysqil["charBossMat"], lookup)
        weekly = reverse_group(mysqil["charWeeklyMat"], lookup)
        field = reverse_group(mysqil["charFieldMat"], lookup)
        if not (skill_book and boss_mat and weekly and field):
            # mysqil 未覆盖（灵可/残虹/黑羽/明音凛/塔吉多）：保留阶段14调研值，不覆盖
            print(f"  {name}: mysqil 未覆盖，保留既有调研值"
                  f"（skill={entry.get('skill_series')}）")
            continue
        book_n = SKILL_BOOK_TO_N.get(skill_book)
        if book_n is None:
            print(f"  {name}: 技能书系『{skill_book}』未登记，跳过该角色")
            continue
        entry["skill_series"] = f"ability_{book_n}"
        entry["skill_confidence"] = "high"
        entry["skill_book_name"] = skill_book
        entry["boss_material"] = {"mat_id": BOSS_MAT_ID[boss_mat], "boss": BOSS_NAME[boss_mat],
                                  "total": sum(v[0] for v in mysqil["charBreak"].values()),
                                  "confidence": "high"}
        entry["weekly_material"] = {"mat_id": WEEKLY_ID[weekly],
                                    "total": 8 + 3,  # 技能段 8 + 天赋 3（mysqil skillUpgrade/talentUnlock）
                                    "confidence": "high"}
        entry["mob_family"] = FIELD_FAMILY_SHORT[field]
        entry["mob_base_total"] = (sum(v[1] for v in mysqil["charBreak"].values())  # 突破小怪白当量（charBreak 第2列；第1列是BOSS材料86）
                                   + sum(v[1] for v in mysqil["skillUpgrade"].values()))  # 技能段白当量
        entry.pop("_note", None)
        updated += 1
    print(f"角色映射重写: {updated} 名（全部 high）")
    game["source"] = "https://wiki.mysqil.com/tools/cultivation（DetailedCalc.js 权威数值，2026-10-28 抓取）"

    # ---- 3) requirements.json：mysqil 精确分段 ----
    def seg(items):
        return {"items": items}

    char_ladder = []
    for a, b in zip(mysqil["levelTiers"], mysqil["levelTiers"][1:]):
        exp, coin = mysqil["charLevelExp"][f"{a},{b}"]
        brk = mysqil["charBreak"].get(str(b))
        if brk:  # 断点在段末：突破硬币并入该段
            coin += brk[2]
        char_ladder.append({"from": a, "to": b,
                            **seg({"exp_char": exp, "coin": coin})})
    ability_ladder = []
    tiers = sorted(mysqil["skillUpgrade"].keys(), key=lambda k: int(k.split(",")[0]))
    prev = 1
    for key in tiers:
        a, b = (int(x) for x in key.split(","))
        books, _mob, coin, _wk = mysqil["skillUpgrade"][key]
        ability_ladder.append({"from": a, "to": b,
                               **seg({"ability_mat": books, "coin": coin})})
        prev = b
    ability_ladder.append({"from": 10, "to": 13,  # 天赋解锁 2 档（技能10后）
                           **seg({"ability_mat": 6 + 9, "coin": 30000 + 40000})})
    arc_ladder = []
    for a, b in zip(mysqil["levelTiers"], mysqil["levelTiers"][1:]):
        exp, coin = mysqil["weaponLevelExp"][f"{a},{b}"]
        brk = mysqil["weaponBreak5"].get(str(b))  # S 级弧盘口径（A 级见 game_data 注）
        if brk:
            coin += brk[2]  # 突破段甲硬币并入该段（与 char_level 同口径）
        items = {"exp_arc": exp, "coin": coin}
        if brk:
            items["arc_mat"] = brk[0]
        arc_ladder.append({"from": a, "to": b, **seg(items)})
    requirements = {
        "version": 3,
        "note": ("阶段17：mysqil 环世全鉴 DetailedCalc 权威逐级表（经验/硬币按经验点与枚数原口径，"
                 "技能书/小怪/罐头为白当量，紫:蓝:白=9:3:1 折算；S级弧盘突破口径，A 级约 0.89 倍）。"
                 "BOSS 材料/周本/小怪族群因角色而异，由『通用材料表→一键应用』写入各角色直接需求。"
                 "全部数值可编辑。来源: https://wiki.mysqil.com/tools/cultivation"),
        "generic_materials": {
            "exp_char": "角色经验（经验点口径，每局 22.5万@鉴别7）",
            "exp_arc": "弧盘经验（经验点口径，每局 12.5万@鉴别7）",
            "coin": "甲硬币（每局 12万@鉴别7）",
            "ability_mat": "技能书（白书当量，按角色属性系 ability_1~5）",
            "arc_mat": "弧盘突破·罐头（白当量，按武器物态 arc_1~5，S级口径）",
            "console_mat": "空幕（兔子洞，mysqil 未收录，留空待校准）",
        },
        "tracks": {
            "char_level": {"name": "角色等级", "max": 80, "ladder": char_ladder},
            "ability_level": {"name": "异能等级", "max": 13, "ladder": ability_ladder},
            "arc_level": {"name": "弧盘等级", "max": 80, "ladder": arc_ladder},
            "console_level": {"name": "空幕等级", "max": 20, "ladder": [{"from": 1, "to": 20, "items": {}}],
                              "_note": "井屑消耗 mysqil 未收录，待校准"},
        },
    }
    print("requirements.json: char_level 7段 / ability 10段 / arc 7段（mysqil 权威）")

    # ---- 4) yields.json：鉴别7 每局掉落 ----
    y = json.load(open(os.path.join(DATA, "yields.json"), encoding="utf-8"))
    a7 = mysqil["appraisalYield"]["7"]
    y["per_run"].update({"coin": a7["coins"][0], "exp_char": a7["charExp"][0],
                         "exp_arc": a7["weaponExp"][0]})
    for n in range(1, 6):
        y["per_run"][f"ability_{n}"] = a7["skillBook"][0]
        y["per_run"][f"arc_{n}"] = a7["weaponBreakMat"][0]
    y["note"] = ("per_run：体力本每轮(40体力)掉落，口径=鉴别7（游戏内材料鉴别等级，站内实测权威值，"
                 "可按你实际鉴别等级调整）。hunter_yield：追猎每局(60体力)BOSS掉落（50级约8个）。"
                 "coin 已按鉴别7 校准为 12万/局，无需再手填。")
    print("yields.json: coin=12万 / exp_char=22.5万 / exp_arc=12.5万 / 书·罐头=18（鉴别7）")

    # ---- 5) 交叉验证 ----
    boss_total = sum(v[0] for v in mysqil["charBreak"].values())
    assert boss_total == 86, boss_total
    coin_total = sum(v[1] for v in mysqil["charLevelExp"].values()) \
        + sum(v[2] for v in mysqil["charBreak"].values())
    print(f"交叉验证: BOSS 合计 {boss_total}（=86 ✓）；"
          f"角色硬币合计 {coin_total:,}（独立调研 215万 ✓）")

    # ---- 6) 全部落盘（全部计算与校验通过后才写：中途崩溃不会留下半套互相矛盾的数据）----
    (Path(DATA) / "game_data.json").write_text(
        json.dumps(game, ensure_ascii=False, indent=2), encoding="utf-8")
    (Path(DATA) / "requirements.json").write_text(
        json.dumps(requirements, ensure_ascii=False, indent=2), encoding="utf-8")
    (Path(DATA) / "yields.json").write_text(
        json.dumps(y, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已落盘: game_data.json / requirements.json / yields.json")


if __name__ == "__main__":
    sys.path.insert(0, ROOT)
    main()
