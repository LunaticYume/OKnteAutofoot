"""规划器本地 Web 面板（纯标准库）：API + 静态页 + 桌面窗口壳。"""
import base64
import copy
import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app import planner
from app import game_data
from app import growth
from app import wiki_fetch
from app.oknte_adapter import OkNte, OkNteError
from app.runner import RUNNER
from app.state import (DATA_DIR, ROOT, STATE, STATE_SCHEMA, load_json,
                       new_character, save_json_atomic as _save_json_atomic)

if getattr(sys, "frozen", False):
    # PyInstaller：入口脚本被存为根级 main，__file__ 指向 _MEIPASS/main.py，
    # 面板页按 --add-data 的目标路径解析（onefile 解包到 _MEIPASS/app/web）
    base = getattr(sys, "_MEIPASS", "") or os.path.dirname(os.path.abspath(sys.executable))
    WEB_DIR = os.path.join(base, "app", "web")
    if not os.path.isfile(os.path.join(WEB_DIR, "index.html")):
        WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
else:
    WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
INT_RE = re.compile(r"^-?\d+$")


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def _build_info():
    """构建标识（build_exe 打包时写入 data/build_info.json）；源码运行为 dev。
    用户据此一眼区分面板新旧——"什么也没改"类反馈的定位锚点。"""
    try:
        with open(os.path.join(DATA_DIR, "build_info.json"), encoding="utf-8") as f:
            info = json.load(f)
        if isinstance(info, dict) and info.get("built_at"):
            return info
    except (OSError, ValueError):
        pass
    return {"built_at": "dev", "git": ""}


def _tables():
    return {
        "materials": planner.MATERIALS,
        "yields": planner.YIELDS,
        "requirements": planner.REQUIREMENTS,
    }


def _clamp_int(value, lo, hi, fallback):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(lo, min(hi, value))


def _opt_text(value, limit):
    """可空文本：None/空白 → None；绝不把 None 变成字符串 "None"（旧版缺陷：
    str(None)=="None" 被当作真值存库，回显时与下拉选项永远匹配不上）。"""
    if value is None:
        return None
    text = str(value).strip()[:limit]
    return text or None


def _sanitize_character(raw):
    char = new_character(str(raw.get("name", ""))[:32])
    # 空/缺失 id 重新生成，避免落库后出现空主键（str(raw["id"]) 允许自定义值）
    char["id"] = str(raw.get("id") or char["id"])[:16]
    char["enabled"] = bool(raw.get("enabled", True))
    weapon = raw.get("weapon")
    if isinstance(weapon, str) and weapon.strip():
        char["weapon"] = weapon.strip()[:32]  # 来自武器库的下拉选择（可空）
    else:
        char["weapon"] = ""
    mats = raw.get("mats") or {}
    char["mats"] = {
        "ability": _clamp_int(mats.get("ability"), 1, 5, 1),
        "arc": _clamp_int(mats.get("arc"), 1, 5, 1),
        "console": _clamp_int(mats.get("console"), 1, 6, 1),
    }
    tracks = raw.get("tracks") or {}
    clean_tracks = {}
    for key, levels in tracks.items():
        if key not in char["tracks"] or not isinstance(levels, dict):
            continue
        clean_tracks[key] = {
            "cur": _clamp_int(levels.get("cur"), 0, 999, 1),
            "tgt": _clamp_int(levels.get("tgt"), 0, 999, 1),
        }
    char["tracks"].update(clean_tracks)
    extra = raw.get("extra_needs") or {}
    char["extra_needs"] = {
        str(k): _clamp_int(v, 0, 10**7, 0)
        for k, v in extra.items() if str(k) in planner.ALL_ORDER  # 全量目录（体力本+外部）
    }
    # ===== 角色档案扩展（真机反馈第三批）——缺失/类型不符时回落默认，绝不抛异常 =====
    def _text(key, limit=32):
        value = raw.get(key)
        return value.strip()[:limit] if isinstance(value, str) and value.strip() else None

    # 属性/物态：弧盘适配过滤依赖（为空时弧盘页显示全部并提示）
    char["attribute"] = _text("attribute", 8)
    char["matter_state"] = _text("matter_state", 8)
    # 觉醒档位 0-6：独立记录字段（用户实机口径：与突破材料无关，不参与计算）
    char["awakening"] = _clamp_int(raw.get("awakening"), 0, 6, 0)
    # 异能=技能：四技能（普攻/变轨/极轨/援护）cur/tgt，上限取 wiki skillUpgrade
    # 实际覆盖的最高级（growth.skill_max()，当前数据为 10），不再硬编码
    skill_top = growth.skill_max()
    skills = raw.get("skills") if isinstance(raw.get("skills"), dict) else {}
    entry_skills = {}
    for k in growth.SKILL_KEYS:
        block = skills.get(k) if isinstance(skills.get(k), dict) else {}
        entry_skills[k] = {"cur": _clamp_int(block.get("cur"), 0, skill_top, 0),
                           "tgt": _clamp_int(block.get("tgt"), 0, skill_top, 0)}
    char["skills"] = entry_skills
    # 天赋解锁（wiki talentUnlock 两档，角色可选开启；参与技能材料计算）
    raw_unlocks = raw.get("talent_unlocks")
    unlocks = raw_unlocks if isinstance(raw_unlocks, list) else []
    char["talent_unlocks"] = [bool(unlocks[i]) if i < len(unlocks) else False
                              for i in range(2)]
    # 都市特技（1-2 个，名字+等级；开放槽位数 1|2，由实机数据/用户开关决定）
    talents = raw.get("city_talents") if isinstance(raw.get("city_talents"), list) else []
    char["city_talents"] = []
    for t in talents[:2]:
        if isinstance(t, dict) and str(t.get("name", "")).strip():
            char["city_talents"].append({"name": str(t["name"]).strip()[:32],
                                         "lv": _clamp_int(t.get("lv"), 0, 5, 0)})
    char["city_talent_slots"] = _clamp_int(raw.get("city_talent_slots"), 1, 2, 1)
    # 空幕卡带（1 张/角色：套装+主属性）；空值必须是 None（不是字符串 "None"）
    cassette = raw.get("cassette") if isinstance(raw.get("cassette"), dict) else {}
    char["cassette"] = {"set": _opt_text(cassette.get("set"), 48),
                        "main": _opt_text(cassette.get("main"), 24)}
    # 驱动块（多件只记录：形状/品质/主属性/副词条×4，≤8 件）
    drivers = raw.get("drivers") if isinstance(raw.get("drivers"), list) else []
    char["drivers"] = []
    for d in drivers[:8]:
        if not isinstance(d, dict) or not str(d.get("shape", "")).strip():
            continue
        char["drivers"].append({
            "shape": str(d.get("shape")).strip()[:16],
            "quality": str(d.get("quality")).strip()[:8],
            "main": str(d.get("main", "")).strip()[:24],
            "subs": [str(x).strip()[:24] for x in (d.get("subs") or [])[:4]
                     if isinstance(x, str) and x.strip()]})
    # 最佳词条组（≤3 组：标签+主属性+副词条×4）
    groups = raw.get("stigma_groups") if isinstance(raw.get("stigma_groups"), list) else []
    char["stigma_groups"] = []
    for g in groups[:3]:
        if not isinstance(g, dict):
            continue
        char["stigma_groups"].append({
            "label": str(g.get("label", "")).strip()[:32] or f"词条组{len(char['stigma_groups'])+1}",
            "main": str(g.get("main", "")).strip()[:24],
            "subs": [str(x).strip()[:24] for x in (g.get("subs") or [])[:4]
                     if isinstance(x, str) and x.strip()]})
    # 导入置信度元数据（目录调研结论）：纯展示/提示用，不参与任何计算。
    # 此前不在白名单内，落库即丢 → 导入完成提示永远显示"全部字段高/中置信"。
    confidence = raw.get("game_confidence")
    if isinstance(confidence, dict) and confidence:
        char["game_confidence"] = {str(k)[:32]: str(v)[:200]
                                   for k, v in confidence.items() if v}
    else:
        char["game_confidence"] = None
    return char


