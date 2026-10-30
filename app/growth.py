"""角色成长材料模型（数据源：data/mysqil/cultivation_data.json，环世全鉴权威快照）。

口径与 wiki 养成计算器（DetailedCalc.js）逐行核对，并按用户实机规则修正：
- 四技能（普攻/变轨/极轨/援护）各自独立 1→10：每段消耗 [技能书, 族群小怪, 硬币, 周本]
  （skillUpgrade 段 "1,2".."9,10"，单技能 1→10 合计 书184/小怪184/硬币43.7万/周本8）；
- 天赋解锁 2 档（角色可选开启）：每档 [书, 硬币, 周本] = [6,3万,1] / [9,4万,2]（talentUnlock）；
- **突破材料挂角色等级门槛，与觉醒档位无关**（用户实机口径 + wiki 角色页对位核实）：
  30/40/50/60/70 满级后突破到 31/41/51/61/71，另有满级 80 的最终突破（突破6）。
  等级路径（tracks.char_level cur→tgt）跨越门槛时计入对应 charBreak 档的 BOSS+族群
  小怪（wiki 对位：Lv X 突破 = charBreak[X-10]）；硬币已包含在角色等级梯内
  （mysqil 逐段合计），门槛处不重复加。觉醒档位（0-6）是独立记录字段，不参与计算。

旧 requirements.ability_level 1→13 单轨是错误口径（四技能只计了 1 份书、天赋硬拼在
10→13 段），由本模块取代其计算职能；旧梯数据保留在 requirements.json 不删除。
都市特技升级消耗 CityAbility_UpMaterial+fons（方斯），均非体力本材料，不纳入体力计划。
"""
import json
import os

from . import game_data
from .state import DATA_DIR

CULTIVATION_FILE = os.path.join(DATA_DIR, "mysqil", "cultivation_data.json")

SKILL_KEYS = ("normal", "variable", "orbit", "assist")  # 普攻/变轨/极轨/援护
AWAKENING_MAX = 6  # 觉醒档位 0-6（独立记录字段，与突破材料无关）
# 突破等级门槛（用户实机口径：满级后突破到下一个十级，如 30→31）；
# wiki charBreak 还有 20 档一行（0 BOSS/5 小怪/2.5万硬币），按实机口径不计入
LEVEL_GATES = (30, 40, 50, 60, 70)

_DATA = None


def _load():
    global _DATA
    if _DATA is None:
        with open(CULTIVATION_FILE, encoding="utf-8") as f:
            _DATA = json.load(f)
    return _DATA


def reload():
    """数据文件被外部更新后强制重读（测试/维护用）。"""
    global _DATA
    _DATA = None


def skill_max():
    """技能等级上限：由 wiki skillUpgrade 覆盖的最高段推导（当前数据为 10），
    不再使用前端/后端硬编码。"""
    su = _load().get("skillUpgrade", {})
    return max((int(k.split(",")[1]) for k in su), default=10)


def skill_segment_cost(cur, tgt):
    """单个技能 cur→tgt 的逐级累计消耗 {skill_book, field_mat, coins, weekly}。"""
    su = _load().get("skillUpgrade", {})
    top = skill_max()
    total = {"skill_book": 0, "field_mat": 0, "coins": 0, "weekly": 0}
    for lv in range(max(1, cur), min(tgt, top)):
        seg = su.get(f"{lv},{lv + 1}")
        if seg:
            total["skill_book"] += int(seg[0])
            total["field_mat"] += int(seg[1])
            total["coins"] += int(seg[2])
            total["weekly"] += int(seg[3])
    return total


def talent_cost(tiers):
    """天赋解锁：tiers 为开启的档位（1/2）；每档 [书, 硬币, 周本]。"""
    tu = _load().get("talentUnlock", {})
    total = {"skill_book": 0, "coins": 0, "weekly": 0}
    for t in tiers:
        seg = tu.get(str(t))
        if seg:
            total["skill_book"] += int(seg[0])
            total["coins"] += int(seg[1])
            total["weekly"] += int(seg[2])
    return total


BREAKTHROUGH_LEVELS = (20, 30, 40, 50, 60, 70)  # wiki charBreak 原始键
# 突破门槛（用户实机口径 + wiki 角色页对位核实）：满级 30/40/50/60/70 后突破到
# 31/41/51/61/71；另有满级 80 的最终突破（wiki 突破6）。
# wiki 对位关系：Lv X 的突破消耗 charBreak[X-10]（如 突破1 Lv30 = charBreak["20"]
# = 0 BOSS/5 小怪/2.5万硬币；突破6 Lv80 = charBreak["70"]）。六档合计 BOSS 86/小怪 206。
LEVEL_GATES = (30, 40, 50, 60, 70)
FINAL_GATE = 80


