"""游戏静态目录（data/game_data.json）的加载与导入逻辑。

目录与用户状态严格分离（阶段13既定原则）：本模块只读目录、生成预填数据，
绝不直接改用户状态——落库由 main.py 的 API 在校验后执行。
"""
import json
import os

from .state import DATA_DIR

GAME_DATA_FILE = os.path.join(DATA_DIR, "game_data.json")

_MOB_FAMILY_TO_IDS = {
    "絮语": ("mob_whisper_c", "mob_whisper_b", "mob_whisper_a"),
    "掠影": ("mob_silhouette_c", "mob_silhouette_b", "mob_silhouette_a"),
    "数符": ("mob_numeral_c", "mob_numeral_b", "mob_numeral_a"),
    "幻妄": ("mob_delusion_c", "mob_delusion_b", "mob_delusion_a"),
}
_LOW_CONFIDENCE_KEYS = ("skill_confidence",)

# 通用养成数量（每角色相同、材料因角色而异的部分在这里给出数量；可校准）
COMMON_COST_DEFAULTS = {
    "char_exp": 325,                 # 角色 1→80 猎人攻略（特级口径）
    "ascension_boss": 86,            # 突破满级 BOSS 材料（每角色一只对应 BOSS）
    "ascension_mob": [17, 18, 15],   # 突破小怪素材三档（低/中/高）
    "skill_books": [40, 40, 64],     # 技能全满技能书三档
    "skill_weekly": 32,              # 技能周本素材
    "skill_mob": [40, 40, 64],       # 技能小怪素材三档
    "arc_exp": 365,                  # 弧盘 1→60 染剂（混沌当量）
    "arc_cans": [14, 18, 18],        # 弧盘突破罐头三档
    "arc_mob": [14, 18, 18],         # 弧盘小怪素材三档
    "coin_char": 2150000,            # 角色升级+突破甲硬币（已含突破段52.5万）
    "coin_skill": 1750000,           # 技能全满甲硬币
    "coin_arc": 1510000,             # 弧盘全程甲硬币
}


def load():
    with open(GAME_DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


def find_character(data, name):
    for entry in data.get("characters", []):
        if entry.get("name") == name:
            return entry
    raise ValueError(f"游戏目录中没有角色『{name}』")


def character_material_mapping(data, entry, costs):
    """单个角色条目 → 具体实名材料的通用数量映射（boss/weekly/mobs 三类；同一处真相，
    供『按目录导入角色』与『通用材料表一键应用』共用）。costs 为（可校准的）通用数量。

    返回 {mat_id: count} 与跳过说明列表；数量为 0 或映射缺失的类目跳过。
    """
    extra = {}
    skipped = []
    boss = entry.get("boss_material")
    if boss and boss.get("mat_id") and boss.get("total"):
        extra[boss["mat_id"]] = int(costs.get("ascension_boss", 86))
    elif entry.get("live"):
        skipped.append("BOSS 材料（归属未查到）")
    weekly = entry.get("weekly_material")
    if weekly and weekly.get("mat_id") and weekly.get("total"):
        extra[weekly["mat_id"]] = int(costs.get("skill_weekly", 32))
    family_ids = _MOB_FAMILY_TO_IDS.get(entry.get("mob_family") or "", ("", "", ""))
    slots = ("c", "b", "a")
    has_mob = any(int((entry.get(s) or {}).get(k) or 0) > 0
                  for s in ("mob_breakthrough", "mob_skill") for k in slots)
    if has_mob and all(family_ids):
        asc = costs.get("ascension_mob") or [0, 0, 0]
        skl = costs.get("skill_mob") or [0, 0, 0]
        for slot, mat_id in zip(slots, family_ids):
            total = int(asc[slots.index(slot)] or 0) + int(skl[slots.index(slot)] or 0)
            if total > 0:
                extra[mat_id] = extra.get(mat_id, 0) + total
    elif entry.get("live"):
        skipped.append("小怪素材（族群/数量未查到）")
    return extra, skipped


def build_character_import(data, name):
    """按目录生成一个预填角色行（返回原始 dict，交由 API 的 _sanitize_character 校验落库）。

    预填内容：属性→技能书系（仅目录给出时）、物态→罐头系（弧盘材料）、
    专武（下拉默认）、直接材料需求（BOSS 材料×总量 + 周本×总量 + 小怪素材
    突破/技能段合计）。低置信与未查到的字段一律不编造。
    """
    entry = find_character(data, name)
    from .state import new_character  # 局部导入避免环

    char = new_character(entry["name"])
    char["enabled"] = True
    char["attribute"] = entry.get("attribute")
    char["matter_state"] = entry.get("matter_state")
    char["game_confidence"] = {
        "skill": entry.get("skill_confidence"),
        "boss": (entry.get("boss_material") or {}).get("confidence"),
        "notes": entry.get("_note", ""),
    }
    # 属性→技能书系（仅目录映射给出时才设置，绝不猜）
    series = data.get("attribute_to_skill_series", {}).get(entry.get("attribute"))
    if series and entry.get("skill_series"):
        char["mats"]["ability"] = int(series.rsplit("_", 1)[1])
    # 物态→罐头系
    can = data.get("matter_state_to_can", {}).get(entry.get("matter_state"))
    if can:
        char["mats"]["arc"] = int(can.rsplit("_", 1)[1])
    # 专武下拉默认
    if entry.get("signature_weapon"):
        char["weapon"] = entry["signature_weapon"]
    # 直接材料需求：boss/周本/小怪（与『通用材料表一键应用』同一映射真相）
    extra, _skipped = character_material_mapping(data, entry, dict(COMMON_COST_DEFAULTS))
    char["extra_needs"] = extra
    return char


def library_names(data):
    """角色库/武器库导入用名单：仅已实装角色（未上线不入库——不可养成），
    角色名为干净值（不带任何后缀标记）；带标记的名单只用于『按目录导入角色』下拉。"""
    char_names, weapon_names = [], []
    for entry in data.get("characters", []):
        if entry.get("live"):
            char_names.append(entry["name"])
    for entry in data.get("weapons", []):
        weapon_names.append(entry["name"])
    return char_names, weapon_names
