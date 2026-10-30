"""游戏静态目录（data/game_data.json）的加载与导入逻辑。

目录与用户状态严格分离（阶段13既定原则）：本模块只读目录、生成预填数据，
绝不直接改用户状态——落库由 main.py 的 API 在校验后执行。

图鉴覆盖层（阶段19）：用户在「图鉴数据」页手动录入/更新的角色与武器写入
data/game_data_custom.json（按 name 覆盖合并，绝不写回基础 game_data.json），
因此不会被未来的 mysqil_ingest 或官方数据更新冲掉。
"""
import json
import os

from .state import DATA_DIR, load_json, save_json_atomic

GAME_DATA_FILE = os.path.join(DATA_DIR, "game_data.json")
CUSTOM_FILE = os.path.join(DATA_DIR, "game_data_custom.json")
CUSTOM_KINDS = ("characters", "weapons")

_MOB_FAMILY_TO_IDS = {
    "絮语": ("mob_whisper_c", "mob_whisper_b", "mob_whisper_a"),
    "掠影": ("mob_silhouette_c", "mob_silhouette_b", "mob_silhouette_a"),
    "数符": ("mob_numeral_c", "mob_numeral_b", "mob_numeral_a"),
    "幻妄": ("mob_delusion_c", "mob_delusion_b", "mob_delusion_a"),
}
_LOW_CONFIDENCE_KEYS = ("skill_confidence",)

# 通用养成数量（每角色相同、材料因角色而异的部分在这里给出数量；可校准）
COMMON_COST_DEFAULTS = {
    # 阶段17：mysqil 环世全鉴权威数值（白当量口径，紫:蓝:白=9:3:1 折算）
    "char_exp": 6496000,             # 角色 1→80 经验点（每局 22.5万@鉴别7）
    "ascension_boss": 86,            # 突破满级 BOSS 材料（每角色一只对应 BOSS）
    "ascension_mob": 206,            # 突破小怪素材（白当量，写入族群基础档）
    "skill_books": 199,              # 技能 1→10 白书 184 + 天赋解锁 15（白当量）
    "skill_weekly": 11,              # 技能周本素材（技能段8+天赋3）
    "skill_mob": 184,                # 技能小怪素材（白当量，写入族群基础档）
    "arc_exp": 1240000,              # 弧盘 1→60 经验点（每局 12.5万@鉴别7；1→80 为 3727000）
    "arc_cans": 230,                 # 弧盘突破罐头（白当量，S级口径，走练度轨道）
    "arc_mob": 230,                  # 弧盘小怪素材（白当量，写入族群基础档）
    "coin_char": 2148212,            # 角色升级+突破甲硬币（mysqil 逐段合计）
    "coin_skill": 555000,            # 技能全满甲硬币（逐级48.5万+天赋7万）
    "coin_arc": 1207264,             # 弧盘 1→80 甲硬币（mysqil weaponLevelExp 合计）
}


