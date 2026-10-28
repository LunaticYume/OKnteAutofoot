"""需求 → 缺口 → 优先级顺序补齐 → 当日体力分配。"""
import json
import math
import os
from datetime import datetime, timedelta

from .state import DATA_DIR, STATE

GENERIC_TO_ID = {
    "exp_char": "exp_char",
    "exp_arc": "exp_arc",
    "coin": "coin",
}


def _load(name):
    with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
        return json.load(f)


MATERIALS = _load("materials.json")
YIELDS = _load("yields.json")
REQUIREMENTS = _load("requirements.json")

COST_PER_RUN = MATERIALS.get("cost_per_run", 40)
TARGETS = {t["id"]: t for t in MATERIALS["targets"]}
TARGET_ORDER = [t["id"] for t in MATERIALS["targets"]]
# 追猎 BOSS 名单（顺序即 ok-nte 界面顺序）与 BOSS→掉落材料映射
HUNTER_BOSSES = [b["name"] for b in MATERIALS.get("hunter_bosses", [])]
BOSS_MAT = {b["name"]: b["mat_id"] for b in MATERIALS.get("hunter_bosses", [])}
# 不可由体力本规划的材料（BOSS掉落/周本/大世界…），仅参与库存与缺口追踪
EXTRA = {m["id"]: m for m in MATERIALS.get("extra_materials", [])}
EXTRA_ORDER = [m["id"] for m in MATERIALS.get("extra_materials", [])]
# 全量材料展示顺序：体力本目标在前，extra 在后
ALL_ORDER = TARGET_ORDER + EXTRA_ORDER


def reload_tables():
    global MATERIALS, YIELDS, REQUIREMENTS, COST_PER_RUN, TARGETS, TARGET_ORDER
    global HUNTER_BOSSES, BOSS_MAT, EXTRA, EXTRA_ORDER, ALL_ORDER
    MATERIALS = _load("materials.json")
    YIELDS = _load("yields.json")
    REQUIREMENTS = _load("requirements.json")
    COST_PER_RUN = MATERIALS.get("cost_per_run", 40)
    TARGETS = {t["id"]: t for t in MATERIALS["targets"]}
    TARGET_ORDER = [t["id"] for t in MATERIALS["targets"]]
    HUNTER_BOSSES = [b["name"] for b in MATERIALS.get("hunter_bosses", [])]
    BOSS_MAT = {b["name"]: b["mat_id"] for b in MATERIALS.get("hunter_bosses", [])}
    EXTRA = {m["id"]: m for m in MATERIALS.get("extra_materials", [])}
    EXTRA_ORDER = [m["id"] for m in MATERIALS.get("extra_materials", [])]
    ALL_ORDER = TARGET_ORDER + EXTRA_ORDER


def display_name(mat_id):
    """材料的展示名：优先实名 display_name，回退旧 name，再回退 id。"""
    entry = TARGETS.get(mat_id) or EXTRA.get(mat_id) or {}
    return entry.get("display_name") or entry.get("name") or mat_id


def hunter_yield_of(boss):
    """追猎每局（60体力）BOSS 掉落目标材料的数量（可校准，默认8）。"""
    return float(YIELDS.get("hunter_yield", {}).get(boss, 8) or 0)


def yield_of(mat_id):
    return float(YIELDS["per_run"].get(mat_id, 0) or 0)


def _variant_map(character):
    mats = character.get("mats", {})
    return {
        "ability_mat": f"ability_{int(mats.get('ability', 1))}",
        "arc_mat": f"arc_{int(mats.get('arc', 1))}",
        "console_mat": f"console_{int(mats.get('console', 1))}",
        **GENERIC_TO_ID,
    }


