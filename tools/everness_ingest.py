"""everness.info 参考名一次性抓取 + 本地数据订正（脚本式，幂等可重跑）。

背景（AGENTS.md 数据源白名单 2026-11-11 用户指定新增）：everness.info 为官方数据站
（Next.js JS 壳，静态抓取仅 <title> 可用——items 页 <title>=官方中文名，已验证
WeaponBreakMaterial_04_lv1=无味冷点心）。本脚本：
1. 抓 /zh-Hans/items/<code> 系列（基础码 + 系列材料的 _lv1 变体，2026-11-12 实探：
   系列材料基础页 <title> 回显码名，中文名在 _lv1 页）与 /zh-Hans/arcs/<code> 三弧盘；
   全部走 app/sites_fetch 白名单通道（https + 主机白名单 + 拒私网/环回 + 禁重定向）。
2. 结果整理进 data/sites/everness_refs.json（{fetched_at, items, espers, arcs, notes}），
   抓取失败/无中文名的条目如实记录（zh="" + status），绝不编造。
3. data/materials.json 订正：huan_stone/niefu_dice/zhishi_dice 三条 manual_only 的
   source 备注与 item_names 补充（用户写法保留作别名，官方名经实抓确认才追加；
   官方名与用户写法相同则 display 不动，备注写明）。
4. data/sites/guide_sites.json arcs 增 fork_worldrain（source=everness；rarity/type
   站上未提供静态数据 → null 如实记录）。快照经 save_json_atomic 落盘（.bak 先行）。

运行（项目根目录）：python tools/everness_ingest.py [--skip-fetch]
--skip-fetch：跳过网络抓取，仅用已存在的 everness_refs.json 重放 materials/guide_sites 订正。
"""
import json
import re
import sys
import time
from datetime import datetime
from html import unescape
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.sites_fetch import _http_get  # noqa: E402  白名单通道（私有函数，仓库内复用）
from app.state import save_json_atomic  # noqa: E402

REFS_PATH = _ROOT / "data" / "sites" / "everness_refs.json"
MATERIALS_PATH = _ROOT / "data" / "materials.json"
GUIDE_SITES_PATH = _ROOT / "data" / "sites" / "guide_sites.json"

FETCH_TIMEOUT = 20
FETCH_RETRIES = 3
FETCH_DELAY = 0.4  # 请求间隔（对源站礼貌）

# 基础码（任务口径 22 项）：2026-11-12 实探，系列材料（WeaponBreak/SkillUp）基础页
# <title> 回显码名，中文名在 _lv1 变体页 → 两者都抓、分开记录
BASE_ITEM_CODES = [
    "OracleStone", "DiceNormal", "Dicelimite_01", "Dicelimite_02",
    "WeaponBreakMaterial_01", "WeaponBreakMaterial_02", "WeaponBreakMaterial_03",
    "WeaponBreakMaterial_04", "WeaponBreakMaterial_05",
    "SkillUpMaterial_01", "SkillUpMaterial_02", "SkillUpMaterial_03",
    "SkillUpMaterial_04", "SkillUpMaterial_05",
    "fons", "gold", "CityAbility_UpMaterial",
    "Worldboss_material_01", "Worldboss_material_02", "Worldboss_material_03",
    "Worldboss_material_04", "Worldboss_material_05", "Worldboss_material_06",
    "Worldboss_material_07",
]
LV1_ITEM_CODES = [f"WeaponBreakMaterial_0{i}_lv1" for i in range(1, 6)] + \
                 [f"SkillUpMaterial_0{i}_lv1" for i in range(1, 6)]
ARC_CODES = ["fork_GoldWool", "fork_Prokaryon", "fork_worldrain"]

# 27 项 esper id↔名：来自任务简报探查实锤（2026-11-11），本次未逐一重抓验证
ESPERS = {
    "1003": "早雾", "1004": "安魂曲", "1008": "翳", "1010": "娜娜莉",
    "1019": "薄荷", "1020": "哈尼娅", "1021": "埃德嘉", "1023": "白藏",
    "1025": "哈索尔", "1033": "阿德勒", "1036": "残虹", "1039": "法帝娅",
    "1042": "黑羽", "1046": "零", "1051": "Zero", "1052": "浔",
    "1054": "达芙蒂尔", "1055": "九原", "1057": "明音凛", "1070": "海月",
    "1071": "卡厄斯", "1072": "灵可", "1073": "小吱", "1074": "EXE",
    "1075": "伊洛伊", "1076": "真红", "1090": "曳尔沐",
}

_TITLE_RE = re.compile(r"<title>([^<]+)</title>")


def _fetch_title(url):
    """抓取页面 <title>，返回 (zh, page_len, error)。失败重试 FETCH_RETRIES 次，
    最终失败返回 error 原文（如实记录，绝不编造）。zh 取 | 前首段（站上标题格式
    「名称 | Everness Database」）。"""
    last_err = ""
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            text = _http_get(url, FETCH_TIMEOUT)
            m = _TITLE_RE.search(text)
            title = unescape(m.group(1)).strip() if m else ""
            zh = title.split("|")[0].strip()
            return zh, len(text), ""
        except Exception as e:  # noqa: BLE001  网络/白名单异常统一记录后重试
            last_err = f"{e.__class__.__name__}: {e}"
            if attempt < FETCH_RETRIES:
                time.sleep(0.6)
    return "", 0, last_err


