"""规划器本地 Web 面板（纯标准库）：API + 静态页 + 桌面窗口壳。"""
import base64
import copy
import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app import planner
from app import game_data
from app import wiki_fetch
from app.oknte_adapter import OkNte, OkNteError
from app.runner import RUNNER
from app.state import (DATA_DIR, ROOT, STATE, STATE_SCHEMA, new_character,
                       save_json_atomic as _save_json_atomic)

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
    # 觉醒档位 0-6
    char["awakening"] = _clamp_int(raw.get("awakening"), 0, 6, 0)
    # 异能技能拆分：普攻/变轨/极轨/援护 cur/tgt（体力计划仍用 ability_level 总轨）
    skills = raw.get("skills") if isinstance(raw.get("skills"), dict) else {}
    entry_skills = {}
    for k in ("normal", "variable", "orbit", "assist"):
        block = skills.get(k) if isinstance(skills.get(k), dict) else {}
        entry_skills[k] = {"cur": _clamp_int(block.get("cur"), 0, 10, 0),
                           "tgt": _clamp_int(block.get("tgt"), 0, 10, 0)}
    char["skills"] = entry_skills
    # 都市特技（1-2 个，名字+等级）
    talents = raw.get("city_talents") if isinstance(raw.get("city_talents"), list) else []
    char["city_talents"] = []
    for t in talents[:2]:
        if isinstance(t, dict) and str(t.get("name", "")).strip():
            char["city_talents"].append({"name": str(t["name"]).strip()[:32],
                                         "lv": _clamp_int(t.get("lv"), 0, 5, 0)})
    # 空幕卡带（1 张/角色：套装+主属性）
    cassette = raw.get("cassette") if isinstance(raw.get("cassette"), dict) else {}
    char["cassette"] = {"set": str(cassette.get("set", "")).strip()[:48] or None,
                        "main": str(cassette.get("main", "")).strip()[:24] or None}
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
    if mat_id in planner.TARGETS or mat_id in planner.EXTRA:
        raise ValueError(f"材料 id『{mat_id}』已存在")
    entry = {"id": mat_id, "display_name": display,
             "category": str(body.get("category", "")).strip()[:32] or "自定义",
             "obtain": body.get("obtain") if body.get("obtain") in ("hunter", "external") else "external",
             "source": str(body.get("source", "")).strip()[:96]}
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


def _run_backpack_ocr():
    """实验性背包 OCR：PowerShell 截屏 + Windows.Media.Ocr（系统组件，零第三方依赖）。

    返回识别文本或 None（无中文语言包/识别失败）。游戏窗口须保持可见；
    识别结果只作差异预填参考，由用户核对后保存。
    """
    ps = base64.b64encode(_OCR_PS.encode("utf-16-le")).decode("ascii")
    r = subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", ps],
                       capture_output=True, timeout=90,
                       creationflags=0x08000000)
    text = (r.stdout or b"").decode("utf-8", errors="replace").strip()
    if not text or "__OCR_NOLANG__" in text:
        return None
    return text


_OCR_PS = r'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.DataWriterStoreOperation, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType=WindowsRuntime]
Function Await($WinRtTask, $ResultType) {
  $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
  $netTask = $asTask.MakeGenericMethod($ResultType).Invoke($null, @($WinRtTask))
  $netTask.Wait(-1) | Out-Null
  $netTask.Result
}
$bmp = New-Object System.Drawing.Bitmap([System.Windows.Forms.SystemInformation]::VirtualScreen.Width, [System.Windows.Forms.SystemInformation]::VirtualScreen.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen(0, 0, 0, 0, $bmp.Size)
$g.Dispose()
$ms = New-Object System.IO.MemoryStream
$bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
$stream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
$writer = New-Object Windows.Storage.Streams.DataWriter($stream.GetOutputStreamAt(0))
$writer.WriteBytes($ms.ToArray())
$ms.Dispose()
Await ($writer.StoreAsync()) ([Windows.Storage.Streams.DataWriterStoreOperation]) | Out-Null
$writer.Dispose()
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$soft = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if (-not $engine) { Write-Output "__OCR_NOLANG__"; exit 1 }
$result = Await ($engine.RecognizeAsync($soft)) ([Windows.Media.Ocr.OcrResult])
Write-Output $result.Text
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
        if path not in ("/api/stop", "/api/dry-run", "/api/today/clear-blacklist") and \
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
            with STATE.lock:
                chars = [_sanitize_character(c) for c in body["characters"][:50]]
                chars = _auto_fill_extra_needs(chars)
                STATE.data["characters"] = chars
                STATE.save()
            self._send(200, {"ok": True, "characters": STATE.characters})
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
        elif path == "/api/inventory/ocr":
            # 实验性：截屏 OCR 原文返回，匹配与预填都在前端完成（绝不直接写库存）
            try:
                text = _run_backpack_ocr()
            except (OSError, subprocess.TimeoutExpired) as e:
                self._send(502, {"ok": False, "error": f"OCR 执行失败：{e}"})
                return
            if text is None:
                self._send(502, {"ok": False,
                                 "error": "本机没有可用的 Windows OCR 中文语言包，实验功能不可用"})
                return
            self._send(200, {"ok": True, "text": text})
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
        elif path == "/api/teams":
            STATE.ensure_today()
            self._send(200, {"teams": _sanitize_teams(STATE.data)})
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


def create_server(port, auto_range=20):
    """绑定端口；被占用时自动在 [port+1, port+auto_range] 内找可用端口（端口冲突避让）。

    返回 (server, actual_port)。整段不可用才抛带处理建议的中文错误。
    ——现实教训：输入法等常驻软件会随机抢占 8765 这类常见端口，闪退不可接受。
    """
    last_error = None
    for candidate in [port] + list(range(port + 1, port + 1 + auto_range)):
        try:
            return ThreadingHTTPServer(("127.0.0.1", candidate), Handler), candidate
        except OSError as e:
            last_error = e
    raise RuntimeError(
        f"端口 {port}~{port + auto_range} 均无法绑定（{last_error}）。"
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