def _sanitize_settings(raw):
    current = dict(STATE.settings)
    for key in ("daily_stamina_limit", "daily_reset_hour", "anomaly_hunter_stamina",
                "max_run_minutes"):
        if key in raw:
            current[key] = _clamp_int(raw[key], 0, 999, current[key])
    if "stamina_cap" in raw:
        current["stamina_cap"] = _clamp_int(raw["stamina_cap"], 1, 999, current["stamina_cap"])
    if "port" in raw:
        current["port"] = _clamp_int(raw["port"], 1, 65535, current["port"])
    if "stamina_regen_per_hour" in raw:
        current["stamina_regen_per_hour"] = _clamp_int(
            raw["stamina_regen_per_hour"], 0, 999, current["stamina_regen_per_hour"])
    if "stamina_now" in raw:  # 填写即以服务端当前时刻为推算基准；空值清除
        value = raw["stamina_now"]
        if value is None or value == "":
            current["stamina_now"] = None
            current["stamina_now_at"] = ""
        else:
            try:
                current["stamina_now"] = round(max(0.0, min(float(value), 9999.0)), 1)
                current["stamina_now_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                pass  # 非法输入忽略，保留原值
    if "busy_retry_seconds" in raw:
        current["busy_retry_seconds"] = _clamp_int(
            raw["busy_retry_seconds"], 0, 3600, current.get("busy_retry_seconds", 300))
    if "game_window_keywords" in raw and isinstance(raw["game_window_keywords"], str):
        current["game_window_keywords"] = raw["game_window_keywords"].strip()[:64]
    if "game_process_name" in raw and isinstance(raw["game_process_name"], str):
        current["game_process_name"] = raw["game_process_name"].strip()[:64]
    if "ocr_max_pages" in raw:
        current["ocr_max_pages"] = _clamp_int(raw["ocr_max_pages"], 1, 20,
                                              current.get("ocr_max_pages", 5))
    if "oknte_root" in raw and isinstance(raw["oknte_root"], str):
        current["oknte_root"] = raw["oknte_root"].strip()[:200]
    if "anomaly_hunter_target" in raw and isinstance(raw["anomaly_hunter_target"], str):
        current["anomaly_hunter_target"] = raw["anomaly_hunter_target"].strip()[:32]
    for key in ("reserve_anomaly_hunter", "notify_enabled", "continuous_mode"):
        if key in raw:
            current[key] = bool(raw[key])
    current["daily_reset_hour"] = _clamp_int(current["daily_reset_hour"], 0, 23, 4)
    return current


def _sanitize_custom_character(raw):
    """图鉴覆盖层角色条目消毒：name 必填；养成字段可空；置信度固定 manual。"""
    if not isinstance(raw, dict):
        raise ValueError("角色条目必须是对象")
    name = str(raw.get("name", "")).strip()
    if not name:
        raise ValueError("角色名必填")

    def _text(key, limit=32):
        value = raw.get(key)
        return value.strip()[:limit] if isinstance(value, str) and value.strip() else None

    def _mat_block(raw_block, default_total):
        if not isinstance(raw_block, dict):
            return None
        mat_id = str(raw_block.get("mat_id", "")).strip()
        if not mat_id:
            return None
        return {"mat_id": mat_id[:48], "boss": str(raw_block.get("boss", "")).strip()[:32],
                "total": _clamp_int(raw_block.get("total"), 0, 10 ** 6, default_total),
                "confidence": "manual"}

    entry = {"name": name[:32],
             "rarity": raw.get("rarity") if raw.get("rarity") in ("S", "A") else "S",
             "live": bool(raw.get("live", True))}
    for key in ("attribute", "matter_state", "version", "signature_weapon",
                "skill_series", "skill_book_name", "mob_family"):
        entry[key] = _text(key)
    entry["boss_material"] = _mat_block(raw.get("boss_material"), 86)
    entry["weekly_material"] = _mat_block(raw.get("weekly_material"), 11)
    if entry["mob_family"]:
        # 小怪白当量：留空取全角色统一口径 390（206 突破 + 184 技能）
        entry["mob_base_total"] = _clamp_int(raw.get("mob_base_total"), 0, 10 ** 6, 390)
    else:
        entry["mob_base_total"] = None
    entry["skill_confidence"] = "manual"
    # 觉醒档位 0-6（真机反馈：角色档案扩展）
    entry["awakening"] = _clamp_int(raw.get("awakening"), 0, 6, 0)
    # 异能技能拆分：普攻/变轨/极轨/援护 cur/tgt + 都市特技（1-2 个，lv 各异）
    skills = raw.get("skills") if isinstance(raw.get("skills"), dict) else {}
    entry["skills"] = {k: {"cur": _clamp_int((skills.get(k) or {}).get("cur"), 0, 10, 0),
                           "tgt": _clamp_int((skills.get(k) or {}).get("tgt"), 0, 10, 0)}
                       for k in ("normal", "variable", "orbit", "assist")}
    talents = raw.get("city_talents") if isinstance(raw.get("city_talents"), list) else []
    entry["city_talents"] = [{"name": str(t.get("name", ""))[:32],
                              "lv": _clamp_int(t.get("lv"), 0, 5, 0)}
                             for t in talents[:2] if isinstance(t, dict) and t.get("name")]
    # 词条组（最佳 2-3 组；主属性 1 + 副属性 4）
    groups = raw.get("stigma_groups") if isinstance(raw.get("stigma_groups"), list) else []
    clean_groups = []
    for g in groups[:3]:
        if not isinstance(g, dict):
            continue
        clean_groups.append({
            "label": str(g.get("label", "")).strip()[:32] or f"词条组{len(clean_groups)+1}",
            "main": str(g.get("main", "")).strip()[:24],
            "subs": [str(x).strip()[:24] for x in (g.get("subs") or [])[:4]
                     if isinstance(x, str) and x.strip()]})
    entry["stigma_groups"] = clean_groups
    return entry


def _sanitize_custom_weapon(raw):
    """图鉴覆盖层武器条目消毒（与基础 weapons 条目同构：name/rarity/matter_state/owner/acquisition）。"""
    if not isinstance(raw, dict):
        raise ValueError("武器条目必须是对象")
    name = str(raw.get("name", "")).strip()
    if not name:
        raise ValueError("武器名必填")
    entry = {"name": name[:32],
             "rarity": raw.get("rarity") if raw.get("rarity") in ("S", "A") else "S"}
    for key in ("matter_state", "owner", "acquisition"):
        value = raw.get(key)
        entry[key] = value.strip()[:48] if isinstance(value, str) and value.strip() else None
    return entry


# materials.json 读-改-写互斥（评审 2026-11-05）：服务端是 ThreadingHTTPServer，
# 图鉴新材料登记（add_custom_material）、display 批量保存（/api/tables/materials）
# 与 OCR 实名映射（_ocr_map_unknown）并发读-改-写会互相丢更新（后写整盘覆盖先写）
# ——三个写者的查重+落盘+reload 段统一持这把锁串行化。
_MATERIALS_LOCK = threading.Lock()


def add_custom_material(body):
    """图鉴页新材料登记：追加进 materials.json 的 extra_materials（按 id 查重）。

    新角色的 BOSS/周本/小怪材料必须先登记，extra_needs 的白名单（planner.ALL_ORDER）
    才会收——否则角色导入的预填会被静默丢弃。
    """
    mat_id = str((body or {}).get("id", "")).strip()
    if not re.fullmatch(r"[a-z0-9_]+", mat_id):
        raise ValueError("材料 id 只允许小写字母/数字/下划线（如 boss_new_x）")
    display = str(body.get("display_name", "")).strip()[:64]
    if not display:
        raise ValueError("材料显示名必填")
    entry = {"id": mat_id, "display_name": display,
             "category": str(body.get("category", "")).strip()[:32] or "自定义",
             "obtain": body.get("obtain") if body.get("obtain") in ("hunter", "external") else "external",
             "source": str(body.get("source", "")).strip()[:96]}
    with _MATERIALS_LOCK:  # 查重+追加+落盘+reload 整体持锁（防并发丢更新）
        if mat_id in planner.TARGETS or mat_id in planner.EXTRA:
            raise ValueError(f"材料 id『{mat_id}』已存在")
        planner.MATERIALS.setdefault("extra_materials", []).append(entry)
        _save_json_atomic(os.path.join(DATA_DIR, "materials.json"), planner.MATERIALS)
        planner.reload_tables()
    return entry


def _auto_fill_extra_needs(chars):
    """保存角色时自动计算直接材料（用户要算术活儿交给机器）：
    名字命中目录/图鉴覆盖层的角色，BOSS×86、周本×11、小怪族×390 按目录映射
    覆盖写入 extra_needs；用户手填的其它键（自定义材料）保留；未命中角色不动。"""
    try:
        data = game_data.load()
    except (OSError, ValueError):
        return chars
    costs = _common_costs()
    by_name = {c.get("name"): c for c in data.get("characters", []) if isinstance(c, dict)}
    for char in chars:
        entry = by_name.get(char.get("name"))
        if not entry:
            continue
        prefill, _skipped = game_data.character_material_mapping(data, entry, costs)
        if not prefill:
            continue
        manual = {k: v for k, v in (char.get("extra_needs") or {}).items() if k not in prefill}
        char["extra_needs"] = {**manual, **prefill}
    return chars


def _sanitize_teams(raw):
    """配队：[{name, members:[角色名 ≤4]}]；队内唯一，跨队可重复（用户语义）。"""
    teams = raw.get("teams") if isinstance(raw.get("teams"), list) else []
    clean, seen_names = [], set()
    for t in teams[:20]:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name", "")).strip()[:32] or f"队伍{len(clean)+1}"
        key = name
        if key in seen_names:
            continue
        seen_names.add(key)
        members, seen_m = [], set()
        for m in (t.get("members") or [])[:4]:
            n = str(m).strip()[:32]
            if n and n not in seen_m:
                seen_m.add(n)
                members.append(n)
        clean.append({"name": name, "members": members})
    return clean


def duplicate_character_names(chars):
    """后端重名互斥的判定（与『按目录导入』同一口径；此前只在前端拦截）。"""
    names = [c.get("name") for c in chars if isinstance(c, dict) and c.get("name")]
    return sorted({n for n in names if names.count(n) > 1})


def reorder_characters(ids):
    """按 id 重排角色数组：只排列现有对象，绝不重建——任何字段都不会丢。
    未出现在 ids 里的角色（如"不想练"区的停用角色）按原相对顺序追加在尾部；
    列表顺序 = 补齐优先级（planner.snapshot 消费数组序）。"""
    with STATE.lock:
        by_id = {c.get("id"): c for c in STATE.data["characters"]}
        ordered = [by_id[i] for i in ids if i in by_id]
        seen = set(ids)
        ordered += [c for c in STATE.data["characters"] if c.get("id") not in seen]
        STATE.data["characters"] = ordered
        STATE.save()
    return ordered


def _sanitize_libraries(raw):
    """角色库：非空去重字符串列表（≤32字/项，≤200项）。
    武器库允许同名多条（用户语义：同名多把，不同角色各用各的把），不做去重。"""
    def _clean(value, dedupe=True):
        if not isinstance(value, list):
            return []
        seen, out = set(), []
        for item in value[:400]:
            name = str(item).strip()[:32]
            if name and (name not in seen or not dedupe):
                if dedupe:
                    seen.add(name)
                out.append(name)
        return out[:200]
    return {"char_library": _clean(raw.get("char_library")),
            "weapon_library": _clean(raw.get("weapon_library"), dedupe=False)}


def _common_costs():
    """通用养成数量：默认 + 用户覆盖深合并（用户只存改过的键）。"""
    merged = dict(game_data.COMMON_COST_DEFAULTS)
    for key, value in (STATE.data.get("common_costs") or {}).items():
        merged[key] = [int(v) for v in value] if isinstance(value, list) else value
    return merged


def _common_mapping_rows(data, costs):
    """每角色 → 具体实名材料的通用量映射预览（仅已实装角色）。"""
    rows = []
    for entry in data.get("characters", []):
        if not entry.get("live"):
            continue
        prefill, skipped = game_data.character_material_mapping(data, entry, costs)
        rows.append({
            "name": entry["name"],
            "attribute": entry.get("attribute"),
            "matter_state": entry.get("matter_state"),
            "skill_series": data.get("attribute_to_skill_series", {}).get(entry.get("attribute")),
            "can_series": data.get("matter_state_to_can", {}).get(entry.get("matter_state")),
            "boss": entry.get("boss_material"),
            "weekly": entry.get("weekly_material"),
            "mob_family": entry.get("mob_family"),
            "prefill": prefill,
            "skipped": skipped,
        })
    return rows


def _sanitize_common_costs(raw):
    """通用量保存校验：键白名单 + 数量钳位；列表固定三档。"""
    clean = {}
    for key, value in (raw or {}).items():
        if key not in game_data.COMMON_COST_DEFAULTS:
            continue
        if isinstance(game_data.COMMON_COST_DEFAULTS[key], list):
            if isinstance(value, list) and len(value) == 3:
                clean[key] = [_clamp_int(v, 0, 10**7, 0) for v in value]
        else:
            clean[key] = _clamp_int(value, 0, 10**7, 0)
    return clean


def build_export():
    """导出用户配置（settings/角色/库存/库/产量/需求表；不含历史与运行时）。"""
    with STATE.lock:
        return {
            "schema": STATE_SCHEMA,
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "settings": copy.deepcopy(STATE.data["settings"]),
            "characters": copy.deepcopy(STATE.data["characters"]),
            "char_library": copy.deepcopy(STATE.data.get("char_library", [])),
            "weapon_library": copy.deepcopy(STATE.data.get("weapon_library", [])),
            "teams": copy.deepcopy(STATE.data.get("teams", [])),
            "inventory": copy.deepcopy(STATE.data["inventory"]),
            "common_costs": _common_costs(),
            "yields": {"per_run": dict(planner.YIELDS["per_run"]),
                       "hunter_yield": dict(planner.YIELDS.get("hunter_yield", {}))},
            "requirements": copy.deepcopy(planner.REQUIREMENTS),
        }


def apply_import(payload):
    """导入配置：全部段落先校验/脱敏，任何不合法整体拒绝（绝不留半套数据）；
    导入前自动备份当前用户数据；状态与数据表统一原子落盘。"""
    if not isinstance(payload, dict) or payload.get("schema") != STATE_SCHEMA:
        raise ValueError(f"导入文件缺少 schema 或版本不符（需要 {STATE_SCHEMA}）")
    settings = payload.get("settings")
    characters = payload.get("characters")
    inventory = payload.get("inventory")
    yields = payload.get("yields")
    requirements = payload.get("requirements")
    if not isinstance(settings, dict):
        raise ValueError("settings 必须是对象")
    if not isinstance(characters, list) or not characters:
        raise ValueError("characters 必须是非空数组")
    if not isinstance(inventory, dict):
        raise ValueError("inventory 必须是对象")
    if not isinstance(yields, dict) or not isinstance(yields.get("per_run"), dict):
        raise ValueError("yields.per_run 必须是对象")
    if not isinstance(requirements, dict) or "tracks" not in requirements:
        raise ValueError("requirements 缺少 tracks 字段")
    clean_chars = [_sanitize_character(c) for c in characters[:50]]
    clean_inv = {str(k): _clamp_int(v, 0, 10**8, 0)
                 for k, v in inventory.items() if str(k) in planner.ALL_ORDER}  # 全量目录（体力本+外部），与 /api/inventory 同口径
    clean_settings = _sanitize_settings(settings)
    clean_yields = {**planner.YIELDS, "per_run": {
        str(k): _clamp_int(v, 0, 10**6, 1)
        for k, v in yields["per_run"].items() if str(k) in planner.TARGETS}}
    backup_dir = os.path.join(DATA_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    backup_path = os.path.join(
        backup_dir, f"state.import.{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with STATE.lock:
        _save_json_atomic(backup_path, {
            "settings": copy.deepcopy(STATE.data["settings"]),
            "characters": copy.deepcopy(STATE.data["characters"]),
            "inventory": copy.deepcopy(STATE.data["inventory"]),
        })
        STATE.data["settings"] = clean_settings
        STATE.data["characters"] = clean_chars
        STATE.data["inventory"] = clean_inv
        # 库为可选段（旧版导出可能没有）：仅在文件里出现时才覆盖，避免误清空
        if "char_library" in payload or "weapon_library" in payload:
            libs = _sanitize_libraries(payload)
            STATE.data["char_library"] = libs["char_library"]
            STATE.data["weapon_library"] = libs["weapon_library"]
        if "common_costs" in payload:  # 通用量随导入迁移（可选段）
            clean_costs = _sanitize_common_costs(payload["common_costs"])
            STATE.data["common_costs"] = {**(STATE.data.get("common_costs") or {}),
                                          **clean_costs}
        if "teams" in payload:  # 配队随导入迁移（v3 起导出包含；旧文件无此段不动）
            STATE.data["teams"] = _sanitize_teams(payload)
        STATE.data["schema"] = STATE_SCHEMA
        STATE.save()
    _save_json_atomic(os.path.join(DATA_DIR, "yields.json"), clean_yields)
    _save_json_atomic(os.path.join(DATA_DIR, "requirements.json"), requirements)
    planner.reload_tables()
    return {"characters": len(clean_chars), "inventory_items": len(clean_inv),
            "backup": backup_path}


def clear_today_blacklist():
    """清空今日拉黑（用户自救：历史启动失败残留/想重试被跳过的目标）。
    运行中调用由 do_POST 的统一拦截放行（清拉黑不影响执行安全）。"""
    STATE.ensure_today()
    with STATE.lock:
        cleared = len(STATE.today.get("blacklist", []))
        STATE.today["blacklist"] = []
        STATE.save()
    return cleared


def confirm_history_entry(day, time_str, mat_id, actual_gain):
    """用户确认某条 done 历史的实际获得（bookkeeping 修改：写 STATE 并持久化）。"""
    with STATE.lock:
        for entry in reversed(STATE.data["history"]):
            if (entry.get("day") == day and entry.get("time") == time_str
                    and entry.get("mat_id") == mat_id):
                if entry.get("status") != "done":
                    return False, "只能确认状态为完成的记录"
                entry["actual_gain"] = int(actual_gain)
                entry["confirmed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                STATE.save()
                return True, "已确认实际获得"
        return False, "未找到匹配的历史记录"


def build_compat_report(oknte):
    """一键兼容报告（markdown）：指纹结果 + 首查清单 + 校准状态 + 运行状态。"""
    compat = oknte.compat_check()
    costs = _common_costs()
    snap = RUNNER.snapshot()
    lines = [
        "# ok-nte 兼容报告（一键生成）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- ok-nte 版本：{compat.get('version') or '未知'}（仅展示，规划器不做版本比对）",
        f"- 兼容指纹：**{compat['status']}**（检查依赖能力而非版本号）",
    ]
    for p in compat.get("problems", []):
        lines.append(f"  - ❌ {p}")
    for w in compat.get("warnings", []):
        lines.append(f"  - ⚠️ {w}")
    if not compat.get("problems") and not compat.get("warnings"):
        lines.append("  - 全部能力检查通过")
    coin_y = planner.YIELDS["per_run"].get("coin", 0)
    lines += [
        "",
        "## 依赖面首查清单（ok-nte 更新后按序核对）",
        "1. 本报告指纹状态：compatible 即可正常使用；incompatible 时规划器拒绝写配置（不会写坏 ok-nte）",
        "2. 面板总览『生成计划』是否正常产出（覆盖日志读取路径）",
        "3. 配置键（daily_anomaly / daily_anomaly_hunter）→ 日志文案（RunMonitor 正则）→ CLI 任务名（DailyRoutineTask / AnomalyHunter）",
        "4. `data/backups/` 保存最近 20 份原配置，可随时回滚",
        "",
        "## 校准状态",
        f"- 甲硬币（coin）每局产量：{'⚠️ 未校准（为0，相关需求被跳过）' if not coin_y else coin_y}",
        f"- 追猎每局掉落：{planner.YIELDS.get('hunter_yield', {})}",
        f"- 通用材料表（common_costs）：{json.dumps(costs, ensure_ascii=False)}",
        f"- 追猎/日常今日记账：追猎 {STATE.today.get('hunter_spent', 0)} / 日常 {STATE.today.get('planner_spent', 0)}",
        f"- 执行器状态：{snap['state']}" + (f"（模式 {snap.get('mode')}）" if snap.get("state") != "idle" else ""),
        f"- 未决执行：{len(snap.get('pending', []))} 笔" + ("（需手动对账后才能开始新执行）" if snap.get("pending") else ""),
        "",
        "完整依赖面清单见 docs/UPSTREAM_COMPATIBILITY.md；详细验收报告见 docs/results/。",
    ]
    return "\n".join(lines)


def perform_dry_run(oknte):
    """Dry Run 预演（全程纯读）：完整走 计划 → 指纹 → 补丁构建 → 补丁校验，
    展示"如果执行将发生什么"。不写文件、不启动 ok-nte、不改额度/库存、不留运行账目；
    与真实执行共用 planner.make_plan、compat_check、build_anomaly_patch（单一逻辑源）。
    """
    compat = oknte.compat_check()
    result = {"compat": compat, "oknte_running": oknte.is_running_cached()}
    if compat["status"] == "incompatible":
        result.update({"ok": False, "error": "兼容性检查失败，未生成补丁", "patch": None})
        return result
    plan = planner.make_plan(oknte)
    result["plan"] = plan
    if not plan["steps"]:
        result.update({"ok": True, "patch": None})
        return result
    step = plan["steps"][0]
    settings = STATE.settings
    hunter, hunter_note = None, ""
    if settings.get("reserve_anomaly_hunter"):
        hunter = {
            "stamina": int(settings.get("anomaly_hunter_stamina", 180)),
            "target": settings.get("anomaly_hunter_target", ""),
        }
        hunter_note = "追猎预留启用：真实执行会在当天首次写配置时一并写入追猎段；本预演按首写展示"
    try:
        patch = oknte.build_anomaly_patch(
            planner.TARGETS[step["mat_id"]]["config"], step["stamina"], hunter=hunter)
    except (OkNteError, KeyError, ValueError) as e:
        result.update({"ok": False, "error": f"补丁构建失败：{e}", "patch": None})
        return result
    result.update({
        "ok": True,
        "patch": {"file": oknte.routine_config, "diff": patch["diff"]},
        "hunter_note": hunter_note,
    })
    return result


def _collect_log_sources(oknte_root):
    """日志页白名单：[(显示名, 绝对路径)]，只收三类目录下按模式命中的 .log。
    端点以白名单下标定位文件（查询串里不传路径，天然免路径穿越）。"""
    apps = os.path.join(oknte_root, "data", "apps", "ok-nte")
    working_logs = os.path.join(apps, "working", "logs")
    app_logs = os.path.join(oknte_root, "logs")
    out = []

    def add(directory, pattern, label):
        if not os.path.isdir(directory):
            return
        try:
            names = sorted(os.listdir(directory), reverse=True)
        except OSError:
            return
        for name in names:
            if re.fullmatch(pattern, name) and os.path.isfile(os.path.join(directory, name)):
                out.append((f"{label}/{name}", os.path.join(directory, name)))

    add(working_logs, r"ok-script.*\.log", "任务日志")
    add(app_logs, r"app\..+\.log", "应用日志")
    add(DATA_DIR, r"(panel|crash)\.log", "面板")
    add(ROOT, r"(crash|launcher)\.log", "面板")
    return out


# ---- 背包 OCR v3：窗口枚举/绑定 + 网格点击遍历 ----
# 窗口绑定改用 EnumWindows 全量枚举（Get-Process 的 MainWindowHandle 启发式在
# UE 多顶层窗口进程上不可靠），绑定优先级见 _bind_game_window。
_OCR_LAYOUT_FILE = os.path.join(DATA_DIR, "ocr_layout.json")
# 契约默认布局（2026-11-04 用户游戏截图实测：背包 7列×4行，列中心
# 197/365/523/689/851/1015/1181，行中心 243/451/655/840，详情面板在右侧；
# 全部可在设置/标定调整；缺失时落盘该默认）
_OCR_LAYOUT_DEFAULT = {
    "first_cell": {"x": 0.1026, "y": 0.225},
    "cols": 7,
    "rows": 4,
    "step": {"dx": 0.0854, "dy": 0.1843},
    "detail_roi": {"x": 0.682, "y": 0.139, "w": 0.297, "h": 0.713},
    "page_wait_ms": 800,
}
_OCR_GAME_WINDOW_CLASS = "UnrealWindow"  # UE 主窗口类名（ok-nte 同款绑定依据）
_OCR_WINDOWS_PREFIX = "__OCR_WINDOWS__:"


class _OcrWindowNotFound(OSError):
    """游戏窗口未绑定（/api/inventory/ocr 转 502 明确报错，不再静默回退全屏）。"""


_OCR_PS_COMMON = r'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Runtime.WindowsRuntime
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32OCR {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lParam);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassNameW(IntPtr hWnd, System.Text.StringBuilder sb, int maxCount);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr hWnd, System.Text.StringBuilder sb, int maxCount);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
  [DllImport("kernel32.dll")] public static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
  [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] public static extern bool QueryFullProcessImageNameW(IntPtr proc, uint flags, System.Text.StringBuilder sb, ref uint size);
  [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr h);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, int dwData, UIntPtr dwExtraInfo);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
}
"@
[Win32OCR]::SetProcessDPIAware() | Out-Null
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.DataWriterStoreOperation, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType=WindowsRuntime]
Function Await($WinRtTask, $ResultType) {
  # $ResultType 非空 → IAsyncOperation`1（识别等）；$null → IAsyncAction（StoreAsync 等无返回值的异步）
  $ext = [System.WindowsRuntimeSystemExtensions]
  if ($ResultType) {
    $asTask = ($ext.GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
    $netTask = $asTask.MakeGenericMethod($ResultType).Invoke($null, @($WinRtTask))
  } else {
    $asTask = ($ext.GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' })[0]
    $netTask = $asTask.Invoke($null, @($WinRtTask))
  }
  $netTask.Wait(-1) | Out-Null
  $netTask.Result
}
Function New-OcrEngine {
  # 中文 OCR 引擎：zh-CN 优先 → 用户语言回退；都没有 → 输出标记后退出（Python 转 ValueError）
  $eng = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage("zh-CN")
  if (-not $eng) { $eng = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
  if (-not $eng) {
    $langs = ([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object { $_.LanguageTag }) -join ","
    Write-Output "__OCR_NOLANG__"
    Write-Output "__OCR_LANGS__:$langs"
    exit 1
  }
  return $eng
}
Function Invoke-Ocr($rect, $engine) {
  # 截 $rect 屏幕区域 → WinRT 识别 → 文本（截屏与识别的唯一实现，各脚本段共用）
  $bmp = New-Object System.Drawing.Bitmap($rect.Width, $rect.Height)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($rect.X, $rect.Y, 0, 0, (New-Object System.Drawing.Size($rect.Width, $rect.Height)))
  $g.Dispose()
  $ms = New-Object System.IO.MemoryStream
  $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
  $bmp.Dispose()
  $stream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
  $writer = New-Object Windows.Storage.Streams.DataWriter($stream.GetOutputStreamAt(0))
  $writer.WriteBytes($ms.ToArray())
  $ms.Dispose()
  Await ($writer.StoreAsync()) $null | Out-Null
  $writer.Dispose()
  $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $soft = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $result = Await ($engine.RecognizeAsync($soft)) ([Windows.Media.Ocr.OcrResult])
  return $result.Text
}
'''

_OCR_PS_WINDOW = r'''
# ---- 窗口定位：Python 已绑定 hwnd 注入（每轮经 IsWindow 校验；0/失效 → 全屏回退）----
$Hwnd = [IntPtr]__HWND__
$RoiRaw = '__ROI__'
$meta = @{ window = ""; x = 0; y = 0; w = 0; h = 0; fallback = $false }
$win = @{ x = 0; y = 0; w = 0; h = 0 }   # 完整窗口矩形（点击坐标基准；$meta 为截屏区域）
if ($Hwnd -ne [IntPtr]::Zero -and [Win32OCR]::IsWindow($Hwnd)) {
  $null = [Win32OCR]::SetForegroundWindow($Hwnd)
  Start-Sleep -Milliseconds 250
  $wt = New-Object System.Text.StringBuilder 512
  $null = [Win32OCR]::GetWindowTextW($Hwnd, $wt, 512)
  $meta.window = $wt.ToString()
  $rect = New-Object Win32OCR+RECT
  $null = [Win32OCR]::GetWindowRect($Hwnd, [ref]$rect)
  $win.x = $rect.Left; $win.y = $rect.Top
  $win.w = [Math]::Max(1, $rect.Right - $rect.Left)
  $win.h = [Math]::Max(1, $rect.Bottom - $rect.Top)
  $meta.x = $win.x; $meta.y = $win.y; $meta.w = $win.w; $meta.h = $win.h
  if ($RoiRaw -ne '') {
    # 详情面板 ROI（相对窗口比例 x/y/w/h），钳位 0-1 并裁剪到窗口内
    $rp = @()
    foreach ($p in ($RoiRaw -split ',')) { $rp += [double]$p }
    if ($rp.Count -ge 4) {
      $rx = [Math]::Max(0.0, [Math]::Min(1.0, $rp[0]))
      $ry = [Math]::Max(0.0, [Math]::Min(1.0, $rp[1]))
      $rw = [Math]::Max(0.0, [Math]::Min(1.0, $rp[2]))
      $rh = [Math]::Max(0.0, [Math]::Min(1.0, $rp[3]))
      $meta.x = $win.x + [int][Math]::Round($rx * $win.w)
      $meta.y = $win.y + [int][Math]::Round($ry * $win.h)
      $meta.w = [Math]::Max(1, [int][Math]::Round($rw * $win.w))
      $meta.h = [Math]::Max(1, [int][Math]::Round($rh * $win.h))
      if ($meta.x -gt $win.x + $win.w - 1) { $meta.x = $win.x; $meta.w = $win.w }
      if ($meta.y -gt $win.y + $win.h - 1) { $meta.y = $win.y; $meta.h = $win.h }
      if ($meta.x + $meta.w -gt $win.x + $win.w) { $meta.w = [Math]::Max(1, $win.x + $win.w - $meta.x) }
      if ($meta.y + $meta.h -gt $win.y + $win.h) { $meta.h = [Math]::Max(1, $win.y + $win.h - $meta.y) }
    }
  }
} else {
  $meta.fallback = $true
  $vs = [System.Windows.Forms.SystemInformation]::VirtualScreen
  $meta.x = $vs.Left; $meta.y = $vs.Top; $meta.w = $vs.Width; $meta.h = $vs.Height
  $win.x = $meta.x; $win.y = $meta.y; $win.w = $meta.w; $win.h = $meta.h
}
'''


def _ocr_run_ps(script, replacements, timeout=90):
    """执行 OCR 相关 PS 脚本（UTF-16LE base64 EncodedCommand，防控制台编码问题）。
    replacements 为 __占位符__ → 注入值（值必须不含占位符冲突内容）。
    返回 (stdout 解码, stderr 解码, returncode)。"""
    for key, value in replacements.items():
        script = script.replace(key, value)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    r = subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", encoded],
                       capture_output=True, timeout=timeout,
                       creationflags=0x08000000)
    out = (r.stdout or b"").decode("utf-8", errors="replace").replace("\r\n", "\n")
    err = (r.stderr or b"").decode("utf-8", errors="replace").strip()
    return out, err, r.returncode


def _ocr_check_errors(out, err, returncode):
    """OCR PS 输出的公共错误标记：语言包缺失 → ValueError（message 带可用语言）；
    脚本失败 → OSError。"""
    if "__OCR_NOLANG__" in out:
        m = re.search(r"__OCR_LANGS__:(.+)", out)
        langs = m.group(1).strip() if m else ""
        raise ValueError("本机没有可用的 Windows OCR 中文语言包。"
                         "请在系统设置→时间和语言→语言中添加中文（简体）语言包。"
                         + (f"（当前可选语言：{langs}）" if langs else ""))
    if "__OCR_ERR__" in out or (returncode not in (0, None) and not out.strip()):
        raise OSError(f"OCR 脚本执行失败：{(err or out).strip()[-200:]}")


def _ratio01(value, fallback=0.0):
    """OCR 布局比例钳位 0-1（x/y/dx/dy/w/h 共用）；非法值回落 fallback。"""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return fallback
    if value != value:  # NaN
        return fallback
    return round(min(1.0, max(0.0, value)), 4)


def _sanitize_ocr_layout(raw):
    """OCR 网格布局消毒（契约 3）：比例钳位 0-1、cols/rows 钳位 1-20、page_wait_ms 钳位；
    缺失/非法键回落契约默认值。raw 非对象时 raise ValueError（POST /api/ocr/layout → 400）。
    另收拢 step：first+step×(n-1) 超出 1 的部分截断——遍历点击用的是全局鼠标事件，
    越界点会点到游戏窗口之外的其他应用上。"""
    if not isinstance(raw, dict):
        raise ValueError("layout 必须是对象")
    first = raw.get("first_cell") if isinstance(raw.get("first_cell"), dict) else {}
    step = raw.get("step") if isinstance(raw.get("step"), dict) else {}
    roi = raw.get("detail_roi") if isinstance(raw.get("detail_roi"), dict) else {}
    d = _OCR_LAYOUT_DEFAULT
    cols = _clamp_int(raw.get("cols"), 1, 20, d["cols"])
    rows = _clamp_int(raw.get("rows"), 1, 20, d["rows"])
    fx = _ratio01(first.get("x"), d["first_cell"]["x"])
    fy = _ratio01(first.get("y"), d["first_cell"]["y"])
    dx = _ratio01(step.get("dx"), d["step"]["dx"])
    dy = _ratio01(step.get("dy"), d["step"]["dy"])
    if cols > 1:  # 最后一列/行收拢到窗口内（截断 4 位小数：round 会进位回越界；
        # 1e-6 容差抵消 (1.0-0.8)=0.1999... 型二进制浮点亏差）
        dx = max(0.0, min(dx, int((1.0 - fx) / (cols - 1) * 10000 + 1e-6) / 10000))
    if rows > 1:
        dy = max(0.0, min(dy, int((1.0 - fy) / (rows - 1) * 10000 + 1e-6) / 10000))
    return {
        "first_cell": {"x": fx, "y": fy},
        "cols": cols,
        "rows": rows,
        "step": {"dx": dx, "dy": dy},
        "detail_roi": {"x": _ratio01(roi.get("x"), d["detail_roi"]["x"]),
                       "y": _ratio01(roi.get("y"), d["detail_roi"]["y"]),
                       "w": _ratio01(roi.get("w"), d["detail_roi"]["w"]),
                       "h": _ratio01(roi.get("h"), d["detail_roi"]["h"])},
        "page_wait_ms": _clamp_int(raw.get("page_wait_ms"), 100, 10000,
                                   d["page_wait_ms"]),
    }


def _ocr_layout():
    """读取 OCR 网格布局：data/ocr_layout.json 优先（每次读取 → 热生效）；
    缺失/损坏 → 契约默认值并落盘，保证下次读取一致。"""
    try:
        with open(_OCR_LAYOUT_FILE, encoding="utf-8") as f:
            return _sanitize_ocr_layout(json.load(f))
    except (OSError, ValueError):
        layout = _sanitize_ocr_layout(dict(_OCR_LAYOUT_DEFAULT))
        try:
            _save_json_atomic(_OCR_LAYOUT_FILE, layout)
        except OSError:
            pass  # 落盘失败不阻塞识别（本次仍按默认值走）
        return layout


def _save_ocr_layout(raw):
    """校验并保存 OCR 布局（POST /api/ocr/layout）；返回消毒后的布局。
    raw 非法 raise ValueError；写盘失败 raise OSError。
    2026-11-04 实测缺陷防线：标定两点落在同一行/同一格时 dy/dx≈0，遍历会
    "只在同一行来回点"——拒绝保存（内部默认布局的钳位路径不经过这里）。"""
    layout = _sanitize_ocr_layout(raw)
    if layout["rows"] > 1 and layout["step"]["dy"] < 0.005:
        raise ValueError(
            f"网格行距 dy={layout['step']['dy']} 过小（标定的两点几乎在同一水平线上）。"
            "三点标定请点：①第一格中心 → ②同行相邻格中心 → ③第一格正下方一格中心。")
    if layout["cols"] > 1 and layout["step"]["dx"] < 0.005:
        raise ValueError(f"网格列距 dx={layout['step']['dx']} 过小（标定的两点几乎在同一格上）。")
    _save_json_atomic(_OCR_LAYOUT_FILE, layout)
    return layout


def _roi_str(roi):
    """detail_roi → PS 注入串 "x,y,w,h"（比例 0-1）；非法为空串（=整窗截取）。"""
    if not isinstance(roi, dict):
        return ""
    try:
        vals = [_ratio01(roi[k], 0.0) for k in ("x", "y", "w", "h")]
    except (TypeError, KeyError):
        return ""
    return ",".join(f"{v:g}" for v in vals)


def _default_game_process():
    """默认游戏进程名：设置 game_process_name 非空用之；否则从 ok-nte devices.json
    读 selected_exe[0]（只读、损坏忽略）；再读不到用实测进程名 HTGame.exe 降级。"""
    name = str(STATE.settings.get("game_process_name") or "").strip()
    if name:
        return name
    path = os.path.join(str(STATE.settings.get("oknte_root") or ""),
                        "data", "apps", "ok-nte", "working", "configs", "devices.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for exe in data.get("selected_exe") or []:
                if isinstance(exe, str) and exe.strip():
                    return exe.strip()
    except (OSError, ValueError):
        pass  # devices.json 不存在/损坏：按实测默认进程名降级
    return "HTGame.exe"


def _norm_process_name(name):
    """进程名归一：去 .exe 后缀、小写（"HTGame.exe" 与 "HTGame" 等价——
    devices.json 存文件名，EnumWindows 的 ProcessName 无后缀）。"""
    p = str(name or "").strip().lower()
    if p.endswith(".exe"):
        p = p[:-4]
    return p


def _window_matches_process(w, proc_name):
    """窗口进程匹配：进程名或 exe 路径文件名任一相等（忽略 .exe 后缀与大小写）。"""
    target = _norm_process_name(proc_name)
    if not target:
        return False
    if _norm_process_name(w.get("process")) == target:
        return True
    exe = str(w.get("exe_path") or "").replace("/", "\\")
    base = exe.rsplit("\\", 1)[-1] if exe else ""
    return bool(base) and _norm_process_name(base) == target


def _ocr_windows():
    """枚举全部顶层可见窗口（PS EnumWindows：IsWindowVisible + 非空标题，
    工具窗多为无标题一并排除）。失败/超时返回 []（端点显示空列表，不阻塞面板）。"""
    try:
        out, _err, _code = _ocr_run_ps(_OCR_PS_COMMON + "\n" + _OCR_PS_WINDOWS,
                                       {}, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return _parse_ocr_windows(out)


def _parse_ocr_windows(out):
    """解析 PS 输出里的窗口清单（纯函数，selftest 覆盖）：每行
    __OCR_WINDOWS__:<单对象 JSON>（也兼容整行数组 JSON），坏行忽略。"""
    windows = []
    for line in (out or "").splitlines():
        if not line.startswith(_OCR_WINDOWS_PREFIX):
            continue
        payload = line[len(_OCR_WINDOWS_PREFIX):].strip()
        if not payload:
            continue
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        for w in (data if isinstance(data, list) else [data]):
            if isinstance(w, dict) and w.get("hwnd") is not None:
                windows.append(w)
    return windows


def _bind_game_window(windows=None):
    """游戏窗口绑定（v3 优先级）：
    ① 进程名匹配（设置 game_process_name，空=devices.json 自动）且窗口类 UnrealWindow，
       多个命中取可见面积最大（替代 Get-Process MainWindowHandle 启发式）；
    ② 标题关键词兜底（设置 game_window_keywords，逗号分隔任一子串命中），取面积最大；
    ③ 无命中 → (None, None, 全量清单)（调用方决定报错/回退）。
    返回 (hwnd, 命中窗口元数据|None, 全量窗口清单)；
    绑定结果 hwnd 每轮由 _OCR_PS_WINDOW 的 IsWindow 校验，失效走重绑/回退。"""
    if windows is None:
        windows = _ocr_windows()
    if not windows:
        return None, None, windows

    def area(w):
        try:
            return max(0, int(w.get("w") or 0)) * max(0, int(w.get("h") or 0))
        except (TypeError, ValueError):
            return 0

    cands = [w for w in windows if _window_matches_process(w, _default_game_process())
             and str(w.get("class") or "") == _OCR_GAME_WINDOW_CLASS]
    if cands:
        best = max(cands, key=area)
        return int(best.get("hwnd")), best, windows
    kws = [k.strip() for k in str(STATE.settings.get("game_window_keywords") or "").split(",")
           if k.strip()]
    if kws:
        cands = [w for w in windows
                 if any(k in str(w.get("title") or "") for k in kws)]
        if cands:
            best = max(cands, key=area)
            return int(best.get("hwnd")), best, windows
    return None, None, windows


def _ocr_capture_page(hwnd=0):
    """单页截屏+识别（背包"只扫当前页"/角色等级 OCR 共用）。
    hwnd=0 或窗口失效时回退全屏截屏（fallback=True，调用方给警告）。
    返回 (text, window_title, fallback)。失败语义：ValueError=语言包缺失；
    OSError/TimeoutExpired=脚本失败。"""
    out, err, code = _ocr_run_ps(
        _OCR_PS_COMMON + "\n" + _OCR_PS_WINDOW + "\n" + _OCR_PS_CAPTURE,
        {"__HWND__": str(int(hwnd or 0)), "__ROI__": ""})
    _ocr_check_errors(out, err, code)
    m = re.search(r"__OCR_META__\t(.*)", out)
    meta = m.group(1).split("\t") if m else ["", "fallback"]
    fallback = len(meta) < 2 or meta[1] == "fallback" or not meta[0]
    body = out.split("__OCR_TEXT__\n", 1)[1] if "__OCR_TEXT__\n" in out else ""
    return body.strip(), (meta[0] if not fallback else ""), fallback


def _ocr_capture_png(hwnd=0):
    """整窗截图（DPI 感知，不做识别）→ {image, width, height, fallback}，
    供 /api/ocr/capture 前端网格标定。fallback=True 表示绑定窗口在截图瞬间失效、
    PS 已回退整屏截取——该图按窗口尺寸解释会系统性错位，端点必须透传给前端。"""
    out, err, code = _ocr_run_ps(
        _OCR_PS_COMMON + "\n" + _OCR_PS_WINDOW + "\n" + _OCR_PS_SHOT,
        {"__HWND__": str(int(hwnd or 0)), "__ROI__": ""})
    if "__OCR_ERR__" in out or (code not in (0, None) and "__OCR_PNG__" not in out):
        raise OSError(f"截图脚本执行失败：{(err or out).strip()[-200:]}")
    meta = re.search(r"__OCR_SHOT__\t(\d+)\t(\d+)\t([^\t\r\n]*)", out)
    body = out.split("__OCR_PNG__\n", 1)[1].strip() if "__OCR_PNG__\n" in out else ""
    if not body:
        raise OSError("截图脚本未返回图像数据")
    return {"image": body, "width": int(meta.group(1)) if meta else 0,
            "height": int(meta.group(2)) if meta else 0,
            "fallback": bool(meta and meta.group(3).strip() == "fallback")}


def _ocr_cell_points(layout, w, h):
    """网格坐标计算（纯函数，selftest 覆盖）：first/step 比例 × 窗口宽高 →
    row-major（先行后列）各格在窗口内的绝对像素坐标。"""
    first = layout.get("first_cell") if isinstance(layout.get("first_cell"), dict) else {}
    step = layout.get("step") if isinstance(layout.get("step"), dict) else {}
    fx, fy = _ratio01(first.get("x")), _ratio01(first.get("y"))
    dx, dy = _ratio01(step.get("dx")), _ratio01(step.get("dy"))
    cols = _clamp_int(layout.get("cols"), 1, 20, 1)
    rows = _clamp_int(layout.get("rows"), 1, 20, 1)
    points = []
    for row in range(rows):
        for col in range(cols):
            points.append((int(round((fx + dx * col) * w)),
                           int(round((fy + dy * row) * h))))
    return points


def _ocr_traverse_page(hwnd, layout, win_size):
    """单页逐格点击遍历（PS 一次跑完整页）：按注入坐标逐格点击，每格点击后
    截 detail ROI 识别，返回按点击顺序的 cell 文本列表；窗口失效返回 None。
    PS 内连续 3 格空白提前收尾（点出网格范围），其余判停由调用方按页处理。"""
    points = _ocr_cell_points(layout, win_size[0], win_size[1])
    if not points:
        return None
    # pre-flight（2026-11-04 实测缺陷）：标定两点同行时 dy≈0，全部点落在同一条
    # 水平线 → "只在同一行来回点"。直接报错，不浪费几十次真实点击。
    if len(points) > 1 and len({y for _x, y in points}) == 1:
        raise ValueError(
            "网格布局的行距 dy 为 0：所有点击点落在同一条水平线上（标定的两点"
            "在同一行）。请重新标定：第二/第三点必须点在下一行的格子上。")
    ps = _OCR_PS_COMMON + "\n" + _OCR_PS_WINDOW + "\n" + _OCR_PS_TRAVERSE
    out, err, code = _ocr_run_ps(
        ps, {"__HWND__": str(int(hwnd)), "__ROI__": _roi_str(layout.get("detail_roi")),
             "__POINTS__": json.dumps([list(p) for p in points])},
        timeout=min(600, 30 + len(points) * 2))
    if "__OCR_NOWIN__" in out:
        return None
    _ocr_check_errors(out, err, code)
    cells, title = {}, ""
    m = re.search(r"__OCR_META__\t([^\t\r\n]*)", out)
    if m:
        title = m.group(1)
    for line in out.splitlines():
        if not line.startswith("__OCR_CELL__:"):
            continue
        try:
            _head, idx, payload = line.split(":", 2)
            cells[int(idx)] = json.loads(payload)
        except ValueError:
            continue
    return [cells[k] for k in sorted(cells)]


def _ocr_scroll(hwnd, safe_kw):
    """滚轮下翻一页：游戏窗口保持前台 + 滚轮下滚 5 格。hwnd 失效时 PS 内回退
    标题关键词查找（v2 行为）。失败静默——翻不动由"滚后文本与之前页相同"判停。"""
    ps = _OCR_PS_SCROLL.replace("__HWND__", str(int(hwnd or 0))).replace(
        "__KEYWORDS__", safe_kw)
    encoded = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
    try:
        subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", encoded],
                       capture_output=True, timeout=30, creationflags=0x08000000)
    except (OSError, subprocess.TimeoutExpired):
        pass


def parse_count_cn(text):
    """OCR 数量文本 → 整数（纯函数，selftest 覆盖；MAA/MaaEnd 口径移植）。

    支持：千分位逗号（"1,234"→1234）、小数、量级词（万/千/亿/K/M/k/m/b，
    "1.2万"→12000、"3K"→3000、"2.5亿"→250000000）、可选 ×/x/* 前缀。
    无量级词的小数截断为整数（OCR 噪声不放大数量）；解析失败/负数 → None。"""
    if text is None:
        return None
    t = re.sub(r"[\s,，]", "", str(text))
    m = re.fullmatch(r"[×xX*＊:：+]?(\d+(?:\.\d+)?)([万亿千KkMmBb]?)", t)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2)
    if unit:
        num *= {"万": 1e4, "千": 1e3, "亿": 1e8,
                "k": 1e3, "m": 1e6, "b": 1e9}[unit.lower()]
        return max(0, int(round(num)))
    return max(0, int(num))


# 数量必须「紧邻」名称：中间只允许 ×/x/*/：/-/空格 等分隔符（≤8 字符）。
# 无界跳取时同线多道具会串位——"雏鸟的希冀 海之泪 3" 里 3 会被前一名抢走。
# group(1)=数字、group(2)=量级词；group(0) 含前导分隔符，取数用 group(1)+group(2)。
_OCR_COUNT_RE = re.compile(
    r"[×xX*＊χ✕*:：\-—\s]{0,8}(\d[\d,，]*(?:\.\d+)?)[ \t]*([万亿千KkMmBb]?)")


# ×N 数量徽标（2026-11-04 实测取证：详情面板的 ×数量徽标与名称分两处，须全文找）：
# 标记符 ×/χ/✕/＊/x/X/* + 可选空格 + 数字（千分位/小数）+ 可选量级词。
# x/X/* 与英文字串冲突时以「独立 token」为准：标记符前一字符不得是 ASCII
# 字母/数字（"max42"/"4x2" 不计；中文紧邻的 "焦子×996" 放行）。
_OCR_BADGE_RE = re.compile(
    r"(?:^|[^0-9A-Za-z])([×χ✕＊xX*])\s*(\d[\d,，]*(?:\.\d+)?)[ \t]*([万亿千KkMmBb]?)")


def _ocr_badge_count(text):
    """格文本全文首个 ×N 徽标 token → 数量（int）；无徽标/全无效 → None。

    ×0 或解析失败的徽标跳过继续找（0 会把持有量覆盖成没有，一律不作数量）。"""
    for m in _OCR_BADGE_RE.finditer(text or ""):
        count = parse_count_cn(m.group(2) + m.group(3))
        if count:
            return count
    return None


def _inventory_candidates():
    """库存实名候选表（match_inventory_text/_parse_cell_item 共用的单一逻辑源）：
    item_names 别名 + display_name（含去括号前缀），归一化去重后最长优先。"""
    inv = {m["id"]: m for m in planner.MATERIALS.get("targets", [])}
    for e in planner.MATERIALS.get("extra_materials", []):
        inv[e["id"]] = e
    item_names = planner.MATERIALS.get("item_names") or {}
    candidates = []  # (norm_alias, mat_id)
    for mat_id, entry in inv.items():
        aliases = list(item_names.get(mat_id) or [])
        disp = entry.get("display_name") or entry.get("name") or ""
        aliases.append(disp)
        aliases.append(disp.split("（")[0])
        seen = set()
        for a in aliases:
            if not a or len(a) < 2:
                continue
            na = _ocr_norm(a)
            if na in seen:
                continue
            seen.add(na)
            candidates.append((na, mat_id))
    candidates.sort(key=lambda x: -len(x[0]))  # 最长优先：先匹配更具体的实名
    return candidates


def _parse_cell_item(text, candidates=None):
    """详情面板 OCR 文本 → (mat_id, count)（纯函数，selftest 覆盖）。

    数量提取 ×N 徽标优先（2026-11-04 实测：详情面板的 ×数量徽标与名称分两处，
    "海之泪"+"×42"；而描述句里的数字是噪声）：先在整个格文本找首个独立 ×N
    token（_ocr_badge_count），找到即数量；找不到回退 match_inventory_text 的
    同行「名称+右侧最近数字」匹配。详情面板存在名称与数量分两行的排版（v2 的
    跨行防线只针对整页 OCR 的列串位，单格详情文本只含一个道具，跨行取紧随
    数字是安全的）：名称行之后最多看 2 行，首个不含其他实名的行的第一个数字
    记为数量。计数 0 一律无效（图标角标/数值噪声，0 会把持有量覆盖成没有）
    → 有名但无有效数量返回 (None, None)；名称未命中同样返回 (None, None)。"""
    if candidates is None:
        candidates = _inventory_candidates()
    lines = [ln for ln in (text or "").splitlines() if _ocr_norm(ln)]
    for i, raw in enumerate(lines):
        line = _ocr_norm(raw)
        hit = next(((na, mat_id, line.find(na)) for na, mat_id in candidates
                    if line.find(na) >= 0), None)
        if not hit:
            continue
        na, mat_id, pos = hit
        badge = _ocr_badge_count(text)  # ×N 徽标优先：与名称分离的独立数量位
        if badge:
            return mat_id, badge
        # 锚定 match（只认名称紧后的分隔符+数字）：同线多道具时前一名不许跳过
        # 中间的其他道具名去抢数字（"雏鸟的希冀 海之泪 3" → 3 归海之泪）
        mnum = _OCR_COUNT_RE.match(line[pos + len(na):])
        if mnum:
            count = parse_count_cn(mnum.group(1) + mnum.group(2))
            if count:  # 0 视为角标/数值噪声，不作数量
                return mat_id, count
        for nxt in lines[i + 1:i + 3]:  # 名称行无数字 → 向下最多看 2 行
            nline = _ocr_norm(nxt)
            if any(na2 in nline for na2, _m in candidates):
                break  # 撞到其他道具名：不许抢它的数字
            # 数量行形态约束：仅认"分隔符+数字(+量级词)"的短行——详情面板的数量行
            # 是"×42"形态；描述句（"从海囚获取的…常被用作角色突破材料。"）即使
            # OCR 误读出数字也不许当数量
            mnum = _OCR_COUNT_RE.fullmatch(nline)
            if mnum:
                count = parse_count_cn(mnum.group(1) + mnum.group(2))
                if count:  # 0 同样无效
                    return mat_id, count
            break  # 数量行只认紧跟的第一行；描述行不取数
        return None, None
    return None, None


# ---- OCR 遍历取消（代数令牌，评审 2026-11-05）与单飞 ----
# 旧方案「每轮遍历入口无条件 clear」存在竞态：取消 set 后旧 handler 线程仍在 PS
# 页内（背包单页上限 ~86s / 角色 240s），用户立刻再点识别，新一轮入口 clear 会把
# 发给旧轮的取消吞掉——旧遍历继续点满所有页，与新轮同时用全局鼠标点击互相污染。
# 改为代数令牌：每轮遍历入口代数 +1，取消只对「登记时的当前代数」生效——旧轮的
# 取消绝不泄漏进新一轮，也无需入口 clear，从根上消除该竞态。
_OCR_CANCEL = threading.Event()   # 取消事件（配合 _OCR_CANCEL_GEN 代数令牌判定）
_OCR_GEN_LOCK = threading.Lock()  # 代数推进与取消定向的登记锁（判定处 int 读 GIL 原子）
_OCR_GEN = 0                      # 遍历代数计数器（每轮遍历入口 +1，永不复用）
_OCR_CANCEL_GEN = 0               # 取消针对的代数（无活跃遍历时指向最近一代=等效无操作）
# OCR 单飞锁：背包/角色遍历共用全局 SetCursorPos/mouse_event，绝不并发（并发会互相
# 污染点击与识别结果）——两个遍历端点非阻塞抢占，抢不到即 409。
# /api/ocr/capture 只截屏不点击，不占用该锁。
_OCR_RUN_LOCK = threading.Lock()


def _ocr_gen_begin():
    """新一轮遍历登记：代数 +1 并返回本轮代数（取消按代数定向）。"""
    global _OCR_GEN
    with _OCR_GEN_LOCK:
        _OCR_GEN += 1
        return _OCR_GEN


def _ocr_cancel_hit(gen):
    """gen 代遍历是否被取消：事件置位且目标代数匹配才生效。"""
    return _OCR_CANCEL.is_set() and _OCR_CANCEL_GEN == gen


def _ocr_cancel_active():
    """取消当前正在跑的那一代遍历（/api/ocr/cancel），返回其代数。无活跃遍历时
    目标指向最近一代——再无人检查该代数，等价无操作，绝不影响下一轮。"""
    global _OCR_CANCEL_GEN
    with _OCR_GEN_LOCK:
        _OCR_CANCEL_GEN = _OCR_GEN
        gen = _OCR_GEN
    _OCR_CANCEL.set()
    return gen


def _ocr_cancel_file(gen):
    """gen 代角色遍历的取消哨兵文件路径：角色遍历的"每头像循环"在 PS 子进程里
    跑，Python Event 传不进去 → 取消时落哨兵文件，PS 脚本每头像循环头 Test-Path
    探测到即提前收尾（已点击头像的结果照常输出）。路径按代数区分——旧轮哨兵与
    新轮互不可见（与代数令牌同一竞态防线）；纯标准库零依赖。"""
    return os.path.join(tempfile.gettempdir(), "oknte_ocr_cancel_%d.flag" % gen)


def _ocr_cancel_file_write(gen):
    """gen 代取消哨兵落盘（/api/ocr/cancel 调用）。写失败不报错：Event 侧取消
    仍生效，角色遍历退化为跑满子进程超时。"""
    try:
        with open(_ocr_cancel_file(gen), "w", encoding="utf-8") as f:
            f.write("cancel")
    except OSError:
        pass


def _ocr_cancel_file_clear(gen):
    """清除 gen 代取消哨兵（该代遍历收尾自清；子进程超时路径的残留亦无害——
    该代数永不复用，文件不会再被任何一轮引用）。"""
    try:
        os.remove(_ocr_cancel_file(gen))
    except OSError:
        pass


def _run_backpack_ocr(mode="traverse"):
    """实验性背包 OCR v3：窗口绑定（进程名+UnrealWindow 类择优）+ 网格逐格点击遍历 +
    滚轮翻页 + 道具实名匹配（同名跨格取最大，不累加——MAA merge 口径）。
    系统组件零第三方依赖。

    mode="page"：不点击，仅整窗识别当前页（保留 v2 单页语义，共用单页捕获 helper）。

    游戏窗口找不到 → raise _OcrWindowNotFound（端点转 502 明确报错，不再静默回退全屏）；
    语言包缺失 → ValueError；脚本失败 → OSError/TimeoutExpired。
    返回 {matches, unmatched_lines, unknown_items, pages, cells_scanned, window,
    warnings, text}。
    """
    gen = _ocr_gen_begin()  # 代数登记：取消按代数定向，旧轮的取消绝不泄漏进新一轮
    hwnd, meta, _wins = _bind_game_window()
    if not hwnd:
        raise _OcrWindowNotFound("未找到游戏窗口（" + _default_game_process()
                                 + "）——请先启动游戏，或在设置/窗口列表中手动选择")
    window_id = {"process": str(meta.get("process") or ""),
                 "title": str(meta.get("title") or "")}
    if mode == "page":
        text, title, fallback = _ocr_capture_page(hwnd=hwnd)
        r = match_inventory_text(text)
        warnings = (["未找到游戏窗口（" + window_id["process"] + "），已回退全屏截屏"]
                    if fallback else [])
        window_id["title"] = title or window_id["title"]
        return {"matches": r["matches"], "unmatched_lines": r["unmatched_lines"],
                "unknown_items": [],  # 单页模式无逐格分类，unknown 仅 traverse 收集
                "pages": 1, "cells_scanned": 0, "window": window_id,
                "warnings": warnings, "text": text}

    layout = _ocr_layout()
    max_pages = _clamp_int(STATE.settings.get("ocr_max_pages"), 1, 20, 5)
    safe_kw = str(STATE.settings.get("game_window_keywords") or "").strip().replace("'", "''")
    win_w = _clamp_int(meta.get("w"), 1, 100000, 1)
    win_h = _clamp_int(meta.get("h"), 1, 100000, 1)
    cands = _inventory_candidates()
    warnings, page_norms = [], set()
    matches, unmatched_lines = {}, []
    unknown_items, unknown_seen = [], set()  # 未收录格清单（首行归一化去重）
    seen_names = set()
    cells_scanned, pages = 0, 0
    empty_streak = 0
    stop_reason = None
    combined = []
    while pages < max_pages:
        if _ocr_cancel_hit(gen):  # 取消在页首生效：不再发起下一页点击/翻页
            stop_reason = "用户取消，遍历提前结束"
            break
        cells = _ocr_traverse_page(hwnd, layout, (win_w, win_h))
        if cells is None:  # hwnd IsWindow 校验失败 → 重新绑定一次
            hwnd, meta, _wins = _bind_game_window()
            if not hwnd:
                if pages == 0:
                    raise _OcrWindowNotFound(
                        "未找到游戏窗口（" + _default_game_process()
                        + "）——请先启动游戏，或在设置/窗口列表中手动选择")
                warnings.append("游戏窗口已关闭，遍历提前结束")
                break
            win_w = _clamp_int(meta.get("w"), 1, 100000, win_w)
            win_h = _clamp_int(meta.get("h"), 1, 100000, win_h)
            window_id = {"process": str(meta.get("process") or ""),
                         "title": str(meta.get("title") or "")}
            cells = _ocr_traverse_page(hwnd, layout, (win_w, win_h))
            if cells is None:
                warnings.append("游戏窗口已关闭，遍历提前结束")
                break
        pages += 1
        # 重复格判据按页内计：滚轮翻页的重叠行位于下一页 row-major 顺序的开头，
        # 与之前页的重复是翻页的必然形态（max-merge 口径不亏数量），绝不能计入
        # 判停——否则网格 ≥4 行或滚动推进 <行数-3 时，下一页新行永远扫不到。
        # 逐格分类（2026-11-04 实测修正：未收录 ≠ 空，未收录格绝不得判停整页）：
        #   a) 空白 → empty_streak（连续 3 格 = 点出网格范围才停——与 PS 端
        #      _OCR_PS_TRAVERSE 的 3 连空 break 对齐：返回 cells 永不含 ≥4 连续
        #      空串，阈值再高永不触发，短背包只会白扫一整页）；
        #   b) 实名命中 → 聚合（同名取最大），empty_streak 清零；
        #   c) 有文本但未收录 → 收集 unknown_items（×N 徽标计数，首行归一化去重，
        #      供 /api/ocr/map-unknown 人工映射）；新首行同时视作页级新信息——
        #      整页未收录不得以「本页未发现新道具」截断后续页（否则多页未知背包
        #      每轮 OCR 只能推进一页，unknown_items 持续漏报）；
        #   页内与之前格文本完全重复 → 真重复：无新信息，不进 unknown、不重复聚合。
        page_cell_norms = set()
        empty_streak = 0
        page_new = False
        for cell_text in cells:
            if _ocr_cancel_hit(gen):  # 格间取消（2026-11-05 用户实测：遍历必须可停）：
                stop_reason = "用户取消，遍历提前结束"  # 本页 PS 点击期间收到的取消
                break  # 立刻生效——取消点之后的格不再解析，已完成部分原样返回
            cells_scanned += 1
            combined.append(cell_text)
            norm = re.sub(r"\s+", "", cell_text)
            if not norm:
                empty_streak += 1  # a) 空白：点出网格范围
            elif norm in page_cell_norms:
                empty_streak = 0   # 页内真重复：本格无新信息
            else:
                empty_streak = 0
                page_cell_norms.add(norm)
                mat_id, count = _parse_cell_item(cell_text, cands)
                if mat_id is not None:
                    if mat_id not in seen_names:
                        page_new = True
                    seen_names.add(mat_id)
                    matches[mat_id] = max(matches.get(mat_id, 0), count)  # 同名跨格取最大
                else:
                    # c) 未收录：不判停，收集待映射清单（同时进 unmatched_lines
                    # 供前端「未命中原文」折叠展示——前端契约不变）
                    first_line = cell_text.strip().splitlines()[0][:80]
                    key = _ocr_norm(first_line)
                    if key not in unknown_seen and len(unknown_items) < 60:
                        unknown_seen.add(key)
                        unknown_items.append({"name_text": first_line,
                                              "count": _ocr_badge_count(cell_text)})
                        page_new = True  # 新未收录首行也是新信息：继续翻页收集
                    if len(unmatched_lines) < 60:
                        unmatched_lines.append(first_line)
            if empty_streak >= 3:
                stop_reason = "连续 3 格空白（点出网格范围），遍历提前结束"
                break
        if stop_reason:
            break
        if _ocr_cancel_hit(gen):  # 页末复查：整页恰好解析完时收到的取消不必再翻页
            stop_reason = "用户取消，遍历提前结束"
            break
        page_norm = re.sub(r"\s+", "", "\n".join(cells))
        if page_norm in page_norms:
            stop_reason = "翻页后内容与之前页相同，遍历结束"
            break
        page_norms.add(page_norm)
        if not page_new:
            stop_reason = "本页未发现新道具，遍历结束"
            break
        if pages >= max_pages:
            break
        # 滚轮翻页（占位符修复后滚动脚本恢复生效）+ 等待渲染
        _ocr_scroll(hwnd, safe_kw)
        time.sleep(max(0.1, layout.get("page_wait_ms", 800)) / 1000.0)
    if stop_reason:
        warnings.append(stop_reason)
    return {"matches": matches, "unmatched_lines": unmatched_lines,
            "unknown_items": unknown_items,
            "pages": pages, "cells_scanned": cells_scanned, "window": window_id,
            "warnings": warnings, "text": "\n".join(combined)}


def _ocr_map_unknown(name_text, mat_id):
    """未收录道具实名映射（POST /api/ocr/map-unknown 后端语义，selftest 覆盖）：
    把人工确认的 OCR 首行文本追加为 materials.json item_names[mat_id] 别名
    （归一化查重去重、只追加不覆盖已有条目），原子写回并 reload_tables 热生效
    ——此后同名 OCR 自动命中（_inventory_candidates 每次现读 item_names，
    下次识别即生效，无需重启）。
    校验：mat_id ∈ planner.ALL_ORDER；name_text 归一化后 2-24 字（原文另做
    控制字符过滤、空白折叠与 64 字截断，防止噪声原文撑大 materials.json）；
    归一化同名已归属其它材料 → 拒绝（候选最长优先会静默遮蔽后登记的映射，
    同一实名只能归属一个材料）。
    先落盘后改内存：写盘失败（OSError→500）时内存零污染，用户重试会真实重写
    而非误命中幂等分支假成功、重启丢别名。
    非法输入 raise ValueError（端点转 400）；磁盘写入失败 raise OSError。
    返回更新后的 item_names。"""
    mat_id = str(mat_id or "").strip()
    if mat_id not in planner.ALL_ORDER:
        raise ValueError(f"未知材料 id：{mat_id or '（空）'}——请从材料目录选择")
    # 原文消毒：控制字符→空格、空白折叠、截断 ≤64（norm 2-24 管语义，这里管体积）
    name_text = re.sub(r"[\x00-\x1f\x7f]", " ", str(name_text or ""))
    name_text = re.sub(r"\s+", " ", name_text).strip()[:64]
    norm = _ocr_norm(name_text)
    if not 2 <= len(norm) <= 24:
        raise ValueError("name_text 归一化后须为 2-24 字（当前 " + str(len(norm)) + " 字）")
    with _MATERIALS_LOCK:  # 读-改-写整体持锁：与图鉴/display 写者互斥（防并发丢更新）
        item_names = planner.MATERIALS.get("item_names") or {}
        aliases = [a for a in (item_names.get(mat_id) or []) if isinstance(a, str)]
        if any(_ocr_norm(a) == norm for a in aliases):
            return item_names  # 同材料重复映射：幂等去重（此时内存与盘一致）
        conflict = next((mid for na, mid in _inventory_candidates()
                         if na == norm and mid != mat_id), None)
        if conflict:
            raise ValueError(f"实名「{name_text}」已归属材料 {conflict}——"
                             "同一实名只能映射一个材料（重复登记会被匹配静默遮蔽）")
        # 先落盘后改内存：写盘失败时 planner.MATERIALS 零污染，重试仍会真实写盘
        snapshot = dict(planner.MATERIALS)
        snapshot["item_names"] = {**item_names, mat_id: aliases + [name_text]}
        _save_json_atomic(os.path.join(DATA_DIR, "materials.json"), snapshot)
        planner.reload_tables()  # 落盘成功才从盘重载内存（失败路径内存保持原状）
    return planner.MATERIALS.get("item_names") or {}


def match_inventory_text(text):
    """OCR 文本 → 库存命中表（纯函数，selftest 覆盖）。

    归一化：全角→半角、去空白、「」剥离、异体字（眛/昧 互换）。
    匹配：候选 = item_names 别名 + display_name（含去括号前缀），最长优先；
    每行可命中多个不重叠候选，数量=名称右侧最近的独立数字（parse_count_cn 口径：
    支持千分位逗号/小数/量级词 万千亿K M b）；同一道具跨行取最大
    （自动翻页的重叠行天然按行去重）。返回 {matches, unmatched_lines}。
    """
    candidates = _inventory_candidates()
    matches, matched_lines, seen_lines = {}, [], set()
    unmatched_lines = []
    for raw in (text or "").splitlines():
        line = _ocr_norm(raw)
        if not line or line in seen_lines:
            continue
        seen_lines.add(line)
        line_hits = []  # (start, end, mat_id, count)
        for na, mat_id in candidates:
            start = 0
            while True:
                pos = line.find(na, start)
                if pos < 0:
                    break
                end = pos + len(na)
                if any(not (end <= s or pos >= e2) for s, e2, _x, _c in line_hits):
                    start = pos + 1
                    continue
                # 锚定 match：数量必须紧邻名称（只隔分隔符），同线多道具不串位
                mnum = _OCR_COUNT_RE.match(line[end:])
                if not mnum:
                    break
                count = parse_count_cn(mnum.group(1) + mnum.group(2))
                if not count:  # 0/None 一律不作数量：与 traverse 路径「0 一律不作
                    break      # 数量」守卫（_parse_cell_item）口径一致——图标角标
                               # 噪声"0"不得经 page 模式预填进命中表
                line_hits.append((pos, end + mnum.end(), mat_id, count))
                break
        if not line_hits:
            if len(unmatched_lines) < 60:
                unmatched_lines.append(raw.strip())
            continue
        matched_lines.append(line)
        for _s, _e, mat_id, count in line_hits:
            matches[mat_id] = max(matches.get(mat_id, 0), count)
    return {"matches": matches, "unmatched_lines": unmatched_lines}


_OCR_VARIANT_MAP = {"眛": "昧"}


def _ocr_norm(s, keep_space=False):
    """OCR 文本归一化：全角→半角、「」剥离、异体字互换；默认去空白（实名/名字
    子串匹配不容忍 OCR 空格噪声——"妄想彼 端的一页" 必须命中 "妄想彼端的一页"），
    keep_space=True 时把空白折叠为单空格（等级/异能解析需要：两个数字之间、
    都市特技 "0/5 0/2" 两档之间靠空格分隔，去空白会把两档并成 "0/50/2"）。"""
    table = {0xFF01 + i: 0x21 + i for i in range(94)}
    s = (s or "").translate(table)
    s = re.sub(r"\s+", "" if not keep_space else " ", s).strip()
    s = s.replace("「", "").replace("」", "")
    for a, b in _OCR_VARIANT_MAP.items():
        s = s.replace(a, b)
    return s


def match_character_text(text):
    """角色列表 OCR 文本 → 等级表（纯函数，selftest 覆盖）。

    用户确认的行格式：每行 = 角色名 + 等级 + 异能等级（两个数字）。
    角色名候选 = 本系统角色名 + 游戏目录角色名（归一化后最长优先）；
    行内按顺序取前两个数字：第 1 个=角色等级，第 2 个=异能等级（只有一个数字时
    只给 level）。返回 {"characters": [{name, level, ability}], "unmatched_lines"}；
    同名多行取首次出现（列表页不会重复）。角色等级 1-999、异能 0-99 越界丢弃。
    """
    names = {}
    for c in STATE.characters:
        if c.get("name"):
            names[_ocr_norm(c["name"])] = c["name"]
    try:
        data = game_data.load()
    except (OSError, ValueError):
        data = {}
    for entry in data.get("characters", []):
        if isinstance(entry, dict) and entry.get("name"):
            names.setdefault(_ocr_norm(entry["name"]), entry["name"])
    candidates = sorted(((k, v) for k, v in names.items() if len(k) >= 1),
                        key=lambda x: -len(x[0]))  # 单字名（浔/零/翳）合法；最长优先防误配
    out, seen, unmatched_lines = [], set(), []
    for raw in (text or "").splitlines():
        line = _ocr_norm(raw, keep_space=True)  # 保留空格：等级/异能两个数字靠空格分隔
        if not line or line in seen:
            continue
        seen.add(line)
        hit_key = next((k for k, v in candidates if k in line), None)
        if not hit_key:
            if len(unmatched_lines) < 60:
                unmatched_lines.append(raw.strip())
            continue
        name = names[hit_key]
        if name in [x["name"] for x in out]:
            continue  # 同名取首次出现（列表页不重复）
        after = line[line.find(hit_key) + len(hit_key):]
        nums = re.findall(r"\d{1,3}", after)
        level = int(nums[0]) if nums and 1 <= int(nums[0]) <= 999 else None
        ability = (int(nums[1]) if len(nums) > 1 and 0 <= int(nums[1]) <= 99
                   else None)
        if level is None:
            if len(unmatched_lines) < 60:
                unmatched_lines.append(raw.strip())
            continue
        out.append({"name": name, "level": level, "ability": ability})
    return {"characters": out, "unmatched_lines": unmatched_lines}


_OCR_PS_CAPTURE = r'''
# ---- 截屏（$meta 区域，应用 detail ROI 或整窗）+ WinRT 识别 ----
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$engine = New-OcrEngine
$text = Invoke-Ocr (New-Object System.Drawing.Rectangle($meta.x, $meta.y, $meta.w, $meta.h)) $engine
Write-Output ("__OCR_META__" + "`t" + $meta.window + "`t" + $(if ($meta.fallback) { "fallback" } else { "window" }))
Write-Output "__OCR_TEXT__"
Write-Output $text
'''

_OCR_PS_WINDOWS = r'''
# ---- 枚举全部顶层可见窗口（IsWindowVisible + 非空标题；工具窗多为无标题一并排除）----
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
# 每窗口输出一行 __OCR_WINDOWS__:<JSON>（hwnd/class/title/pid/process/exe_path/w/h），
# w/h 供绑定择优（多命中取可见面积最大）。
$rows = New-Object System.Collections.ArrayList
$cb = [Win32OCR+EnumWindowsProc]{
  param($h, $l)
  try {
    if (-not [Win32OCR]::IsWindowVisible($h)) { return $true }
    $ttl = New-Object System.Text.StringBuilder 512
    $null = [Win32OCR]::GetWindowTextW($h, $ttl, 512)
    $title = $ttl.ToString()
    if (-not $title.Trim()) { return $true }
    $cls = New-Object System.Text.StringBuilder 256
    $null = [Win32OCR]::GetClassNameW($h, $cls, 256)
    [uint32]$wpid = 0
    $null = [Win32OCR]::GetWindowThreadProcessId($h, [ref]$wpid)
    $procName = ""
    try {
      $p = Get-Process -Id $wpid -ErrorAction Stop
      $procName = $p.ProcessName
    } catch {}
    $exe = ""
    $hProc = [Win32OCR]::OpenProcess(0x1000, $false, $wpid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if ($hProc -ne [IntPtr]::Zero) {
      $exeSb = New-Object System.Text.StringBuilder 1024
      $size = [uint32]1024
      if ([Win32OCR]::QueryFullProcessImageNameW($hProc, 0, $exeSb, [ref]$size)) {
        $exe = $exeSb.ToString()
      }
      $null = [Win32OCR]::CloseHandle($hProc)
    }
    $rect = New-Object Win32OCR+RECT
    $null = [Win32OCR]::GetWindowRect($h, [ref]$rect)
    $obj = @{
      hwnd = $h.ToInt64(); class = $cls.ToString(); title = $title; pid = $wpid
      process = $procName; exe_path = $exe
      w = [Math]::Max(0, $rect.Right - $rect.Left); h = [Math]::Max(0, $rect.Bottom - $rect.Top)
    }
    [void]$rows.Add(($obj | ConvertTo-Json -Compress))
  } catch {}
  return $true
}
$null = [Win32OCR]::EnumWindows($cb, [IntPtr]::Zero)
foreach ($line in $rows) {
  Write-Output ("__OCR_WINDOWS__:" + $line)
}
'''

_OCR_PS_SHOT = r'''
# ---- 整窗截图 → base64 PNG（供前端网格标定；不做识别） ----
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$bmp = New-Object System.Drawing.Bitmap($meta.w, $meta.h)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($meta.x, $meta.y, 0, 0, (New-Object System.Drawing.Size($meta.w, $meta.h)))
$g.Dispose()
$ms = New-Object System.IO.MemoryStream
$bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
Write-Output ("__OCR_SHOT__" + "`t" + $meta.w + "`t" + $meta.h + "`t" + $(if ($meta.fallback) { "fallback" } else { "window" }))
Write-Output "__OCR_PNG__"
Write-Output ([Convert]::ToBase64String($ms.ToArray()))
$ms.Dispose()
'''

_OCR_PS_TRAVERSE = r'''
# ---- 逐格点击遍历：Python 预计算网格像素坐标注入（row-major），每格
#      SetCursorPos + 左键单击（LEFTDOWN 0x0002 / LEFTUP 0x0004）→ 等详情渲染
#      → 截 $meta 区域（detail ROI）识别，逐格输出 __OCR_CELL__:<序号>:<JSON 文本> ----
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
if ($meta.fallback) {
  Write-Output "__OCR_NOWIN__"
  exit 1
}
$Points = @()
try {
  $parsed = ConvertFrom-Json '__POINTS__'
  if ($parsed) { $Points = @($parsed) }
} catch {}
if (-not $Points) {
  Write-Output "__OCR_ERR__ 网格坐标为空"
  exit 1
}
$engine = New-OcrEngine
$null = [Win32OCR]::SetForegroundWindow($Hwnd)
Start-Sleep -Milliseconds 200
$region = New-Object System.Drawing.Rectangle($meta.x, $meta.y, $meta.w, $meta.h)
$idx = 0
$emptyStreak = 0
foreach ($pt in $Points) {
  $null = [Win32OCR]::SetCursorPos($win.x + [int]$pt[0], $win.y + [int]$pt[1])
  Start-Sleep -Milliseconds 100
  [Win32OCR]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)   # LEFTDOWN
  Start-Sleep -Milliseconds 60
  [Win32OCR]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)   # LEFTUP
  Start-Sleep -Milliseconds 350                                # 详情面板渲染等待
  $cellText = Invoke-Ocr $region $engine
  Write-Output ("__OCR_CELL__:" + $idx + ":" + ($cellText | ConvertTo-Json -Compress))
  $idx++
  if ([string]::IsNullOrWhiteSpace($cellText)) {
    $emptyStreak++
    if ($emptyStreak -ge 3) { break }   # 连续 3 格空白：已点出网格范围
  } else {
    $emptyStreak = 0
  }
}
Write-Output ("__OCR_META__" + "`t" + $meta.window + "`t" + "window")
'''

# ---- 角色等级 OCR v2：真实 UI 遍历（2026-11-04 用户截图取证；2026-11-06 实机重定标，
#      新增"弧盘"导航与弧盘页 ROI：左导航竖排 信息/弧盘/异能/觉醒；2026-11-06 实机
#      反馈二轮：300ms 等待截到过渡帧致全空+误判停 → 等待可配置 800ms；点头像后显式
#      点"信息"导航兜底；头像列滚轮下滚遍历整列，按信息页文本去重、整列无新角色 2 轮自停）----
_CHAR_OCR_FILE = os.path.join(DATA_DIR, "char_ocr_layout.json")
_CHAR_OCR_DEFAULT = {
    "avatars": {"x": 0.9572, "y": 0.182, "dy": 0.1468, "count": 5},
    "info_nav": {"x": 0.058, "y": 0.229},
    "ability_nav": {"x": 0.058, "y": 0.57},
    "arc_nav": {"x": 0.058, "y": 0.322},
    "info_roi": {"x": 0.655, "y": 0.14, "w": 0.17, "h": 0.24},
    "arc_roi": {"x": 0.63, "y": 0.13, "w": 0.17, "h": 0.125},
    "ability_roi": {"x": 0.24, "y": 0.315, "w": 0.56, "h": 0.545},
    "page_wait_ms": 800,
    "scroll": {"x": 0.9572, "y": 0.5, "notches": 3},
}


def _sanitize_char_ocr_layout(raw):
    """角色等级 OCR 布局消毒：比例钳位 0-1、头像数 1-10、页等待 200-2000ms；
    缺失/非法回落默认。"""
    if not isinstance(raw, dict):
        raise ValueError("char layout 必须是对象")
    d = _CHAR_OCR_DEFAULT
    av = raw.get("avatars") if isinstance(raw.get("avatars"), dict) else {}
    inn = raw.get("info_nav") if isinstance(raw.get("info_nav"), dict) else {}
    nav = raw.get("ability_nav") if isinstance(raw.get("ability_nav"), dict) else {}
    an = raw.get("arc_nav") if isinstance(raw.get("arc_nav"), dict) else {}
    ir = raw.get("info_roi") if isinstance(raw.get("info_roi"), dict) else {}
    ac = raw.get("arc_roi") if isinstance(raw.get("arc_roi"), dict) else {}
    ar = raw.get("ability_roi") if isinstance(raw.get("ability_roi"), dict) else {}
    sc = raw.get("scroll") if isinstance(raw.get("scroll"), dict) else {}

    def _r(value, default):
        try:
            return max(0.0, min(1.0, round(float(value), 4)))
        except (TypeError, ValueError):
            return default

    return {
        "avatars": {"x": _r(av.get("x"), d["avatars"]["x"]),
                    "y": _r(av.get("y"), d["avatars"]["y"]),
                    "dy": _r(av.get("dy"), d["avatars"]["dy"]),
                    "count": _clamp_int(av.get("count"), 1, 10, d["avatars"]["count"])},
        "info_nav": {"x": _r(inn.get("x"), d["info_nav"]["x"]),
                     "y": _r(inn.get("y"), d["info_nav"]["y"])},
        "ability_nav": {"x": _r(nav.get("x"), d["ability_nav"]["x"]),
                        "y": _r(nav.get("y"), d["ability_nav"]["y"])},
        "arc_nav": {"x": _r(an.get("x"), d["arc_nav"]["x"]),
                    "y": _r(an.get("y"), d["arc_nav"]["y"])},
        "info_roi": {"x": _r(ir.get("x"), d["info_roi"]["x"]),
                     "y": _r(ir.get("y"), d["info_roi"]["y"]),
                     "w": _r(ir.get("w"), d["info_roi"]["w"]),
                     "h": _r(ir.get("h"), d["info_roi"]["h"])},
        "arc_roi": {"x": _r(ac.get("x"), d["arc_roi"]["x"]),
                    "y": _r(ac.get("y"), d["arc_roi"]["y"]),
                    "w": _r(ac.get("w"), d["arc_roi"]["w"]),
                    "h": _r(ac.get("h"), d["arc_roi"]["h"])},
        "ability_roi": {"x": _r(ar.get("x"), d["ability_roi"]["x"]),
                        "y": _r(ar.get("y"), d["ability_roi"]["y"]),
                        "w": _r(ar.get("w"), d["ability_roi"]["w"]),
                        "h": _r(ar.get("h"), d["ability_roi"]["h"])},
        "page_wait_ms": _clamp_int(raw.get("page_wait_ms"), 200, 2000,
                                   d["page_wait_ms"]),
        "scroll": {"x": _r(sc.get("x"), d["scroll"]["x"]),
                   "y": _r(sc.get("y"), d["scroll"]["y"]),
                   "notches": _clamp_int(sc.get("notches"), 1, 10,
                                         d["scroll"]["notches"])},
    }


def _char_ocr_layout():
    """读取角色等级 OCR 布局：data/char_ocr_layout.json 优先（每次读取 → 热生效）；
    缺失时落盘默认值。"""
    layout = load_json(_CHAR_OCR_FILE)
    if not isinstance(layout, dict):
        layout = {}
    clean = _sanitize_char_ocr_layout(layout)
    if not layout:
        # 缺失时落盘默认布局（自愈）：必须用别名 _save_json_atomic——本模块只以
        # 别名导入（见 import 区），裸名不存在，真机曾每次点击必抛 NameError
        _save_json_atomic(_CHAR_OCR_FILE, clean)
    return clean


_CHAR_SKILL_ANCHORS = (("普通攻击", "normal"), ("变轨技能", "variable"),
                       ("极轨终结", "orbit"), ("援护技", "assist"))


def _parse_char_info_text(text):
    """角色信息页 OCR → {name, level, level_cap}（纯函数，selftest 覆盖）。

    名字 = 已知角色名（本系统角色 + 游戏目录，归一化后最长优先）在文本中出现；
    等级 = "Lv.70/70" 或 "70/70" 的当前值（斜杠前）；level_cap = 斜杠后的上限段
    （仅识别展示，不参与应用）。名字识别失败时 name=None（前端标注"未识别"，
    不可应用）。"""
    norm_ns = _ocr_norm(text)                       # 去空白：名字子串匹配用
    norm_sp = _ocr_norm(text, keep_space=True)      # 保留空格：Lv 数字提取用
    names = {}
    for c in STATE.characters:
        if c.get("name"):
            names.setdefault(_ocr_norm(c["name"]), c["name"])
    try:
        data = game_data.load()
    except (OSError, ValueError):
        data = {}
    for entry in data.get("characters", []):
        if isinstance(entry, dict) and entry.get("name"):
            names.setdefault(_ocr_norm(entry["name"]), entry["name"])
    name = None
    for key, value in sorted(names.items(), key=lambda x: -len(x[0])):
        if len(key) >= 1 and key in norm_ns:
            name = value
            break
    level = None
    level_cap = None
    # 斜杠对优先（当前值/上限值都取）；仅 "Lv.70" 无斜杠时退回单数（不编造上限）。
    # 2026-11-06 实机 OCR：字符间带空格（"L v ： 75 / 80"，L 还会误读成"廴"）——
    # 故 l\s*v 容忍 L/v 分离，斜杠两侧容忍空格，且回退对不依赖 Lv 前缀。
    m = (re.search(r"l\s*v[.:．]?\s*(\d{1,3})\s*/\s*(\d{1,3})", norm_sp, re.I)
         or re.search(r"(\d{1,3})\s*/\s*(\d{1,3})", norm_sp)
         or re.search(r"l\s*v[.:．]?\s*(\d{1,3})", norm_sp, re.I))
    if m:
        value = int(m.group(1))
        if 1 <= value <= 999:
            level = value
        if m.lastindex and m.lastindex >= 2:
            cap = int(m.group(2))
            if 1 <= cap <= 999:
                level_cap = cap
    return {"name": name, "level": level, "level_cap": level_cap}


def _parse_char_ability_text(text):
    """异能页 OCR → 四技能与都市特技当前等级（纯函数，selftest 覆盖）。

    锚定技能实名（普通攻击/变轨技能/极轨终结/援护技）后取首个 N/10 的分子；
    都市特技两档按分母区分：N/5 → talent1、N/2 → talent2（2026-11-03 截图实证
    安魂曲为 0/5 与 0/2）。锚点缺失的键保持 None（不编造）。
    2026-11-06 实机 OCR 字符间带空格（"普 通 攻 击 6 / 10"）→ 锚点匹配与技能数字
    用去空格文本；档位间空格是 "0/5 0/2" 两档的唯一分隔（并成 "0/50/2" 无法分档），
    故斜杠紧凑化（"6 / 10"→"6/10"）但保留其余空格，特技在紧凑文本上提取。"""
    norm = _ocr_norm(text, keep_space=True)
    compact = re.sub(r"\s*[/／]\s*", "/", norm)
    stripped = compact.replace(" ", "")
    out = {"normal": None, "variable": None, "orbit": None, "assist": None,
           "talent1": None, "talent2": None}
    for anchor, key in _CHAR_SKILL_ANCHORS:
        pos = stripped.find(anchor)
        if pos < 0:
            continue
        m = re.search(r"(\d{1,2})/\d{1,2}", stripped[pos + len(anchor):])
        if m and 0 <= int(m.group(1)) <= 99:
            out[key] = int(m.group(1))
    for m in re.finditer(r"(\d{1,2})/([52])(?!\d)", compact):
        num, den = int(m.group(1)), int(m.group(2))
        key = "talent1" if den == 5 else "talent2"
        if out[key] is None and 0 <= num <= 5:
            out[key] = num
    return out


def _parse_char_arc_text(text):
    """弧盘页 OCR → {arc_name, arc_level, arc_level_cap}（纯函数，selftest 覆盖）。

    弧盘实名 = 已知弧盘名（游戏目录 weapons + 本系统角色已绑 weapon，归一化后
    最长优先）在文本中出现（2026-11-06 截图口径："漆黑青春妄想 Lv: 60/60"）；
    arc_level = "Lv: 60/60" 斜杠前当前值（clamp 0-999），arc_level_cap = 斜杠后
    上限值（缺失 None）。实名未收录时 arc_name=None（前端用 data-arcmap 下拉
    手动指定，不可直接应用）。"""
    norm_ns = _ocr_norm(text)                       # 去空白：实名子串匹配用
    norm_sp = _ocr_norm(text, keep_space=True)      # 保留空格：Lv 数字提取用
    weapons = {}
    try:
        data = game_data.load()
    except (OSError, ValueError):
        data = {}
    for entry in data.get("weapons", []):
        if isinstance(entry, dict) and entry.get("name"):
            weapons.setdefault(_ocr_norm(entry["name"]), entry["name"])
    for c in STATE.characters:
        w = c.get("weapon")
        if isinstance(w, str) and w.strip():
            weapons.setdefault(_ocr_norm(w), w.strip())
    arc_name = None
    for key, value in sorted(weapons.items(), key=lambda x: -len(x[0])):
        if len(key) >= 1 and key in norm_ns:
            arc_name = value
            break
    arc_level = None
    arc_level_cap = None
    # 2026-11-06 实机 OCR 空格形态（同 _parse_char_info_text）：l\s*v 容忍分离、
    # 斜杠两侧容忍空格、回退对不依赖 Lv 前缀
    m = (re.search(r"l\s*v[:.．]?\s*(\d{1,3})\s*/\s*(\d{1,3})", norm_sp, re.I)
         or re.search(r"(\d{1,3})\s*/\s*(\d{1,3})", norm_sp))
    if m:
        value = int(m.group(1))
        if 0 <= value <= 999:  # 弧盘等级允许 0（未升级），上限与角色等级一致
            arc_level = value
        cap = int(m.group(2))
        if 1 <= cap <= 999:
            arc_level_cap = cap
    return {"arc_name": arc_name, "arc_level": arc_level,
            "arc_level_cap": arc_level_cap}


def _run_chars_ocr():
    """角色等级遍历（实验）：逐个点头像 → 信息 ROI（名字+Lv）→ "弧盘"导航 →
    弧盘 ROI（实名+等级）→ "异能"导航 → 异能 ROI（四技能/都市特技）→ 解析。
    系统组件零第三方依赖。

    返回 {characters, window, warnings}；
    失败 raise OSError/TimeoutExpired/ValueError/_OcrWindowNotFound。"""
    gen = _ocr_gen_begin()  # 代数登记：取消按代数定向，旧轮的取消绝不泄漏进新一轮
    cancel_file = _ocr_cancel_file(gen)  # 本轮专属哨兵路径（按代区分，旧轮互不可见）
    keywords = str(STATE.settings.get("game_window_keywords") or "异环,Neverness")
    hwnd, meta, _wins = _bind_game_window()
    if not hwnd:
        raise _OcrWindowNotFound("未找到游戏窗口（" + _default_game_process()
                                 + "）——请先启动游戏，或在设置/窗口列表中手动选择")
    win_w = _clamp_int(meta.get("w"), 1, 100000, 1)
    win_h = _clamp_int(meta.get("h"), 1, 100000, 1)
    layout = _char_ocr_layout()
    av, nav = layout["avatars"], layout["ability_nav"]
    arc_nav = layout["arc_nav"]
    info_nav = layout["info_nav"]
    scroll_cfg = layout["scroll"]
    safe_kw = keywords.replace("'", "''")
    points = [[int(round(av["x"] * win_w)),
               int(round((av["y"] + av["dy"] * i) * win_h))]
              for i in range(int(av["count"]))]
    nav_px = [int(round(nav["x"] * win_w)), int(round(nav["y"] * win_h))]
    arc_nav_px = [int(round(arc_nav["x"] * win_w)),
                  int(round(arc_nav["y"] * win_h))]
    info_nav_px = [int(round(info_nav["x"] * win_w)),
                   int(round(info_nav["y"] * win_h))]
    scroll_px = {"x": int(round(scroll_cfg["x"] * win_w)),
                 "y": int(round(scroll_cfg["y"] * win_h)),
                 "notches": int(scroll_cfg["notches"])}
    page_wait = int(layout["page_wait_ms"])
    info_px = [int(round(layout["info_roi"]["x"] * win_w)),
               int(round(layout["info_roi"]["y"] * win_h)),
               int(round(layout["info_roi"]["w"] * win_w)),
               int(round(layout["info_roi"]["h"] * win_h))]
    arc_px = [int(round(layout["arc_roi"]["x"] * win_w)),
              int(round(layout["arc_roi"]["y"] * win_h)),
              int(round(layout["arc_roi"]["w"] * win_w)),
              int(round(layout["arc_roi"]["h"] * win_h))]
    abil_px = [int(round(layout["ability_roi"]["x"] * win_w)),
               int(round(layout["ability_roi"]["y"] * win_h)),
               int(round(layout["ability_roi"]["w"] * win_w)),
               int(round(layout["ability_roi"]["h"] * win_h))]
    ps = (_OCR_PS_COMMON + "\n" + _OCR_PS_WINDOW + "\n" + _OCR_PS_TRAVERSE_CHAR)
    ps = ps.replace("__HWND__", str(int(hwnd))).replace(
        "__KEYWORDS__", safe_kw).replace(
        "__POINTS__", json.dumps(points)).replace(
        "__NAV__", json.dumps(nav_px)).replace(
        "__NAV_ARC__", json.dumps(arc_nav_px)).replace(
        "__NAV_INFO__", json.dumps(info_nav_px)).replace(
        "__PAGE_WAIT__", str(page_wait)).replace(
        "__SCROLL__", json.dumps(scroll_px)).replace(
        "__ROI_INFO_PX__", ",".join(str(v) for v in info_px)).replace(
        "__ROI_ARC_PX__", ",".join(str(v) for v in arc_px)).replace(
        "__ROI_ABIL_PX__", ",".join(str(v) for v in abil_px)).replace(
        # 哨兵路径注入 PS：/api/ocr/cancel 落盘 → 每头像循环头 Test-Path 提前收尾
        # （PS 字符串单引号转义，防路径含单引号时脚本崩坏）
        "__CANCEL_FILE__", cancel_file.replace("'", "''"))
    encoded = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
    # timeout 300→240（2026-11-05 用户实测：远程会话下前端 120s 就超时断开，
    # 后端"failed to fetch"仍在跑数分钟）——后端进一步收紧，前端超时放宽到 300s
    r = subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", encoded],
                       capture_output=True, timeout=240,
                       creationflags=0x08000000)
    _ocr_cancel_file_clear(gen)  # PS 已退出，本轮哨兵即时清理（超时路径的残留亦无害：
    # 该代数永不复用——文件不会再被任何一轮引用，见 _ocr_cancel_file_clear）
    out = (r.stdout or b"").decode("utf-8", errors="replace").replace("\r\n", "\n")
    err = (r.stderr or b"").decode("utf-8", errors="replace").strip()
    if "__OCR_NOLANG__" in out:
        raise ValueError("本机没有可用的 Windows OCR 中文语言包（请在系统设置中添加）。")
    if "__OCR_ERR__" in out or "__OCR_NOWIN__" in out or (
            r.returncode not in (0, None) and not out.strip()):
        raise OSError(f"OCR 脚本执行失败：{(err or out).strip()[-200:]}")
    m = re.search(r"__OCR_META__\t([^\t\r\n]*)", out)
    warnings = []
    if not m or not m.group(1):
        warnings.append("未能确认游戏窗口标题，识别结果可能来自全屏")
    characters = []
    for line in out.splitlines():
        if not line.startswith("__OCR_CHAR__:"):
            continue
        try:
            payload = json.loads(line.split(":", 2)[2])
        except ValueError:
            continue
        info = _parse_char_info_text(payload.get("info") or "")
        abil = _parse_char_ability_text(payload.get("ability") or "")
        arc = _parse_char_arc_text(payload.get("arc") or "")
        characters.append({"index": len(characters), **info, **abil, **arc})
    if not characters:
        warnings.append("未识别到任何角色（确认游戏停留在角色信息页，且窗口未被遮挡）")
    if _ocr_cancel_hit(gen):  # PS 内每头像循环头探测哨兵提前收尾：已识别的角色保留
        warnings.append("用户取消，遍历提前结束")
    return {"characters": characters, "window": (m.group(1) if m else ""),
            "warnings": warnings}


_OCR_PS_TRAVERSE_CHAR = r'''
# ---- 角色等级遍历：逐个点击右侧头像列（滚轮下滚遍历整列直到停滞自停）→
#      点"信息"导航兜底 → 信息 ROI（名字+Lv）→ 点"弧盘"导航 → 弧盘 ROI（实名+
#      Lv: 60/60）→ 点"异能"导航 → 异能 ROI（四技能 N/10 + 都市特技 N/5、N/2）
#      → 输出 __OCR_CHAR__:<i>:<json{info,ability,arc}>。
#      去重：信息页 OCR 文本去全部空白为键（WinRT OCR 字符间插空格）；点头像未
#      切换时读到的是上一角色 → 命中已见键自动跳过；无效读（过渡帧/点击落空，
#      无数字）不记不输出，下轮整列重扫再试。自停：整列无新角色连续 2 轮 → 列表
#      到底（或滚动无效）。
#      2026-11-06 实机反馈：300ms 渲染等待截到过渡帧 → 三页全空且"双空判停"误
#      触发（只跑 2 个头像全未识别）→ 等待改可配置 $PageWait（默认 800ms），
#      并在点头像后显式点"信息"导航兜底（点头像不保证回到信息页签）。----
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
if ($meta.fallback) {
  Write-Output "__OCR_NOWIN__"
  exit 1
}
$Points = @()
try {
  $parsed = ConvertFrom-Json '__POINTS__'
  if ($parsed) { $Points = @($parsed) }
} catch {}
if (-not $Points) {
  Write-Output "__OCR_ERR__ 头像坐标为空"
  exit 1
}
$NavP = @()
try {
  $nv = ConvertFrom-Json '__NAV__'
  if ($nv) { $NavP = @($nv) }
} catch {}
$NavArc = @()
try {
  $na = ConvertFrom-Json '__NAV_ARC__'
  if ($na) { $NavArc = @($na) }
} catch {}
$NavInfo = @()
try {
  $ni = ConvertFrom-Json '__NAV_INFO__'
  if ($ni) { $NavInfo = @($ni) }
} catch {}
$ScrollCfg = $null
try {
  $sc = ConvertFrom-Json '__SCROLL__'
  if ($sc) { $ScrollCfg = $sc }
} catch {}
$PageWait = __PAGE_WAIT__
$engine = New-OcrEngine
$null = [Win32OCR]::SetForegroundWindow($Hwnd)
Start-Sleep -Milliseconds 200
$rpI = @(); foreach ($p in ('__ROI_INFO_PX__' -split ',')) { $rpI += [int]$p }
$rpC = @(); foreach ($p in ('__ROI_ARC_PX__' -split ',')) { $rpC += [int]$p }
$rpA = @(); foreach ($p in ('__ROI_ABIL_PX__' -split ',')) { $rpA += [int]$p }
$regionInfo = New-Object System.Drawing.Rectangle($win.x + $rpI[0], $win.y + $rpI[1], $rpI[2], $rpI[3])
$regionArc = New-Object System.Drawing.Rectangle($win.x + $rpC[0], $win.y + $rpC[1], $rpC[2], $rpC[3])
$regionAbil = New-Object System.Drawing.Rectangle($win.x + $rpA[0], $win.y + $rpA[1], $rpA[2], $rpA[3])
function Invoke-ClickAt([int]$cx, [int]$cy) {
  $null = [Win32OCR]::SetCursorPos($cx, $cy)
  Start-Sleep -Milliseconds 100
  [Win32OCR]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)   # LEFTDOWN
  Start-Sleep -Milliseconds 60
  [Win32OCR]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)   # LEFTUP
}
$idx = 0
$seen = @{}
$stagPasses = 0
$maxPasses = 8   # 安全上限：滚动持续有效时最多 8 轮整列（×头像数 ≈ 40 角色）
for ($pass = 1; $pass -le $maxPasses; $pass++) {
  if (Test-Path -LiteralPath '__CANCEL_FILE__') { break }   # 取消哨兵：每轮循环头探测
  $newThisPass = 0
  foreach ($pt in $Points) {
    if (Test-Path -LiteralPath '__CANCEL_FILE__') { break }   # 取消哨兵：每格循环头探测；
    # 用户请求取消即停止点击下一个头像，已识别头像的输出行保留
    Invoke-ClickAt ($win.x + [int]$pt[0]) ($win.y + [int]$pt[1])   # 点头像
    Start-Sleep -Milliseconds $PageWait                            # 角色切换渲染等待
    # 信息页兜底点击：点头像不保证回到"信息"页签，显式点一次（已在该页时幂等）
    Invoke-ClickAt ($win.x + [int]$NavInfo[0]) ($win.y + [int]$NavInfo[1])
    Start-Sleep -Milliseconds $PageWait                            # 信息页渲染等待
    $infoText = Invoke-Ocr $regionInfo $engine
    $key = ($infoText -replace '\s', '')       # 去重键：WinRT OCR 字符间插空格
    if ($key.Length -lt 4 -or -not ($key -match '\d')) { continue }   # 无效读（过渡帧/
    # 点击落空）→ 不记不输出，下轮整列重扫时再试
    if ($seen.ContainsKey($key)) { continue }  # 已处理（含点头像未切换=仍显示上一角色）
    $seen[$key] = 1
    $newThisPass++
    Invoke-ClickAt ($win.x + [int]$NavArc[0]) ($win.y + [int]$NavArc[1])   # 点"弧盘"
    Start-Sleep -Milliseconds $PageWait                            # 弧盘页渲染等待
    $arcText = Invoke-Ocr $regionArc $engine
    Invoke-ClickAt ($win.x + [int]$NavP[0]) ($win.y + [int]$NavP[1])       # 点"异能"
    Start-Sleep -Milliseconds $PageWait                            # 异能页渲染等待
    $abilText = Invoke-Ocr $regionAbil $engine
    Write-Output ("__OCR_CHAR__:" + $idx + ":" + (@{info = $infoText; ability = $abilText; arc = $arcText} | ConvertTo-Json -Compress))
    $idx++
  }
  if (Test-Path -LiteralPath '__CANCEL_FILE__') { break }   # 取消：不再滚动
  if ($newThisPass -eq 0) {
    $stagPasses++
    if ($stagPasses -ge 2) { break }   # 连续 2 整列无新角色：头像列表已到底（或滚动无效）→ 自停
  } else {
    $stagPasses = 0
  }
  # 头像列滚轮下滚：悬停列上滚 notches × 120（0x0800=MOUSEEVENTF_WHEEL，负值=向下）
  if ($ScrollCfg) {
    $null = [Win32OCR]::SetCursorPos($win.x + [int]$ScrollCfg.x, $win.y + [int]$ScrollCfg.y)
    Start-Sleep -Milliseconds 150
    for ($i = 0; $i -lt [int]$ScrollCfg.notches; $i++) {
      [Win32OCR]::mouse_event(0x0800, 0, 0, -120, [UIntPtr]::Zero)
      Start-Sleep -Milliseconds 80
    }
    Start-Sleep -Milliseconds 350      # 列表滚动动画
  }
}
Write-Output ("__OCR_META__" + "`t" + $meta.window + "`t" + "window")
'''

_OCR_PS_SCROLL = r'''
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32Scroll {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, int dwData, UIntPtr dwExtraInfo);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
}
"@
# 目标窗口：优先 Python 绑定注入的 hwnd（IsWindow 校验）；失效时回退标题关键词
# 查找（关键词占位符已修复：此前误写 {{KEYWORDS}} 与 Python 端替换不一致，
# 关键词永远为空，滚动目标永远找不到）
$target = [IntPtr]::Zero
$Hwnd = [IntPtr]__HWND__
if ($Hwnd -ne [IntPtr]::Zero -and [Win32Scroll]::IsWindow($Hwnd)) {
  $target = $Hwnd
} else {
  # 单引号字符串：内容不插值（$( )/反撇号/双引号均字面量）；Python 端已按
  # 单引号语义转义（' → ''）。此前误用双引号，窗口标题含 $(...) 即任意代码执行。
  $kw = '__KEYWORDS__' -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ }
  Get-Process | Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle } | ForEach-Object {
    if ($target -ne [IntPtr]::Zero) { return }
    foreach ($k in $kw) {
      if ($k -and $_.MainWindowTitle -like ("*" + $k + "*")) { $target = $_.MainWindowHandle; break }
    }
  }
}
if ($target -ne [IntPtr]::Zero) {
  $null = [Win32Scroll]::SetForegroundWindow($target)
  Start-Sleep -Milliseconds 250
  $rect = New-Object Win32Scroll+RECT
  $null = [Win32Scroll]::GetWindowRect($target, [ref]$rect)
  $cx = [int](($rect.Left + $rect.Right) / 2); $cy = [int](($rect.Top + $rect.Bottom) / 2)
  $null = [Win32Scroll]::SetCursorPos($cx, $cy)
  Start-Sleep -Milliseconds 120
  # 滚轮下滚 5 格（0x0800=MOUSEEVENTF_WHEEL，负值=向下）
  for ($i = 0; $i -lt 5; $i++) {
    [Win32Scroll]::mouse_event(0x0800, 0, 0, -480, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 60
  }
}
'''


class Handler(BaseHTTPRequestHandler):
    server_version = "OKntePlanner/1.0"

    def log_message(self, fmt, *args):
        pass

    # ---- 基础 ----
    def _send(self, code, payload, content_type="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(
            payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return {}  # 非数字 Content-Length：按无 body 处理，而不是让请求无响应挂死
        if length <= 0 or length > 2 * 1024 * 1024:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def _game_data_or_none(self):
        """读取游戏目录；文件缺失/损坏返回 None（调用方回 500，不让请求无响应挂死）。"""
        try:
            return game_data.load()
        except (OSError, ValueError, TypeError) as e:
            print(f"游戏目录读取失败: {type(e).__name__}: {e}")
            return None

    @staticmethod
    def _loopback_host(value):
        """Host/Origin 的主机部分是否为本机回环（面板只服务本机，阻断 DNS 重绑定）。"""
        if not value:
            return True  # HTTP/1.0 等不带 Host 的极端客户端不误伤
        value = value.strip().lower()
        if value.startswith("["):  # [::1]:8765
            end = value.find("]")
            value = value[1:end] if end > 0 else value.strip("[]")
        elif value.count(":") > 1:  # 裸 IPv6（无端口）
            pass
        else:
            value = value.rsplit(":", 1)[0]
        return value in ("127.0.0.1", "localhost", "::1")

    @classmethod
    def _origin_loopback(cls, origin):
        if not origin:
            return True  # curl/自测等非浏览器客户端通常不带 Origin
        match = re.match(r"^[a-z][a-z0-9+.-]*://([^/?#]+)", origin.strip().lower())
        return bool(match) and cls._loopback_host(match.group(1))

    def _reject_foreign(self):
        """本地面板防线：Host 与 Origin 必须回环。恶意网页对 127.0.0.1 面板的
        跨源写入（CSRF：改设置/写配置/恢复备份）与 DNS 重绑定读取在此被拒。"""
        if not self._loopback_host(self.headers.get("Host", "")) \
                or not self._origin_loopback(self.headers.get("Origin", "")):
            self._send(403, {"error": "拒绝非本机来源的请求"})
            return True
        return False

    def _oknte(self):
        return OkNte(STATE.settings["oknte_root"])

    # ---- GET ----
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/") and self._reject_foreign():
            return
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(404, {"error": "index.html 缺失"})
        elif path == "/api/bootstrap":
            oknte = self._oknte()
            STATE.ensure_today()
            try:  # 游戏目录角色名（供『按目录导入角色』下拉）；损坏不阻塞面板
                gd = game_data.load()
                gd_chars = [c["name"] + ("" if c.get("live") else "（未上线）")
                            for c in gd.get("characters", [])]
                gd_attr_map = {k: v for k, v in (gd.get("attribute_to_skill_series") or {}).items()
                               if k != "_note"}
            except (OSError, ValueError, KeyError, TypeError):
                gd_chars = []
                gd_attr_map = {}
            self._send(200, {
                "state": STATE.data,
                "tables": _tables(),
                "game_characters": gd_chars,
                "compat": oknte.compat_check(),
                "build": _build_info(),
                "attr_map": gd_attr_map,
                "skill_max": growth.skill_max(),
                "admin": is_admin(),
                "oknte_running": oknte.is_running_cached(),
            })
        elif path == "/api/status":
            snap = RUNNER.snapshot()
            snap["today"] = STATE.today
            snap["snapshot"] = planner.snapshot()
            self._send(200, snap)
        elif path == "/api/history":
            self._send(200, {"history": STATE.history[-200:], "today": STATE.today})
        elif path == "/api/yield-suggestions":
            self._send(200, planner.yield_suggestions())
        elif path == "/api/breakthrough-needs":
            # 突破门槛视图（纯读）：等级 cur→tgt 跨越的每道门槛（30→31…）材料 + 持有/缺少，
            # 数据全部来自 wiki charBreak 真实表与目录归属，无前端硬编码数字。
            rows = []
            for char in STATE.characters:
                entry = planner.catalog_entry(char)
                tracks = char.get("tracks") or {}
                levels = tracks.get("char_level") if isinstance(tracks.get("char_level"), dict) else {}
                rows.append({
                    "id": char.get("id"), "name": char.get("name", ""),
                    "level_cur": levels.get("cur", 0),
                    "level_tgt": levels.get("tgt", 0),
                    "awakening": char.get("awakening") or 0,
                    "in_catalog": entry is not None,
                    "gates": growth.character_breakthrough_rows(
                        char, entry, planner.equivalent_inventory(STATE.inventory)),
                })
            self._send(200, {"characters": rows,
                             "skill_max": growth.skill_max()})
        elif path == "/api/preview":
            days = 3
            if "?" in self.path:
                for part in self.path.split("?", 1)[1].split("&"):
                    if part.startswith("days="):
                        try:
                            days = int(part[5:])
                        except ValueError:
                            pass
            self._send(200, planner.preview_days(days))
        elif path == "/api/hunter/plan":
            boss = self.path.split("boss=", 1)[1].split("&")[0] if "boss=" in self.path else ""
            from urllib.parse import unquote
            boss = unquote(boss)
            fixed = 0
            if "fixed_runs=" in self.path:
                try:
                    fixed = int(self.path.split("fixed_runs=", 1)[1].split("&")[0])
                except ValueError:
                    pass
            self._send(200, planner.make_hunter_plan(oknte=None, boss=boss,
                                                     fixed_runs=fixed))
        elif path == "/api/plan":
            oknte = self._oknte()
            self._send(200, {
                "plan": planner.make_plan(oknte),
                "oknte_running": oknte.is_running_cached(),
            })
        elif path == "/api/logs":
            sources = _collect_log_sources(STATE.settings["oknte_root"])
            files = []
            for name, fpath in sources:
                try:
                    files.append({"name": name, "size": os.path.getsize(fpath),
                                  "mtime": datetime.fromtimestamp(os.path.getmtime(fpath))
                                           .strftime("%Y-%m-%d %H:%M:%S")})
                except OSError:
                    continue
            self._send(200, {"logs": files})
        elif path == "/api/logs/tail":
            sources = _collect_log_sources(STATE.settings["oknte_root"])
            try:
                idx = int(self.path.split("i=", 1)[1].split("&")[0]) if "i=" in self.path else -1
            except ValueError:
                idx = -1
            lines_n = _clamp_int(self.path.split("lines=", 1)[1].split("&")[0]
                                 if "lines=" in self.path else "", 50, 2000, 300)
            if not (0 <= idx < len(sources)):
                self._send(404, {"error": "日志不存在或已被清理"})
                return
            label, fpath = sources[idx]
            try:
                with open(fpath, encoding="utf-8", errors="replace") as f:
                    all_lines = f.read().splitlines()
            except OSError as e:
                self._send(500, {"error": f"日志读取失败：{e}"})
                return
            self._send(200, {"name": label, "total": len(all_lines),
                             "lines": all_lines[-lines_n:]})
        elif path == "/api/logs/download":
            sources = _collect_log_sources(STATE.settings["oknte_root"])
            try:
                idx = int(self.path.split("i=", 1)[1].split("&")[0]) if "i=" in self.path else -1
            except ValueError:
                idx = -1
            if not (0 <= idx < len(sources)):
                self._send(404, {"error": "日志不存在或已被清理"})
                return
            label, fpath = sources[idx]
            try:
                with open(fpath, "rb") as f:
                    self._send(200, f.read(), "text/plain; charset=utf-8")
            except OSError as e:
                self._send(500, {"error": f"日志读取失败：{e}"})
        elif path == "/api/wiki/upgrades":
            data = wiki_fetch.load_upgrades()
            if not data:
                self._send(404, {"error": "尚未抓取角色技能/突破数据，请先在图鉴页执行拉取"})
                return
            self._send(200, data)
        elif path == "/api/wiki/cassettes":
            data = wiki_fetch.load_cassettes()
            if not data:
                self._send(404, {"error": "尚未抓取空幕卡带数据，请先在图鉴页执行拉取"})
                return
            self._send(200, data)
        elif path == "/api/export":
            self._send(200, build_export())
        elif path == "/api/backups":
            self._send(200, {"backups": self._oknte().list_backups()})
        elif path == "/api/game-data":
            data = self._game_data_or_none()
            if data is None:
                self._send(500, {"error": "游戏目录 data/game_data.json 无法读取，请检查该文件"})
                return
            data["custom"] = game_data.custom_entries()
            self._send(200, data)
        elif path == "/api/game-data/wiki-fetch":
            name = ""
            if "name=" in self.path:
                from urllib.parse import unquote
                name = unquote(self.path.split("name=", 1)[1].split("&")[0]).strip()
            if not name:
                self._send(400, {"ok": False, "error": "缺少 name 参数"})
                return
            try:
                self._send(200, wiki_fetch.build_prefill_for(name))
            except (OSError, ValueError) as e:
                self._send(502, {"ok": False, "found": False,
                                 "error": f"wiki 拉取失败：{e}"})
        elif path == "/api/compat-report":
            self._send(200, build_compat_report(self._oknte()),
                       "text/markdown; charset=utf-8")
        elif path == "/api/teams":
            STATE.ensure_today()
            self._send(200, {"teams": _sanitize_teams(STATE.data)})
        elif path == "/api/common-costs":
            data = self._game_data_or_none()
            if data is None:
                self._send(500, {"error": "游戏目录 data/game_data.json 无法读取，请检查该文件"})
                return
            costs = _common_costs()
            self._send(200, {"defaults": game_data.COMMON_COST_DEFAULTS,
                             "costs": costs,
                             "mapping": _common_mapping_rows(data, costs)})
        elif path == "/api/ocr/windows":
            # OCR v3：枚举全部顶层可见窗口 + 当前绑定态（供窗口列表手选/诊断）
            windows = _ocr_windows()
            _hwnd, meta, _wins = _bind_game_window(windows)
            self._send(200, {
                "ok": True,
                "windows": [{"pid": w.get("pid"), "process": str(w.get("process") or ""),
                             "title": str(w.get("title") or ""), "hwnd": w.get("hwnd"),
                             "path": str(w.get("exe_path") or "")} for w in windows],
                "current": ({"process": str(meta.get("process") or ""),
                             "title": str(meta.get("title") or "")} if meta else None),
                "game_running": meta is not None,
            })
        elif path == "/api/ocr/layout":
            self._send(200, {"ok": True, "layout": _ocr_layout()})
        elif path == "/api/ocr/capture":
            # 当前绑定窗口整窗截图（DPI 感知），供前端网格标定
            hwnd, _meta, _wins = _bind_game_window()
            if not hwnd:
                self._send(502, {"ok": False, "error": "未找到游戏窗口（"
                                 + _default_game_process() + "）——请先启动游戏，"
                                 "或在设置/窗口列表中手动选择"})
                return
            try:
                shot = _ocr_capture_png(hwnd)
            except (OSError, subprocess.TimeoutExpired) as e:
                self._send(502, {"ok": False, "error": f"截图失败：{e}"})
                return
            # fallback 透传：截图瞬间窗口失效 → PS 回退整屏，按窗口比例解释会系统性错位
            cap_warnings = (["绑定窗口截图时已失效，本次为全屏截取——"
                             "该图不可用于网格标定，请重试截图"]
                            if shot.get("fallback") else [])
            self._send(200, {"ok": True, "image": shot["image"],
                             "width": shot["width"], "height": shot["height"],
                             "fallback": shot.get("fallback", False),
                             "warnings": cap_warnings})
        else:
            self._send(404, {"error": "not found"})

    # ---- POST ----
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if self._reject_foreign():
            return
        body = self._body()
        if body is None:
            self._send(400, {"error": "请求体不是合法 JSON"})
            return
        if path not in ("/api/stop", "/api/dry-run", "/api/today/clear-blacklist",
                        "/api/ocr/cancel") and \
                RUNNER.snapshot()["state"] in ("running", "waiting", "stopping"):
            self._send(409, {"error": "规划器运行中，请先停止再修改数据"})
            return

        if path == "/api/settings":
            with STATE.lock:  # 写 STATE.data 一律持锁：与执行线程/并发请求互斥
                clean = _sanitize_settings(body)
                STATE.data["settings"] = clean
                STATE.save()
            self._send(200, {"ok": True, "settings": STATE.settings})
        elif path == "/api/characters":
            if not isinstance(body.get("characters"), list):
                self._send(400, {"error": "characters 必须是数组"})
                return
            dupes = []
            with STATE.lock:
                chars = [_sanitize_character(c) for c in body["characters"][:50]]
                chars = _auto_fill_extra_needs(chars)
                # 后端重名互斥（此前只在前端拦截，绕过前端即可造出重名角色）
                dupes = duplicate_character_names(chars)
                if not dupes:
                    STATE.data["characters"] = chars
                    STATE.save()
            if dupes:
                self._send(409, {"error": "角色名重复（互斥录入）："
                                          + "、".join(dupes) + "；请修正后再保存"})
                return
            self._send(200, {"ok": True, "characters": STATE.characters})
        elif path == "/api/characters/reorder":
            ids = body.get("ids")
            if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
                self._send(400, {"error": "ids 必须是字符串数组"})
                return
            reorder_characters([str(i) for i in ids])
            self._send(200, {"ok": True})
        elif path == "/api/inventory":
            inv = body.get("inventory")
            if not isinstance(inv, dict):
                self._send(400, {"error": "inventory 必须是对象"})
                return
            with STATE.lock:
                STATE.data["inventory"] = {
                    str(k): _clamp_int(v, 0, 10**8, 0)
                    for k, v in inv.items() if str(k) in planner.ALL_ORDER  # 全量目录
                }
                STATE.save()
            self._send(200, {"ok": True, "inventory": STATE.inventory})
        elif path == "/api/game-data/import":
            action = str(body.get("action", ""))
            data = self._game_data_or_none()
            if data is None:
                self._send(500, {"error": "游戏目录 data/game_data.json 无法读取，请检查该文件"})
                return
            if action == "libraries":
                char_names, weapon_names = game_data.library_names(data)
                libs = _sanitize_libraries({"char_library": char_names,
                                            "weapon_library": weapon_names})

                def _merge(key, names):
                    # 合并而非覆盖：自定义名保留；顺手清洗旧导入残留的『（未上线）』后缀
                    existing = [str(n).replace("（未上线）", "").strip()
                                for n in (STATE.data.get(key) or [])]
                    merged = list(dict.fromkeys(existing + names))[:200]
                    STATE.data[key] = [n for n in merged if n]

                with STATE.lock:
                    _merge("char_library", libs["char_library"])
                    _merge("weapon_library", libs["weapon_library"])
                    STATE.save()
                self._send(200, {"ok": True,
                                 "char_library": STATE.data["char_library"],
                                 "weapon_library": STATE.data["weapon_library"]})
            elif action == "character":
                try:
                    raw = game_data.build_character_import(data, str(body.get("name", "")))
                except ValueError as e:
                    self._send(404, {"error": str(e)})
                    return
                with STATE.lock:  # 读-改-写：与并发请求互斥，防丢更新
                    STATE.data["characters"] = STATE.data["characters"][:50]
                    if any(c.get("name") == raw.get("name")
                           for c in STATE.data["characters"]):
                        payload = {"dup": raw.get("name")}
                    elif len(STATE.data["characters"]) < 50:
                        clean = _sanitize_character(raw)
                        STATE.data["characters"].append(clean)
                        STATE.save()
                        payload = {"ok": True, "character": clean}
                    else:
                        payload = None
                if payload is None:
                    self._send(409, {"error": "角色数量已达上限（50）"})
                elif "dup" in payload:
                    self._send(409, {"error": f"角色「{payload['dup']}」已在列表中（互斥录入）；"
                                              f"如需调整请在角色列表编辑"})
                else:
                    self._send(200, payload)
            else:
                self._send(400, {"error": "action 必须是 libraries 或 character"})
        elif path == "/api/wiki/fetch-upgrades":
            # 批量抓取 27+ 角色详情页：耗时较长（~60s），前端用长超时调用
            try:
                payload, failed = wiki_fetch.fetch_all_upgrades(timeout=15)
            except (OSError, ValueError) as e:
                self._send(502, {"ok": False, "error": f"wiki 拉取失败：{e}"})
                return
            self._send(200, {"ok": True, "count": len(payload.get("characters", {})),
                             "failed": failed, "fetched_at": payload.get("fetched_at")})
        elif path == "/api/wiki/fetch-cassettes":
            try:
                payload = wiki_fetch.fetch_cassettes(timeout=15)
            except (OSError, ValueError) as e:
                self._send(502, {"ok": False, "error": f"wiki 拉取失败：{e}"})
                return
            self._send(200, {"ok": True, "sets": len(payload.get("sets", [])),
                             "fetched_at": payload.get("fetched_at")})
        elif path == "/api/ocr/select":
            # 手动选择窗口：进程名写 game_process_name、标题写 game_window_keywords（标题兜底）
            try:
                pid = int(body.get("pid"))
            except (TypeError, ValueError):
                self._send(400, {"ok": False, "error": "pid 必须是整数"})
                return
            win = next((w for w in _ocr_windows() if str(w.get("pid")) == str(pid)), None)
            if win is None:
                self._send(404, {"ok": False, "error": "窗口不存在或已关闭，请刷新窗口列表"})
                return
            selected = {"process": str(win.get("process") or "").strip()[:64],
                        "title": str(win.get("title") or "").strip()[:64]}
            with STATE.lock:  # 写设置即落盘 state.json
                STATE.settings["game_process_name"] = selected["process"]
                STATE.settings["game_window_keywords"] = selected["title"]
                STATE.save()
            self._send(200, {"ok": True, "selected": selected})
        elif path == "/api/ocr/layout":
            # OCR 网格布局保存（校验后写 data/ocr_layout.json，读取即热生效）
            try:
                layout = _save_ocr_layout(body.get("layout"))
            except ValueError as e:
                self._send(400, {"ok": False, "error": str(e)})
                return
            except OSError as e:
                self._send(500, {"ok": False, "error": f"布局保存失败：{e}"})
                return
            self._send(200, {"ok": True, "layout": layout})
        elif path == "/api/inventory/ocr":
            # 实验性 v3：窗口绑定 + 网格逐格点击遍历识别（mode=page 只识别当前页）。
            # 绝不直接写库存：返回命中表由用户核对后预填保存。
            # OCR 单飞锁（评审 2026-11-05）：背包/角色遍历共用全局鼠标点击，绝不
            # 并发——上一轮还在 PS 页内时新识别请求立即 409，杜绝双遍历互相污染
            # （也堵死「新一轮入口清掉旧轮取消」的时序窗口）。
            if not _OCR_RUN_LOCK.acquire(blocking=False):
                self._send(409, {"ok": False,
                                 "error": "已有 OCR 遍历进行中，请等待完成或先点取消"})
                return
            try:
                try:
                    result = _run_backpack_ocr(mode=str(body.get("mode") or "traverse"))
                except ValueError as e:  # 语言包缺失（message 带可用语言提示）
                    self._send(502, {"ok": False, "error": str(e)})
                    return
                except _OcrWindowNotFound as e:  # 游戏未运行/窗口未绑定：明确报错，不再静默回退
                    self._send(502, {"ok": False, "error": str(e)})
                    return
                except (OSError, subprocess.TimeoutExpired) as e:
                    self._send(502, {"ok": False, "error": f"OCR 执行失败：{e}"})
                    return
                self._send(200, {"ok": True, **result})
            finally:
                _OCR_RUN_LOCK.release()
        elif path == "/api/ocr/cancel":
            # 取消正在进行的 OCR 遍历（背包 + 角色）：代数令牌定向——只对发起时
            # 登记的当前代数生效（旧轮的取消绝不泄漏进新一轮）；角色遍历的每头像
            # 循环在 PS 子进程内，Event 传不进去 → 同时落当前代数的取消哨兵文件，
            # PS 每头像循环头 Test-Path 探测后提前收尾（已识别角色保留）。
            # 运行中白名单放行——遍历挂起时规划器可能恰好启动，取消必须可用。
            _ocr_cancel_file_write(_ocr_cancel_active())
            self._send(200, {"ok": True, "cancelled": True})
        elif path == "/api/ocr/map-unknown":
            # 未收录道具实名映射：OCR unknown_items 的人工确认结果 → item_names 别名。
            # 校验失败 400（ValueError）/500（磁盘 OSError）；成功后同名格下次识别
            # 自动命中（item_names 热生效，无需重启）。
            try:
                item_names = _ocr_map_unknown(body.get("name_text"), body.get("mat_id"))
            except ValueError as e:
                self._send(400, {"ok": False, "error": str(e)})
                return
            except OSError as e:
                self._send(500, {"ok": False, "error": f"实名映射保存失败：{e}"})
                return
            self._send(200, {"ok": True, "item_names": item_names})
        elif path == "/api/characters/ocr":
            # 实验性 v2（2026-11-04 真实 UI 遍历；2026-11-06 增弧盘页）：逐个点头像 →
            # 信息页（名字+Lv）→ "弧盘"页（实名+等级）→ "异能"页（四技能+都市特技）；
            # 不直接写角色。
            # OCR 单飞锁：与背包遍历互斥（共用全局鼠标点击），抢不到即 409。
            if not _OCR_RUN_LOCK.acquire(blocking=False):
                self._send(409, {"ok": False,
                                 "error": "已有 OCR 遍历进行中，请等待完成或先点取消"})
                return
            try:
                try:
                    result = _run_chars_ocr()
                except _OcrWindowNotFound as e:
                    self._send(502, {"ok": False, "error": str(e)})
                    return
                except ValueError as e:
                    self._send(502, {"ok": False, "error": str(e)})
                    return
                except (OSError, subprocess.TimeoutExpired) as e:
                    self._send(502, {"ok": False, "error": f"OCR 执行失败：{e}"})
                    return
                except Exception as e:  # noqa: BLE001 —— 兜底：漏网异常也必须回 HTTP，
                    # 否则连接无响应 → 前端只会看到 Failed to fetch（真机 NameError 即此形态）
                    self._send(500, {"ok": False, "error": f"内部错误：{e}"})
                    return
                self._send(200, {"ok": True, **result})
            finally:
                _OCR_RUN_LOCK.release()
        elif path == "/api/game-data/custom":
            action = str(body.get("action", ""))
            try:
                if action == "upsert_character":
                    raw = body.get("entry") if isinstance(body.get("entry"), dict) else body
                    entry = _sanitize_custom_character(raw)
                    game_data.upsert_custom("characters", entry)
                elif action == "upsert_weapon":
                    raw = body.get("entry") if isinstance(body.get("entry"), dict) else body
                    entry = _sanitize_custom_weapon(raw)
                    game_data.upsert_custom("weapons", entry)
                elif action == "delete":
                    kind = body.get("kind")
                    name = str(body.get("name", "")).strip()
                    if kind not in game_data.CUSTOM_KINDS or not name:
                        self._send(400, {"error": "kind 必须是 characters/weapons，且 name 必填"})
                        return
                    game_data.remove_custom(kind, name)
                else:
                    self._send(400, {"error": "action 必须是 upsert_character/upsert_weapon/delete"})
                    return
            except ValueError as e:
                self._send(400, {"error": str(e)})
                return
            self._send(200, {"ok": True, "custom": game_data.custom_entries()})
        elif path == "/api/game-data/custom-material":
            try:
                entry = add_custom_material(body)
            except ValueError as e:
                self._send(400, {"error": str(e)})
                return
            self._send(200, {"ok": True, "added": entry, "materials": planner.MATERIALS})
        elif path == "/api/common-costs/save":
            clean = _sanitize_common_costs(body)
            with STATE.lock:
                STATE.data["common_costs"] = {**(STATE.data.get("common_costs") or {}),
                                              **clean}
                STATE.save()
            self._send(200, {"ok": True, "costs": _common_costs()})
        elif path == "/api/common-costs/apply":
            data = self._game_data_or_none()
            if data is None:
                self._send(500, {"error": "游戏目录 data/game_data.json 无法读取，请检查该文件"})
                return
            costs = _common_costs()
            rows = {r["name"]: r for r in _common_mapping_rows(data, costs)}
            name = str(body.get("name", "")).strip()
            if name != "全部" and name not in rows:
                self._send(404, {"error": f"游戏目录中没有已实装角色『{name}』"})
                return
            applied, skipped = [], []
            with STATE.lock:
                for char in STATE.data["characters"]:
                    row = rows.get(char.get("name"))
                    if row is None:
                        continue
                    if name != "全部" and char.get("name") != name:
                        continue
                    if not row["prefill"]:
                        skipped.append(f"{char.get('name')}：无已核对的通用映射")
                        continue
                    merged_extra = {**(char.get("extra_needs") or {}), **row["prefill"]}
                    char["extra_needs"] = {
                        str(k): _clamp_int(v, 0, 10**7, 0) for k, v in merged_extra.items()
                    }
                    applied.append(char.get("name"))
                STATE.save()
            notes = [f"{r['name']}：{'、'.join(r['skipped'])}" for r in rows.values()
                     if r["skipped"] and (name == "全部" or r["name"] == name)]
            self._send(200, {"ok": True, "applied": applied, "skipped": skipped,
                             "notes": notes})
        elif path == "/api/libraries":
            libs = _sanitize_libraries(body)
            with STATE.lock:
                # 部分更新：body 里没带的库保持原值（图鉴页只推武器库时不清角色库）
                if "char_library" in body:
                    STATE.data["char_library"] = libs["char_library"]
                if "weapon_library" in body:
                    STATE.data["weapon_library"] = libs["weapon_library"]
                STATE.save()
            self._send(200, {"ok": True,
                             "char_library": STATE.data["char_library"],
                             "weapon_library": STATE.data["weapon_library"]})
        elif path == "/api/tables/materials":
            display = body.get("display")
            if not isinstance(display, dict):
                self._send(400, {"error": "display 必须是 {id: {display_name, source}}"})
                return
            changed = 0
            with _MATERIALS_LOCK:  # 改+落盘+reload 持锁（与其它 materials 写者互斥）
                for section in ("targets", "extra_materials"):
                    for entry in planner.MATERIALS.get(section, []):
                        patch = display.get(entry["id"])
                        if not isinstance(patch, dict):
                            continue
                        if isinstance(patch.get("display_name"), str) and patch["display_name"].strip():
                            entry["display_name"] = patch["display_name"].strip()[:64]
                            changed += 1
                        if isinstance(patch.get("source"), str):
                            entry["source"] = patch["source"].strip()[:96]
                _save_json_atomic(os.path.join(DATA_DIR, "materials.json"), planner.MATERIALS)
                planner.reload_tables()
            self._send(200, {"ok": True, "changed": changed, "materials": planner.MATERIALS})
        elif path == "/api/tables/yields":
            per_run = body.get("per_run")
            hunter_yield = body.get("hunter_yield")
            if per_run is not None:
                if not isinstance(per_run, dict):
                    self._send(400, {"error": "per_run 必须是对象"})
                    return
                planner.YIELDS["per_run"] = {
                    str(k): _clamp_int(v, 0, 10**6, 1)
                    for k, v in per_run.items() if str(k) in planner.TARGETS
                }
            if hunter_yield is not None:
                if not isinstance(hunter_yield, dict):
                    self._send(400, {"error": "hunter_yield 必须是对象"})
                    return
                planner.YIELDS["hunter_yield"] = {
                    str(k): _clamp_int(v, 0, 10**6, 1)
                    for k, v in hunter_yield.items() if str(k) in planner.HUNTER_BOSSES
                }
            _save_json_atomic(os.path.join(DATA_DIR, "yields.json"), planner.YIELDS)
            planner.reload_tables()
            self._send(200, {"ok": True, "yields": planner.YIELDS})
        elif path == "/api/tables/requirements":
            try:
                json.dumps(body)  # 只要求可序列化，结构由用户自行维护
            except (TypeError, ValueError):
                self._send(400, {"error": "需求表必须是合法 JSON 对象"})
                return
            if not isinstance(body, dict) or "tracks" not in body:
                self._send(400, {"error": "需求表缺少 tracks 字段"})
                return
            _save_json_atomic(os.path.join(DATA_DIR, "requirements.json"), body)
            planner.reload_tables()
            self._send(200, {"ok": True, "requirements": planner.REQUIREMENTS})
        elif path == "/api/plan":
            oknte = self._oknte()
            self._send(200, {
                "plan": planner.make_plan(oknte),
                "oknte_running": oknte.is_running_cached(),
            })
        elif path == "/api/dry-run":
            # 纯读预演：运行中也不修改任何数据，故豁免"运行中禁改"拦截
            self._send(200, perform_dry_run(self._oknte()))
        elif path == "/api/start":
            oknte = self._oknte()
            mode = body.get("mode", "daily")
            mode = mode if mode in ("daily", "hunter") else "daily"
            compat = oknte.compat_check()
            if compat["status"] == "incompatible":
                self._send(409, {"error": "兼容性检查失败：" + "；".join(compat["problems"])})
                return
            # 持续模式下不因 ok-nte 运行中而拦截，由执行循环进入等待并自动重试
            if not STATE.settings.get("continuous_mode", True) and oknte.is_running():
                self._send(409, {"error": "检测到 ok-nte 正在运行，请先完全退出它（托盘也要退出）"})
                return
            fixed_runs = _clamp_int(body.get("fixed_runs"), 0, 10**6, 0)
            ok, msg = RUNNER.start(mode, fixed_runs)
            self._send(200 if ok else 409, {"ok": ok, "message": msg})
        elif path == "/api/stop":
            ok, msg = RUNNER.stop()
            self._send(200, {"ok": ok, "message": msg})
        elif path == "/api/today/clear-blacklist":
            cleared = clear_today_blacklist()
            self._send(200, {"ok": True, "cleared": cleared})
        elif path == "/api/teams/save":
            clean = _sanitize_teams(body)
            with STATE.lock:
                STATE.data["teams"] = clean
                STATE.save()
            self._send(200, {"ok": True, "teams": clean})
        elif path == "/api/history/confirm":
            day = str(body.get("day", ""))[:10]
            time_str = str(body.get("time", ""))[:8]
            mat_id = str(body.get("mat_id", ""))[:32]
            if mat_id not in planner.TARGETS:
                self._send(400, {"error": "未知材料"})
                return
            try:
                actual = int(body.get("actual_gain"))
            except (TypeError, ValueError):
                self._send(400, {"error": "actual_gain 必须是整数"})
                return
            if actual < 0:
                self._send(400, {"error": "actual_gain 不能为负"})
                return
            ok, msg = confirm_history_entry(day, time_str, mat_id, actual)
            self._send(200 if ok else 404, {"ok": ok, "message": msg})
        elif path == "/api/import":
            try:
                summary = apply_import(body)
            except ValueError as e:
                self._send(400, {"error": f"导入失败：{e}"})
                return
            self._send(200, {"ok": True, **summary})
        elif path == "/api/backups/restore":
            name = str(body.get("name", ""))
            try:
                restored = self._oknte().restore_backup(name)
            except OkNteError as e:
                self._send(409, {"error": str(e)})
                return
            self._send(200, {"ok": True, "restored": restored})
        elif path == "/api/reconcile":
            execution_id = str(body.get("execution_id", ""))[:64]
            resolution = str(body.get("resolution", ""))
            if resolution not in ("inventory_done", "discard"):
                self._send(400, {"error": "resolution 必须是 inventory_done 或 discard"})
                return
            ok, msg = RUNNER.resolve_pending(execution_id, resolution)
            self._send(200 if ok else 409, {
                "ok": ok, "message": msg, "pending": RUNNER.snapshot()["pending"]})
        else:
            self._send(404, {"error": "not found"})


def _port_candidates(port, auto_range):
    """端口避让的候选序列（纯函数，selftest 覆盖）。

    现实教训（2026-11-03 真机实证）：
    1. 输入法等常驻软件会随机抢占 8765 这类常见端口（百度输入法实测监听 8765）；
    2. Hyper-V/WSL/NAT 类组件重启后会把整段端口加入 Windows 动态排除区
       （netsh interface ipv4 show excludedportrange protocol=tcp 实测 8766~8865
       被排除，绑定报 WinError 10013）——旧实现只试 port+1..+20，整段恰好落在
       排除区内时全部 10013，面板"再也无法打开"。
    因此候选 = 相邻段 [port, port+auto_range] + 跳段候选
    [port+100, port+200] 各 auto_range+1 个（10013 时大步跳出排除区）。
    """
    offsets = list(range(0, auto_range + 1)) + list(range(100, 100 + auto_range + 1)) \
        + list(range(200, 200 + auto_range + 1))
    return [port + d for d in offsets]


def create_server(port, auto_range=20):
    """绑定端口；被占用/被系统排除时自动避让（候选序列见 _port_candidates）。

    返回 (server, actual_port)。全部候选不可用才抛带处理建议的中文错误——
    错误信息区分"被占用"与"WinError 10013（Windows 端口排除区，Hyper-V/WSL
    重启后常见）"两种形态，后者给出 netsh 排查与管理员 winnat 重启两个解法。
    """
    last_error = None
    saw_access_denied = False
    for candidate in _port_candidates(port, auto_range):
        try:
            return ThreadingHTTPServer(("127.0.0.1", candidate), Handler), candidate
        except OSError as e:
            last_error = e
            if getattr(e, "winerror", None) == 10013:
                saw_access_denied = True
    if saw_access_denied:
        raise RuntimeError(
            f"端口 {port} 起的候选端口均无法绑定（{last_error}）。"
            "其中部分端口处于 Windows 动态排除区（Hyper-V/WSL/NAT 重启后常见）。"
            "三种解法任选：① 管理员 PowerShell 执行 net stop winnat && net start winnat"
            "（清掉动态排除区，不影响 Hyper-V 功能）；② 修改 data/state.json 里的 port"
            "（避开排除区，可用 netsh interface ipv4 show excludedportrange protocol=tcp "
            "查看）；③ 重启电脑。") from None
    raise RuntimeError(
        f"端口 {port}~{port + 200 + auto_range} 均无法绑定（{last_error}）。"
        f"请关闭占用程序，或修改 data/state.json 里的 port 后重试。") from None


def _write_crash_log(error):
    """顶层异常落 crash.log（frozen 环境下自动落在 EXE 旁）。"""
    try:
        path = os.path.join(ROOT, "crash.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {type(error).__name__}: {error}\n")
        return path
    except OSError:
        return None


# ---- 桌面窗口壳（阶段12）：Edge App 模式，零第三方依赖 ----

class _Tee:
    """把 stdout/stderr 镜像到 panel.log（无控制台模式下 print 不再丢失）。"""

    def __init__(self, *streams):
        self._streams = [s for s in streams if s is not None]

    def write(self, data):
        for stream in self._streams:
            try:
                stream.write(data)
            except (OSError, ValueError):
                pass
        return len(data)

    def flush(self):
        for stream in self._streams:
            try:
                stream.flush()
            except (OSError, ValueError):
                pass


def _setup_logging_mirror():
    """源码+控制台运行时保持原样；frozen / pythonw（stdout=None）时镜像到 data/panel.log。"""
    try:
        if sys.stdout is not None and not getattr(sys, "frozen", False):
            return
        with STATE.lock:
            log_path = os.path.join(DATA_DIR, "panel.log")
        mirror = open(log_path, "a", encoding="utf-8", buffering=1)
        sys.stdout = _Tee(sys.stdout, mirror)
        sys.stderr = _Tee(sys.stderr, mirror)
    except OSError:
        pass


def _default_ui_mode():
    """默认 UI 策略：打包后的 EXE（frozen）→ 原生窗口进程；源码运行 → Edge 应用窗口。"""
    return "window" if getattr(sys, "frozen", False) else "auto"


def _resolve_ui_mode():
    """UI 策略：OKNTE_PLANNER_UI=window(原生窗口)/edge/browser/none/auto(跟随默认)；
    未设置时 frozen 默认 window、源码默认 auto；OKNTE_PLANNER_NO_BROWSER=1 兼容映射
    none（供自动化测试）。非法值回退默认。"""
    if os.environ.get("OKNTE_PLANNER_NO_BROWSER") == "1":
        return "none"
    default = _default_ui_mode()
    mode = os.environ.get("OKNTE_PLANNER_UI", default).strip().lower()
    if mode in ("", "auto"):
        return default
    return mode if mode in ("window", "edge", "browser", "none") else default


def _pick_edge(candidates):
    """按候选顺序返回第一个真实存在的 msedge.exe（纯函数，供测试）。"""
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _find_edge():
    paths = _pick_edge([
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ])
    if paths:
        return paths
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as key:
            value = winreg.QueryValueEx(key, "")[0]
        return _pick_edge([value])
    except OSError:
        return None


def _run_window_app(url):
    """pywebview 原生窗口：自己的 WinForms 窗口与进程（WebView2 渲染），阻塞至关窗。

    返回 True=窗口正常开合；False=pywebview 不可用/初始化失败（调用方降级 Edge）。
    """
    try:
        import webview
    except ImportError as e:
        print(f"原生窗口模块不可用（{e}），回退 Edge 应用窗口")
        return False
    print(f"正在打开桌面窗口: {url}")
    try:
        webview.create_window("异环养成规划器", url, width=1280, height=860)
        webview.start(gui="edgechromium")  # 阻塞至窗口关闭
    except Exception as e:  # noqa: BLE001 —— WebView2 运行时缺失等，降级而非崩溃
        print(f"原生窗口启动失败（{type(e).__name__}: {e}），回退 Edge 应用窗口")
        return False
    return True


def _run_edge_app_window(url):
    """以 Edge 应用模式打开独立窗口并阻塞到窗口关闭（降级档）。

    --no-proxy-server：面板是本机回环服务，强制直连，避免系统代理截获
    127.0.0.1 请求导致"拒绝连接"。独立 --user-data-dir 保证 Popen 的 PID
    就是该窗口的主进程：关窗口即进程退出。
    返回 True=窗口正常开合；False=Edge 不可用（调用方降级浏览器模式）。
    """
    edge = _find_edge()
    if not edge:
        return False
    profile = os.path.join(DATA_DIR, "edge_profile")
    try:
        os.makedirs(profile, exist_ok=True)
        print(f"正在打开桌面窗口（Edge 应用模式）: {url}")
        proc = subprocess.Popen(
            [edge, f"--app={url}", f"--user-data-dir={profile}",
             "--window-size=1280,860", "--no-first-run", "--no-default-browser-check",
             "--no-proxy-server"])
    except OSError as e:
        print(f"Edge 窗口启动失败（{e}），回退浏览器模式")
        return False
    proc.wait()
    return True


def _backfill_characters_from_catalog():
    """启动回填（schema v3 配套）：旧版 _sanitize_character 白名单缺陷曾把实机角色的
    attribute/matter_state 洗成 null，导致弧盘/空幕适配过滤失效。这里按角色名从
    游戏目录找回——只补空值，绝不覆盖已有数据；幂等（目录未命中则不动）。
    另：wiki 都市特技快照已抓取时，为 city_talents 为空的目录命中角色预填特技名
    与槽位数（1 或 2 位，按实机数据决定；等级留 0）。"""
    chars = STATE.characters
    if not chars:
        return
    try:
        data = game_data.load()
    except (OSError, ValueError):
        return
    try:
        from .wiki_fetch import city_talents_by_name
        wiki_talents = city_talents_by_name()
    except Exception:  # noqa: BLE001 —— 快照缺失不阻塞回填
        wiki_talents = {}
    by_name = {c.get("name"): c for c in data.get("characters", []) if isinstance(c, dict)}
    changed = False
    with STATE.lock:
        for char in chars:
            entry = by_name.get(char.get("name"))
            if not entry:
                continue
            for key in ("attribute", "matter_state"):
                if not char.get(key) and entry.get(key):
                    char[key] = entry[key]
                    changed = True
            if wiki_talents and not char.get("city_talents"):
                talents = wiki_talents.get(char.get("name")) or []
                prefill = [{"name": str(t.get("name", ""))[:32], "lv": 0}
                           for t in talents[:2] if t.get("name")]
                if prefill:
                    char["city_talents"] = prefill
                    char["city_talent_slots"] = 2 if len(prefill) > 1 else 1
                    changed = True
    if changed:
        STATE.save()
        print("启动回填：已按游戏目录/wiki 快照补回缺失的角色属性/物态/都市特技名"
              "（只补空值，不覆盖已有数据）")


def _graceful_shutdown(server):
    """窗口关闭后的优雅退出：先停执行器并给记账留时间，避免留下未决执行。"""
    print("窗口已关闭，正在退出…")
    try:
        RUNNER.stop()
    except Exception as e:  # noqa: BLE001 —— 退出路径尽力而为
        print(f"停止执行器时出现问题（不影响退出）: {e}")
    deadline = time.time() + 3
    while time.time() < deadline and \
            RUNNER.snapshot()["state"] in ("running", "waiting", "stopping"):
        time.sleep(0.1)
    STATE.save()
    server.shutdown()


def main():
    _setup_logging_mirror()
    try:
        _run_panel()
    except Exception as e:  # 顶层兜底：控制台 + crash.log 双通道可见
        path = _write_crash_log(e)
        print(f"面板启动失败：{type(e).__name__}: {e}")
        if path:
            print(f"详情已写入 {path}")
        raise SystemExit(1) from None


def _run_panel():
    # 启动对账：上次面板异常退出遗留的运行状态在此判定（只读检测，不拉起/杀进程）
    summary = RUNNER.recover_on_startup()
    # 启动回填：旧版缺陷洗掉的角色属性/物态按目录找回（只补空值，见函数说明）
    _backfill_characters_from_catalog()
    if summary.get("pending"):
        print("恢复提示：检测到未决执行（accounted 前崩溃遗留），处理前不会开始新的执行：")
        for p in summary["pending"]:
            print(f"  - [{p.get('execution_id')}] {(p.get('step') or {}).get('name', '?')}"
                  f"（PID {p.get('pid')}，{'仍在运行' if p.get('alive') else '已结束未记账'}）："
                  f"{p.get('reason', '')}")
    elif summary.get("prev_state") in ("running", "waiting", "stopping") or summary.get("corrupt"):
        print("恢复提示：" + RUNNER.snapshot()["message"])
    port = int(STATE.settings.get("port", 8765))
    env_port = os.environ.get("OKNTE_PLANNER_PORT", "").strip()
    if env_port:  # 环境变量优先（供自动化测试）
        try:
            port = int(env_port)
        except ValueError:
            print(f"警告：OKNTE_PLANNER_PORT={env_port} 不是数字，仍使用设置端口 {port}")
    server, actual_port = create_server(port)
    if actual_port != port:
        # 端口冲突自动避让：记住新端口并醒目提示（下次直接用新端口）
        STATE.settings["port"] = actual_port
        STATE.save()
        print(f"提示：端口 {port} 被其他程序占用，已自动改用 {actual_port} 并保存到设置。")
    url = f"http://127.0.0.1:{actual_port}"
    print(f"异环养成规划器面板: {url}")
    if not is_admin():
        print("警告：当前不是管理员权限，ok-nte 无界面任务可能无法操作游戏窗口，建议用 run_panel.bat 启动")

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    # 预热进程检测缓存：面板打开时 bootstrap/计划接口直接命中缓存，省 2~3s 首屏等待
    threading.Thread(
        target=lambda: OkNte(STATE.settings["oknte_root"]).is_running_cached(),
        daemon=True).start()
    ui = _resolve_ui_mode()
    try:
        if ui == "none":  # 纯服务模式（自动化测试）：不开任何 UI
            server_thread.join()
        elif ui == "window" and _run_window_app(url):
            _graceful_shutdown(server)  # 关窗口 = 停执行器 → 落账 → 退出
        elif ui in ("auto", "edge", "window") and _run_edge_app_window(url):
            _graceful_shutdown(server)  # window 降级 / auto / edge 共用 Edge 壳
        else:
            if ui == "edge":
                print("警告：未找到 Edge，回退为浏览器模式")
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
            server_thread.join()  # Ctrl+C 或进程被杀退出
    except KeyboardInterrupt:
        STATE.save()


if __name__ == "__main__":
    main()