def load():
    with open(GAME_DATA_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return _merge_custom(data)


def _merge_custom(data):
    """把图鉴覆盖层按 name 合并进目录副本：同名覆盖（如 1.5 上线把未上线角色翻 live）、
    新名字追加；基础 game_data.json 永不被修改。覆盖层缺失/损坏时静默跳过。"""
    custom = load_json(CUSTOM_FILE)
    if not isinstance(custom, dict):
        return data
    for kind in CUSTOM_KINDS:
        entries = custom.get(kind)
        if not isinstance(entries, list):
            continue
        base_list = data.setdefault(kind, [])
        index = {e.get("name"): i for i, e in enumerate(base_list)
                 if isinstance(e, dict) and e.get("name")}
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            name = entry["name"]
            if name in index:
                base_list[index[name]] = entry
            else:
                base_list.append(entry)
                index[name] = len(base_list) - 1
    return data


def custom_entries():
    """覆盖层原始内容（供 /api/game-data 展示与 UI 编辑回填）。"""
    doc = load_json(CUSTOM_FILE)
    if not isinstance(doc, dict):
        doc = {}
    return {"version": doc.get("version", 1),
            "characters": doc.get("characters") or [],
            "weapons": doc.get("weapons") or []}


def upsert_custom(kind, entry):
    """新增/更新一条覆盖层条目（kind ∈ characters/weapons；按 name 覆盖）。"""
    if kind not in CUSTOM_KINDS:
        raise ValueError(f"kind 必须是 {'/'.join(CUSTOM_KINDS)}")
    name = entry.get("name")
    if not name:
        raise ValueError("条目缺少 name")
    doc = custom_entries()
    lst = doc.setdefault(kind, [])
    for i, existing in enumerate(lst):
        if existing.get("name") == name:
            lst[i] = entry
            break
    else:
        lst.append(entry)
    save_json_atomic(CUSTOM_FILE, doc)
    return entry


def remove_custom(kind, name):
    """从覆盖层删除一条；返回是否真的删除了（不存在的名字不算错误）。"""
    if kind not in CUSTOM_KINDS:
        raise ValueError(f"kind 必须是 {'/'.join(CUSTOM_KINDS)}")
    doc = custom_entries()
    lst = doc.setdefault(kind, [])
    kept = [e for e in lst if e.get("name") != name]
    if len(kept) == len(lst):
        return False
    doc[kind] = kept
    save_json_atomic(CUSTOM_FILE, doc)
    return True


def find_character(data, name):
    for entry in data.get("characters", []):
        if entry.get("name") == name:
            return entry
    raise ValueError(f"游戏目录中没有角色『{name}』")


def mob_base_id(entry):
    """角色族群 → 小怪素材基础档 id（突破/技能升级共用）；族群未知返回 None。"""
    if not isinstance(entry, dict):
        return None
    return _MOB_FAMILY_TO_IDS.get(entry.get("mob_family") or "", ("",))[0] or None


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
        extra[weekly["mat_id"]] = int(costs.get("skill_weekly", 11))
    base_id = _MOB_FAMILY_TO_IDS.get(entry.get("mob_family") or "", ("",))[0]
    # 小怪素材需求成立的证据：族群已知，且有阶段14 逐档计数或阶段17 mysqil 白当量合计
    # （ingest 只写 mob_base_total 标量、无逐档键——只查逐档键会让 20/25 名角色被整类跳过）
    has_mob = bool(entry.get("mob_family")) and (
        any(int((entry.get(s) or {}).get(k) or 0) > 0
            for s in ("mob_breakthrough", "mob_skill") for k in ("c", "b", "a"))
        or entry.get("mob_base_total") is not None)
    if has_mob and base_id:
        # mysqil 白当量合计写入族群基础档（白书/白怪为折算基准单位，紫:蓝:白=9:3:1）
        total = int(costs.get("ascension_mob", 206) or 0) + int(costs.get("skill_mob", 184) or 0)
        if total > 0:
            extra[base_id] = extra.get(base_id, 0) + total
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
    # 技能书系：优先角色个体值（mysqil 权威，存在跨属性例外），属性多数派仅作后备
    series = entry.get("skill_series") or data.get("attribute_to_skill_series", {}).get(
        entry.get("attribute"))
    if series:
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
    # 都市特技：wiki 已抓取快照存在时按角色预填名字与槽位数（实机数据决定 1 或 2 位，
    # 满足"根据角色实际情况开放位置"）；等级留 0 由用户填。未抓取/未命中不预填，绝不编造。
    try:
        from .wiki_fetch import city_talents_by_name  # 局部导入避免环
        talents = city_talents_by_name().get(entry["name"]) or []
    except Exception:  # noqa: BLE001 —— 快照缺失/损坏时静默跳过预填
        talents = []
    if talents:
        char["city_talents"] = [{"name": str(t.get("name", ""))[:32], "lv": 0}
                                for t in talents[:2] if t.get("name")]
        if char["city_talents"]:
            char["city_talent_slots"] = 2 if len(char["city_talents"]) > 1 else 1
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