def fetch_refs():
    """抓取全部条目 → everness_refs.json 结构（未落盘）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    items, failed, no_zh = {}, [], []
    for code in BASE_ITEM_CODES + LV1_ITEM_CODES:
        kind = "items"
        url = f"https://everness.info/zh-Hans/{kind}/{code}"
        zh, page_len, err = _fetch_title(url)
        entry = {"code": code, "zh": zh, "page_len": page_len, "url": url}
        if err:
            entry["status"] = f"failed: {err}"
            failed.append(code)
        elif not zh or zh == code:  # 页面存在但未提供中文名（title 回显码名/空）
            entry["status"] = "no_zh_title"
            no_zh.append(code)
        else:
            entry["status"] = "ok"
        items[code] = entry
        time.sleep(FETCH_DELAY)
    arcs = {}
    for code in ARC_CODES:
        url = f"https://everness.info/zh-Hans/arcs/{code}"
        zh, page_len, err = _fetch_title(url)
        entry = {"code": code, "zh": zh, "page_len": page_len, "url": url}
        if err:
            entry["status"] = f"failed: {err}"
            failed.append(code)
        else:
            entry["status"] = "ok"
        arcs[code] = entry
        time.sleep(FETCH_DELAY)
    refs = {
        "fetched_at": now,
        "source": "everness.info（Next.js JS 壳：官方中文名取 /zh-Hans/<页>/<码> 的 "
                  "<title> 首段；抓取经 app/sites_fetch 白名单通道：https+主机白名单"
                  "+拒私网/环回+禁重定向）",
        "items": items,
        "espers": dict(ESPERS),
        "arcs": arcs,
        "notes": {
            "espers_source": "27 项 id↔名来自任务简报探查实锤（2026-11-11），本次未逐一重抓验证",
            "1051": "espers 1051 实为 Zero，疑塔吉多误标（简报探查记录），待核对",
            "1090": "曳尔沐 = esper 1090（mysqil/ntestrategy 未收录）",
            "series_mapping": (
                "系列材料编号口径与本地不同，按名映射（_lv1 页实抓）："
                "WeaponBreakMaterial_01~05_lv1=铁制苹果核/螺旋乐碎屑/液态梦试用装/"
                "无味冷点心/入门级戏剧芯 → 本地 arc_3/arc_4/arc_2/arc_5/arc_1；"
                "SkillUpMaterial_01~05_lv1=雏鸟的希冀/新兵的怯懦/初次的期许/"
                "思维的同频/水波的迟疑 → 本地 ability_2/ability_1/ability_3/"
                "ability_4/ability_5"),
            "worldboss_official": (
                "Worldboss_material_01~07 实抓官方名（倾诉花种/上膛骑士火花塞/"
                "妄想彼端的一页/水月色拨片/护巢残片/斑斓的票根/海之泪）与 materials.json "
                "item_names 现值完全一致 → ntestrategy 侧异译名（忏悔花种/奔袭骑士火花塞/"
                "妄岸一页/水月拨片/巢卫碎片/彩色票根）确认不成立，item_names 无需增别名；"
                "族群小怪系列（低语/剪影/数字/幻想）不在本次码表内，未确认"),
            "huan_stone": (
                "简报疑 OracleStone=环石，实抓 <title>=谕石——与本表既有 oracle_stone"
                "（谕石，大世界收集→魔女之家兑换）同名而非环石，简报推测不成立；"
                "环石官方名本次未能确认，display 维持用户写法「环石」"),
            "zhishi_dice": (
                "Dicelimite_01/02 页面存在但未提供中文名（基础页与 _lv1 页 <title> 均"
                "回显码名）；质实骰子官方名待核，display 维持用户写法"),
            "fons_gold": "fons/gold 页面未提供中文名（<title> 回显码名）",
            "fork_Prokaryon": "实抓 <title>=「我们。」（简报记「我们」）——引号/句号为站上标题形态，如实记录",
            "fork_worldrain": (
                "实抓 <title>=「倾世之雨」；任务简报记「撼世之雨」——两者不同字，"
                "以实抓为准，简报所记待复核（nte.nanoka.cc 第二确认源未跑通，见下条）"),
            "nte_nanoka_cc": (
                "第二确认源探测（2026-11-12）：https://nte.nanoka.cc/ 可达（HTTP 200，"
                "title=nanoka.cc，约 1.9MB JS 壳）；/arcs/fork_worldrain 404——路径结构"
                "未探明（与 AGENTS.md「抓取策略待首台实机探明」一致），未能用作第二确认"),
            "fetch_failures": "，".join(failed) if failed else "无",
            "no_zh_titles": "，".join(no_zh) if no_zh else "无",
        },
    }
    return refs


def write_refs(refs):
    """everness_refs.json 落盘（save_json_atomic：被覆盖时自动 .bak 先行）。"""
    REFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    save_json_atomic(str(REFS_PATH), refs)
    return REFS_PATH


# ---------- materials.json 订正（幂等） ----------

MANUAL_SOURCE_NOTES = {
    "huan_stone": (
        "手动登记项（官方名待核：everness OracleStone 2026-11-12 实抓=谕石，与本表 "
        "oracle_stone 同名而非环石，简报推测不成立；mysqil/ntestrategy 2026-11-11 "
        "实抓均未收录）"),
    "niefu_dice": (
        "手动登记项（官方名经 everness /zh-Hans/items/DiceNormal 2026-11-12 实抓确认"
        "＝「捏造骰子」，与用户写法一致，display 不动）"),
    "zhishi_dice": (
        "手动登记项（官方名待核：everness Dicelimite_01/02 页面存在但未提供中文名，"
        "<title> 回显码名；mysqil/ntestrategy 2026-11-11 实抓均未收录）"),
}

# item_names 补充（用户写法保留作别名，OCR/手工输入仍命中；官方名经实抓确认且
# 与用户写法不同才追加第二别名——本次三项均无「确认的不同官方名」，故只补用户写法）
MANUAL_ITEM_NAMES = {
    "huan_stone": ["环石"],
    "niefu_dice": ["捏造骰子"],
    "zhishi_dice": ["质实骰子"],
}


def patch_materials():
    """materials.json 三条 manual_only 订正（幂等；save_json_atomic 自动 .bak）。"""
    with MATERIALS_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    changed = []
    for entry in data.get("extra_materials", []):
        mid = entry.get("id")
        if mid in MANUAL_SOURCE_NOTES:
            note = MANUAL_SOURCE_NOTES[mid]
            if entry.get("source") != note:
                entry["source"] = note
                changed.append(f"{mid}:source")
    item_names = data.setdefault("item_names", {})
    for mid, names in MANUAL_ITEM_NAMES.items():
        lst = item_names.setdefault(mid, [])
        for name in names:
            if name not in lst:
                lst.append(name)
                changed.append(f"item_names.{mid}+{name}")
    save_json_atomic(str(MATERIALS_PATH), data)
    return changed


def patch_guide_sites(refs):
    """guide_sites.json arcs 增 fork_worldrain（幂等；save_json_atomic 自动 .bak 先行）。

    rarity/type：站上为 JS 壳，<title> 之外无静态数据 → null 如实记录（绝不编造）。
    name 取实抓 <title>（「倾世之雨」——简报所记「撼世之雨」与之不符，以实抓为准）。"""
    with GUIDE_SITES_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    arcs = data.get("arcs")
    if not isinstance(arcs, list):
        raise ValueError(f"guide_sites.json arcs 段不是数组：{GUIDE_SITES_PATH}")
    entry = refs["arcs"].get("fork_worldrain") or {}
    zh = entry.get("zh") or ""
    if not zh or entry.get("status") != "ok":
        return ["skip: fork_worldrain 实抓无有效名称（" + str(entry.get("status")) + "）"]
    if any(a.get("code") == "fork_worldrain" or a.get("name") == zh for a in arcs):
        return ["skip: arcs 已含 fork_worldrain 条目"]
    arcs.append({
        "name": zh,
        "name_en": "",
        "rarity": None,
        "type": None,
        "source": "everness",
        "code": "fork_worldrain",
        "note": "2026-11-12 实抓 /zh-Hans/arcs/fork_worldrain <title>；站上 JS 壳未提供 "
                "rarity/type 静态数据（null=未取得，非站上确认值）",
    })
    save_json_atomic(str(GUIDE_SITES_PATH), data)
    return [f"arcs+{zh}(fork_worldrain, source=everness)"]


def main():
    skip_fetch = "--skip-fetch" in sys.argv
    if skip_fetch and REFS_PATH.is_file():
        with REFS_PATH.open(encoding="utf-8") as f:
            refs = json.load(f)
        print("[everness-ingest] --skip-fetch：沿用既有", REFS_PATH)
    else:
        refs = fetch_refs()
        write_refs(refs)
        ok_n = sum(1 for e in refs["items"].values() if e.get("status") == "ok")
        fail_n = sum(1 for e in refs["items"].values() if str(e.get("status", "")).startswith("failed"))
        nozh_n = sum(1 for e in refs["items"].values() if e.get("status") == "no_zh_title")
        arc_ok = sum(1 for e in refs["arcs"].values() if e.get("status") == "ok")
        print(f"[everness-ingest] items 抓取 成功={ok_n} 无中文名={nozh_n} 失败={fail_n}"
              f" / 共{len(refs['items'])}；arcs 成功={arc_ok}/共{len(refs['arcs'])}")
        print("[everness-ingest] refs →", REFS_PATH)
    changed_m = patch_materials()
    changed_g = patch_guide_sites(refs)
    print("[everness-ingest] materials.json 订正：", changed_m or "（无变化）")
    print("[everness-ingest] guide_sites.json 订正：", changed_g or "（无变化）")


if __name__ == "__main__":
    main()