def _ladder_cost(track_key, cur, tgt):
    track = REQUIREMENTS["tracks"].get(track_key, {})
    items = {}
    if tgt <= cur:
        return items
    for seg in track.get("ladder", []):
        lo, hi = int(seg.get("from", 0)), int(seg.get("to", 0))
        if hi <= cur or lo >= tgt:
            continue
        overlap = min(hi, tgt) - max(lo, cur)
        span = max(hi - lo, 1)
        for key, value in (seg.get("items") or {}).items():
            if value:
                items[key] = items.get(key, 0) + math.ceil(int(value) * overlap / span)
    return items


def character_needs(character):
    """单个角色的材料需求：练度轨道换算 + 直接填写的缺口。"""
    needs = {}
    variant = _variant_map(character)
    for track_key, levels in character.get("tracks", {}).items():
        try:
            cur, tgt = int(levels.get("cur", 0)), int(levels.get("tgt", 0))
        except (TypeError, ValueError):
            continue
        for generic, count in _ladder_cost(track_key, cur, tgt).items():
            mat_id = variant.get(generic, generic)
            needs[mat_id] = needs.get(mat_id, 0) + count
    for mat_id, count in (character.get("extra_needs") or {}).items():
        try:
            needs[mat_id] = needs.get(mat_id, 0) + max(0, int(count))
        except (TypeError, ValueError):
            continue
    return {k: v for k, v in needs.items() if v > 0}


def snapshot():
    """当前所有角色的需求/缺口快照，按角色优先级顺序。"""
    inventory = STATE.inventory
    characters = []
    total_needs = {}
    for character in STATE.characters:
        if not character.get("enabled"):
            continue
        needs = character_needs(character)
        for mat_id, count in needs.items():
            total_needs[mat_id] = total_needs.get(mat_id, 0) + count
        rows = []
        for mat_id in ALL_ORDER:
            if mat_id not in needs:
                continue
            have = int(inventory.get(mat_id, 0) or 0)
            gap = max(0, needs[mat_id] - have)
            farmable = mat_id in TARGETS and "config" in TARGETS[mat_id]
            rows.append({
                "mat_id": mat_id,
                "name": display_name(mat_id),
                "farmable": farmable,
                "broken": mat_id in TARGETS and "config" not in TARGETS[mat_id],
                "need": needs[mat_id],
                "have": have,
                "gap": gap,
                "yield": yield_of(mat_id) if farmable else 0,
                "runs": math.ceil(gap / yield_of(mat_id)) if farmable and gap and yield_of(mat_id) > 0 else 0,
            })
        characters.append({
            "id": character["id"],
            "name": character.get("name", ""),
            "rows": rows,
        })
    gaps = {}
    for mat_id, need in total_needs.items():
        gap = max(0, need - int(inventory.get(mat_id, 0) or 0))
        if gap:
            gaps[mat_id] = gap
    return {"characters": characters, "gaps": gaps}


# ---- 计划为空（no-op）原因枚举 ----
# UI 直接展示 reason 中文文案，no_op_reason 为机器可读枚举（详情见 detail）。
# 不含 upstream_incompatible：不兼容时 runner 在兼容性检查处已终止（runner._run_loop 开头），
# 不会进入规划；预览层由 /api/bootstrap 的 compat 三态标签单独呈现（分层见 Result 报告）。
NO_OP_REASONS = {
    "all_targets_completed": "所有目标已达标，今日无需补齐",
    "no_enabled_targets": "没有启用的角色（或启用角色没有材料需求）",
    "daily_limit_reached": "今日体力额度已用完",
    "insufficient_stamina": "剩余体力不足一轮（40）",
    "invalid_material_config": "有缺口的材料产量未校准（为0），无可执行目标，请在『数据表』填写每轮产出",
    "waiting_for_reset": "有缺口的材料今日均已拉黑（此前执行失败），等待次日重置",
}


def _today_view(day):
    """纯读的当日数据视图：跨天（today.date != day）时返回空的当日结构，不改 STATE。"""
    today = STATE.today
    if today.get("date") == day:
        return today
    return {"date": day, "used_stamina": 0, "planner_spent": 0, "hunter_spent": 0,
            "blacklist": [], "entries": []}