def breakthrough_gate_cost(gate):
    """等级门槛 gate 的突破材料 [BOSS, 族群小怪, 硬币]；wiki 对位 charBreak[gate-10]。"""
    cb = _load().get("charBreak", {})
    seg = cb.get(str(int(gate) - 10))
    return [int(seg[0]), int(seg[1]), int(seg[2])] if seg else None


def crossed_gates(cur, tgt):
    """等级路径 cur→tgt 的突破门槛：五档（停在门槛级去下一级需要材料，cur ≤ g < tgt）
    + 满级最终突破（cur < 80 ≤ tgt，突破6 不再涨级但消耗最终突破材料）。"""
    cur, tgt = int(cur), int(tgt)
    gates = [g for g in LEVEL_GATES if cur <= g < tgt]
    if cur < FINAL_GATE <= tgt:
        gates.append(FINAL_GATE)
    return gates


def _add(needs, mat_id, count):
    if mat_id and count > 0:
        needs[mat_id] = needs.get(mat_id, 0) + count


def character_growth_needs(character, entry):
    """单个角色的成长材料（mat_id → 数量）：四技能 + 天赋解锁 + 等级门槛突破。

    entry 为游戏目录条目（提供书系/族群/周本/BOSS 归属）；目录未收录的角色
    （entry=None）技能书仍按所选书系计算，族群/周本/BOSS 归属缺失的类别跳过
    （缺映射不编造）。技能材料书系用角色所选 mats.ability。
    突破材料与觉醒档位无关：由 tracks.char_level 的 cur→tgt 跨越 31/41/51/61/71
    门槛推导（BOSS+小怪；硬币已含在等级梯）。
    """
    needs = {}
    mats = character.get("mats") or {}
    book_id = f"ability_{int(mats.get('ability', 1) or 1)}"
    base_id = game_data.mob_base_id(entry) if entry else None
    weekly_id = ((entry or {}).get("weekly_material") or {}).get("mat_id")
    boss_id = ((entry or {}).get("boss_material") or {}).get("mat_id")

    skills = character.get("skills") or {}
    for key in SKILL_KEYS:
        block = skills.get(key) if isinstance(skills.get(key), dict) else {}
        cost = skill_segment_cost(int(block.get("cur") or 0), int(block.get("tgt") or 0))
        _add(needs, book_id, cost["skill_book"])
        _add(needs, base_id, cost["field_mat"])
        _add(needs, "coin", cost["coins"])
        _add(needs, weekly_id, cost["weekly"])

    unlocks = character.get("talent_unlocks")
    tiers = [i for i, on in enumerate(unlocks or [], 1) if on]
    cost = talent_cost(tiers)
    _add(needs, book_id, cost["skill_book"])
    _add(needs, "coin", cost["coins"])
    _add(needs, weekly_id, cost["weekly"])

    tracks = character.get("tracks") or {}
    levels = tracks.get("char_level") if isinstance(tracks.get("char_level"), dict) else {}
    try:
        lv_cur, lv_tgt = int(levels.get("cur", 0) or 0), int(levels.get("tgt", 0) or 0)
    except (TypeError, ValueError):
        lv_cur = lv_tgt = 0
    for gate in crossed_gates(lv_cur, lv_tgt):
        seg = breakthrough_gate_cost(gate)
        if seg:
            _add(needs, boss_id, seg[0])
            _add(needs, base_id, seg[1])
    return needs


def character_breakthrough_rows(character, entry, inventory=None):
    """突破门槛视图（纯读）：等级 cur→tgt 跨越的每一道门槛材料与持有/缺少。

    返回 [{gate, to_level, mats:[{mat_id, name, need, have, short}]}]；
    数据全部来自 charBreak 真实表，材料归属来自目录条目，不使用任何硬编码数字。
    """
    from .planner import display_name  # 局部导入避免环
    inv = inventory if inventory is not None else {}
    tracks = character.get("tracks") or {}
    levels = tracks.get("char_level") if isinstance(tracks.get("char_level"), dict) else {}
    try:
        lv_cur, lv_tgt = int(levels.get("cur", 0) or 0), int(levels.get("tgt", 0) or 0)
    except (TypeError, ValueError):
        lv_cur = lv_tgt = 0
    boss_id = ((entry or {}).get("boss_material") or {}).get("mat_id")
    base_id = game_data.mob_base_id(entry) if entry else None
    rows = []
    for gate in crossed_gates(lv_cur, lv_tgt):
        seg = breakthrough_gate_cost(gate)
        if not seg:
            continue
        mats = []
        for mat_id, need in zip((boss_id, base_id), (seg[0], seg[1])):
            if not mat_id:
                continue
            have = int(inv.get(mat_id, 0) or 0)
            mats.append({"mat_id": mat_id, "name": display_name(mat_id),
                         "need": need, "have": have, "short": max(0, need - have)})
        rows.append({"gate": gate, "to_level": gate if gate == FINAL_GATE else gate + 1,
                     "mats": mats})
    return rows