def _ordered_gaps(snap):
    """按角色优先级 → 材料规范顺序展开缺口（仅体力本可规划项；附 reason 所需数据）。"""
    ordered = []
    seen = set()
    for character in snap["characters"]:
        for row in character["rows"]:
            mat_id = row["mat_id"]
            if row["gap"] > 0 and row.get("farmable") and mat_id not in seen:
                seen.add(mat_id)
                ordered.append({
                    "mat_id": mat_id,
                    "name": row["name"],
                    "need": row["need"],
                    "have": row["have"],
                    "gap": row["gap"],
                    "yield": row["yield"],
                    "runs_needed": row["runs"],
                    "character": character["name"],
                })
    return ordered


def make_plan(oknte=None, now=None):
    """生成当日执行计划（预览与实跑共用）。

    纯读且确定性：同 inventory/target/体力额度/配置与同一注入 now → 完全相同的计划；
    不改 STATE、不写 state.json（预览路径无业务副作用）。oknte 日志扫描值只参与本次
    计算，其持久化显式归属执行器（runner._reconcile_used_stamina）。
    计划为空时 no_op_reason 为 NO_OP_REASONS 枚举，reason 为可直接展示的中文文案；
    每个步骤（steps[].reason）与计划整体（detail）都携带结构化解释，UI 不得自行重算。
    追猎预留开启时每次计划都扣留当日份额（不记当日标记），预览与等待重试都不会吃掉预留。
    """
    day = STATE.day_key(now)
    today = _today_view(day)
    snap = snapshot()
    ordered = _ordered_gaps(snap)
    warnings = []
    # 数据表损坏防线：体力本目标缺 config 的缺口必须可见（extra 材料属正常外部来源，不告警）
    broken = sorted({row["name"] for character in snap["characters"]
                     for row in character["rows"] if row["gap"] > 0 and row.get("broken")})
    for name in broken:
        warnings.append(f"{name}：材料表缺少副本配置（config），已跳过，请检查数据表")
    skipped_broken_zero = broken  # 仅损坏目标构成 invalid_material_config
    # 产量未校准防线：任何有缺口且产量为 0 的可规划材料都必须可见警告——
    # 即使预算在其他材料上耗尽、循环从未到达它（如大额甲硬币需求）
    for item in ordered:
        if yield_of(item["mat_id"]) <= 0:
            warnings.append(f"{item['name']}：产量未校准（为0），已跳过，请在『数据表』填写每轮产出")

    local_used = int(today.get("used_stamina", 0) or 0)
    used = local_used
    stamina_used_note = f"已耗体力取本地记录 {used}"
    if oknte is not None:
        scanned = oknte.scan_used_stamina(day)
        if scanned is not None and scanned > used:
            used = scanned
            stamina_used_note = f"已耗体力取 ok-nte 日志扫描值 {scanned}（本地记录 {local_used}）"

    settings = STATE.settings
    limit = max(0, int(settings.get("daily_stamina_limit", 180) or 0))
    # 追猎已改为独立计划（阶段13）：日常额度不再扣减追猎预留，
    # 追猎消耗按 today.hunter_spent 独立记账、独立额度
    budget = max(0, limit - used)
    budget_total = budget  # 可规划总额；planned_stamina + 剩余 与其守恒

    def _result(code, steps, budget_now, skipped_black, skipped_zero):
        text = NO_OP_REASONS[code] if code else ""
        detail = {
            "code": code or "ok",
            "text": text,
            "day": day,
            "stamina_limit": limit,
            "used_stamina": used,
            "stamina_used_note": stamina_used_note,
            "hunter_reserved": 0,  # 追猎已独立计划（阶段13），此键仅为前端兼容保留
            "budget_total": budget_total,
            "budget_remaining": budget_now,
            "targets_with_gap": len(snap["gaps"]),
            "steps_count": len(steps),
            "planned_runs": sum(s["runs"] for s in steps),
            "planned_stamina": sum(s["stamina"] for s in steps),
            "skipped_blacklisted": skipped_black,
            "skipped_zero_yield": skipped_zero,
        }
        return {
            "day": day, "used_stamina": used, "budget": budget_now,
            "steps": steps, "warnings": warnings, "reason": text,
            "no_op_reason": code, "detail": detail,
        }

    # no-op 判定顺序（确定性）：无目标 → 已达标 → 额度用尽 → 不够一轮 → 过滤后无可执行
    if not snap["characters"] or not any(c["rows"] for c in snap["characters"]):
        return _result("no_enabled_targets", [], budget, [], [])
    if not ordered:
        return _result("all_targets_completed", [], budget, [], [])
    if budget <= 0:
        return _result("daily_limit_reached", [], budget, [], [])
    if budget < COST_PER_RUN:
        return _result("insufficient_stamina", [], budget, [], [])
    if COST_PER_RUN <= 0:  # 数据表损坏防线：绝不除零，fail closed 为 invalid_material_config
        warnings.append(f"材料表 cost_per_run={COST_PER_RUN} 无效（须为正数），已停止规划，请检查数据表")
        return _result("invalid_material_config", [], budget, [], [])

    blacklist = set(today.get("blacklist", []))
    skipped_black, skipped_zero = [], []
    steps = []
    for item in ordered:
        mat_id = item["mat_id"]
        if mat_id in blacklist:
            skipped_black.append(item["name"])
            continue
        if "config" not in TARGETS.get(mat_id, {}):  # 数据表损坏防线：缺副本配置不进入执行
            warnings.append(f"{item['name']}：材料表缺少副本配置（config），已跳过，请检查数据表")
            skipped_zero.append(item["name"])
            continue
        if yield_of(mat_id) <= 0:
            # 警告已在预扫描统一生成（含预算耗尽未到达的项），此处仅跳过
            skipped_zero.append(item["name"])
            continue
        budget_before = budget
        runs = min(item["runs_needed"], budget // COST_PER_RUN)
        if runs <= 0:
            continue
        stamina = int(runs * COST_PER_RUN)
        gain = int(runs * yield_of(mat_id))
        steps.append({
            "mat_id": mat_id,
            "name": item["name"],
            "runs": int(runs),
            "stamina": stamina,
            "gain": gain,
            "gap": item["gap"],
            "character": item["character"],
            # 结构化解释：为什么刷这个、刷多少（planner 生成，UI 只展示不重算）
            "reason": {
                "material": mat_id,
                "display_name": item["name"],
                "target": item["need"],
                "inventory": item["have"],
                "deficit": item["gap"],
                "yield_per_run": item["yield"],
                "runs_needed": item["runs_needed"],
                "cost_per_run": COST_PER_RUN,
                "budget_before": budget_before,
                "runs_planned": int(runs),
                "text": (
                    f"{item['character']}需要{item['name']} {item['need']}"
                    f"（库存 {item['have']}，缺口 {item['gap']}）；"
                    f"每轮产出 {item['yield']}、消耗 {COST_PER_RUN} 体力，"
                    f"补齐需 {item['runs_needed']} 轮；安排前剩余额度 {budget_before}，"
                    f"本次执行 {runs} 轮（{stamina} 体力，预计 +{gain}）"),
            },
        })
        budget -= stamina
        if budget < COST_PER_RUN:
            break
    if not steps:
        # 有缺口但全部被过滤：损坏目标/产量未校准（配置问题）优先于全部拉黑（等次日重置）
        code = ("invalid_material_config"
                if (skipped_zero or skipped_broken_zero) else "waiting_for_reset")
        return _result(code, [], budget, skipped_black,
                       skipped_zero + sorted(skipped_broken_zero))
    return _result(None, steps, budget, skipped_black, skipped_zero)


def yield_suggestions():
    """产量校准建议（纯读，派生状态不落盘）。

    数据源：历史中状态为 done 且用户已确认 actual_gain 的条目。
    measured = Σ实际获得 / Σ轮数；suggested 为四舍五入的非负整数。
    只产出建议，绝不自动改表——应用走 /api/tables/yields（与手工编辑同一条通道）。
    """
    stats = {}  # mat_id -> [Σ轮数, Σ实际获得]
    confirmed = 0
    for entry in STATE.history:
        if entry.get("status") != "done" or "actual_gain" not in entry:
            continue
        try:
            runs = int(entry.get("runs") or 0)
            actual = int(entry.get("actual_gain") or 0)
        except (TypeError, ValueError):
            continue
        if runs <= 0:
            continue
        confirmed += 1
        agg = stats.setdefault(entry.get("mat_id", ""), [0, 0])
        agg[0] += runs
        agg[1] += actual
    suggestions = []
    for mat_id, (runs, actual) in sorted(stats.items()):
        target = TARGETS.get(mat_id)
        if target is None:
            continue
        measured = actual / runs
        suggestions.append({
            "mat_id": mat_id,
            "name": target["name"],
            "configured_yield": yield_of(mat_id),
            "confirmed_runs": runs,
            "confirmed_gain": actual,
            "measured_yield": round(measured, 2),
            "suggested_yield": max(0, round(measured)),
        })
    return {"confirmed_entries": confirmed, "suggestions": suggestions}


# ---- 体力恢复模型与多日预演（阶段9，纯读，注入时钟保证确定性） ----

def projected_stamina(now=None):
    """按用户填写的当前体力 + 恢复速率推算体力估计；未填/数据无效返回 None。纯读。

    估计 = min(上限, 填写值 + 每小时恢复 × 距填写时刻的小时数)；只作展示与预演参考，
    不改变执行逻辑（真实体力不足由 ok-nte 检测后进入等待重试）。
    """
    settings = STATE.settings
    try:
        base = float(settings.get("stamina_now"))
        at = datetime.strptime(str(settings.get("stamina_now_at") or ""),
                               "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    if base < 0:
        return None
    rate = max(0.0, float(settings.get("stamina_regen_per_hour") or 0))
    cap = max(1.0, float(settings.get("stamina_cap") or 240))
    now = now or datetime.now()
    hours = max(0.0, (now - at).total_seconds() / 3600)
    return round(min(cap, base + rate * hours), 1)


def preview_days(days=3, now=None):
    """未来 N 天计划推演（纯读、确定性、注入时钟；不改 STATE）。

    模型（诚实边界）：第 0 天额度 = 限额 − 今日已耗（与 make_plan 同口径；
    追猎自阶段13 起独立记账独立额度，日常预演不再扣减预留）；第 1..N-1 天额度 = 限额
    （假设体力自然恢复足以支撑每日限额；若 恢复速率×24 < 限额会给出警告）。
    库存按预计产出虚拟累加，逐日按优先级顺序补齐，与 make_plan 同一贪心。
    返回 {days:[{date,budget,steps,remaining}], projected_stamina_now,
          complete_on(首个全部达标日或 None), warnings}。
    """
    days = max(1, min(int(days or 3), 7))
    now = now or datetime.now()
    warnings = []
    settings = STATE.settings
    limit = max(0, int(settings.get("daily_stamina_limit", 180) or 0))
    rate = max(0.0, float(settings.get("stamina_regen_per_hour") or 0))
    cap = max(1.0, float(settings.get("stamina_cap") or 240))
    if rate > 0 and rate * 24 < limit:
        warnings.append(
            f"体力恢复速率({rate}/小时)×24={rate * 24:g} 低于每日限额 {limit}，"
            "预演按每日限额推算可能偏乐观（实际需攒体力或调低限额）")
    if rate <= 0:
        warnings.append("未设置体力恢复速率（0/小时）：无法评估每日限额是否可支撑")

    day0 = STATE.day_key(now)
    today = _today_view(day0)
    used = int(today.get("used_stamina", 0) or 0)
    # 与 make_plan 同口径：ok-nte 日志扫描值只在执行器落账，预演用本地记录（确定性）
    virtual = {k: int(v or 0) for k, v in STATE.inventory.items()}
    snap = snapshot()
    per_day = []
    complete_on = None
    for offset in range(days):
        date = (datetime.strptime(day0, "%Y-%m-%d") + timedelta(days=offset)).strftime("%Y-%m-%d")
        budget = max(0, limit - used if offset == 0 else limit)
        steps = []
        for item in _ordered_gaps(snapshot_from_inventory(virtual, snap)):
            mat_id = item["mat_id"]
            if yield_of(mat_id) <= 0:
                continue
            gap = max(0, item["need"] - virtual.get(mat_id, 0))
            if gap <= 0:
                continue
            runs_needed = math.ceil(gap / yield_of(mat_id))
            runs = min(runs_needed, budget // COST_PER_RUN)
            if runs <= 0:
                continue
            stamina = int(runs * COST_PER_RUN)
            steps.append({"mat_id": mat_id, "name": item["name"], "runs": int(runs),
                          "stamina": stamina, "gain": int(runs * yield_of(mat_id)),
                          "character": item["character"]})
            virtual[mat_id] = virtual.get(mat_id, 0) + int(runs * yield_of(mat_id))
            budget -= stamina
            if budget < COST_PER_RUN:
                break
        remaining_gaps = [it["mat_id"] for it in snap_ordered_needs(snap)
                          if max(0, it["need"] - virtual.get(it["mat_id"], 0)) > 0
                          and yield_of(it["mat_id"]) > 0]
        if remaining_gaps == [] and complete_on is None:
            complete_on = date
        per_day.append({"date": date,
                        "budget": max(0, limit - used if offset == 0 else limit),
                        "steps": steps, "remaining": budget,
                        "open_gaps": len(remaining_gaps)})
    return {"days": per_day,
            "projected_stamina_now": projected_stamina(now),
            "complete_on": complete_on, "warnings": warnings}


def snap_ordered_needs(snap):
    """快照中全部未达标项（含已达标者不返回），与 _ordered_gaps 同序；预演用。"""
    return [dict(item) for item in _ordered_gaps(snap)]


def snapshot_from_inventory(virtual, snap):
    """把虚拟库存套进既有快照结构（不改 snap 原对象），复用 _ordered_gaps 的排序。"""
    patched = {"characters": [], "gaps": {}}
    for character in snap["characters"]:
        rows = []
        for row in character["rows"]:
            have = int(virtual.get(row["mat_id"], 0) or 0)
            gap = max(0, row["need"] - have)
            rows.append({**row, "have": have, "gap": gap,
                         "runs": math.ceil(gap / row["yield"]) if gap and row["yield"] > 0 else 0})
        patched["characters"].append({**character, "rows": rows})
    return patched


# ---- 追猎独立计划（阶段13） ----

HUNTER_COST_PER_RUN = 60  # 异象追猎每次 60 体力（ok-nte AnomalyHunter.TASK_COST）

HUNTER_NO_OP = {
    "all_targets_completed": "追猎材料已达标，无需追猎",
    "no_enabled_targets": "未设置追猎目标，请在追猎计划卡选择 BOSS",
    "daily_limit_reached": "今日追猎额度已用完",
    "insufficient_stamina": "追猎剩余额度不足一局（60 体力）",
    "invalid_material_config": "追猎目标或产量配置无效，请在『数据表』核对",
    "waiting_for_reset": "该 BOSS 今日已失败拉黑，等待次日重置",
}


def make_hunter_plan(oknte=None, now=None, boss=None, fixed_runs=0):
    """追猎独立计划（纯读、确定性、注入时钟；与日常计划完全分开）。

    额度 = 追猎每日额度(anomaly_hunter_stamina) − 今日追猎已耗(hunter_spent)；
    次数 = fixed_runs>0 时取覆盖值，否则 ceil(所选BOSS掉落材料缺口 / 每局产量)，
    封顶为 额度//60。BOSS 默认取设置里的 anomaly_hunter_target。
    与日常共用游戏内体力池，但记账口径独立（today.hunter_spent）。
    """
    day = STATE.day_key(now)
    today = _today_view(day)
    settings = STATE.settings
    boss = (boss or settings.get("anomaly_hunter_target") or "").strip()
    warnings = []
    hunter_limit = max(0, int(settings.get("anomaly_hunter_stamina", 180) or 0))
    used_h = int(today.get("hunter_spent", 0) or 0)
    budget = max(0, hunter_limit - used_h)

    def _result(code, steps, remaining, note=""):
        text = HUNTER_NO_OP.get(code, "") if code else ""
        return {
            "mode": "hunter", "day": day, "boss": boss,
            "budget": remaining, "steps": steps,
            "warnings": warnings, "reason": text or note,
            "no_op_reason": code,
            "detail": {
                "code": code or "ok", "text": text, "day": day,
                "hunter_limit": hunter_limit, "hunter_spent": used_h,
                "budget_total": max(0, hunter_limit - used_h),
                "cost_per_run": HUNTER_COST_PER_RUN,
                "runs": sum(s["runs"] for s in steps),
                "planned_stamina": sum(s["stamina"] for s in steps),
            },
        }

    if boss not in HUNTER_BOSSES:
        return _result("no_enabled_targets", [], budget,
                       "追猎目标无效，请在追猎计划卡选择 BOSS")
    if fixed_runs < 0:
        fixed_runs = 0
    if budget < HUNTER_COST_PER_RUN:
        return _result("insufficient_stamina", [], budget)
    blacklist = set(today.get("blacklist", []))
    if f"hunt:{boss}" in blacklist:
        return _result("waiting_for_reset", [], budget)
    mat_id = BOSS_MAT.get(boss)
    if not mat_id:
        return _result("invalid_material_config", [], budget)
    yield_per = hunter_yield_of(boss)
    if yield_per <= 0:
        warnings.append(f"{boss}：每局掉落产量未校准（为0），请在『数据表→BOSS掉落产量』填写")
        return _result("invalid_material_config", [], budget)

    have = int(STATE.inventory.get(mat_id, 0) or 0)
    need = 0
    for character in STATE.characters:
        if not character.get("enabled"):
            continue
        need += character_needs(character).get(mat_id, 0)
    gap = max(0, need - have)

    runs = fixed_runs if fixed_runs > 0 else math.ceil(gap / yield_per)
    runs = min(runs, budget // HUNTER_COST_PER_RUN)
    if runs <= 0:
        if gap <= 0 and fixed_runs <= 0:
            return _result("all_targets_completed", [], budget)
        return _result("insufficient_stamina", [], budget)
    stamina = int(runs * HUNTER_COST_PER_RUN)
    steps = [{
        "boss": boss,
        "mat_id": mat_id,
        "name": display_name(mat_id),
        "runs": int(runs),
        "stamina": stamina,
        "gain": int(runs * yield_per),
        "gap": gap,
        "reason": {
            "material": mat_id,
            "display_name": display_name(mat_id),
            "target": need,
            "inventory": have,
            "deficit": gap,
            "yield_per_run": yield_per,
            "runs_planned": int(runs),
            "text": (
                f"{boss}掉落 {display_name(mat_id)}（库存 {have}，缺口 {gap}）；"
                f"每局掉落 {yield_per:g}、消耗 {HUNTER_COST_PER_RUN} 体力，"
                f"本次执行 {runs} 局（{stamina} 体力，预计 +{int(runs * yield_per)}）"),
        },
    }]
    return _result(None, steps, budget - stamina)
