"""攻略站公共数据源接入（ntestrategy.com + wiki.mysqil.com，手动触发 only）。

范围（用户确认）：材料 + 弧盘 + 角色目录 + 角色头像，两站均无登录、无个人数据。
- ntestrategy.com：页面是静态 HTML 空壳，数据内嵌于单个 /js/shared.js（实测 239KB）——
  CHARS 角色库 / ARCS 弧盘库（弧盘=本应用武器口径）/ CHAR_MATS 逐角色养成材料；
  另有 BIS 建议三块 TRAINS/CONSOLE_BUILDS/CHAR_ARCS（2026-11-07 增，仅建议展示：
  按 slug 归并进快照独立 builds 段，不进 char/weapon_library，绝不写 stigma_groups）。
- wiki.mysqil.com（2026-11-10 接入）：Astro 静态站，角色图鉴 /characters/ 页卡片
  （<a href="/characters/<拼音slug>">…<h3>中文名</h3>）提供 25 角色的中文实名→
  拼音 slug 权威对照；页面结构改版时回落 sitemap-0.xml 的 slug 清单 +
  MYSQIL_SLUG_NAMES 已核对照表。角色头像 = /images/characters/<slug>.webp
  （2026-11-10 实测 2160x2560 竖版立绘）与 <slug>-icon.webp（256x256 方图）。
- nteguide.com：自 2026-11-10 起全面禁用（站方头像资产存在错误图片，如
  lacrimosa.webp 为带对话框过场截图）——数据/图片/链接一律不再采用，快照里的
  既往 nteguide 条目在同步时一次性剔除。仓库根 AGENTS.md 有禁用记录。

合并策略（用户确认）：不覆盖已有实名，缺失补录，来源标注，失败回退（单站挂不影响
另一站）。落点：
- 角色目录 → state.json char_library；弧盘目录 → state.json weapon_library（合并由
  调用方 main.py 持 STATE.lock 执行，本模块只算不改 STATE）；
- 材料目录 → 不自动生成 mat_id：对不上现有别名的原文名只能存入快照的
  guide_unmapped_materials 供人工核对后再录入（无证据的 id 映射绝不编造）。
- 跨源同名角色/弧盘按英文名（或 slug）归并：先到先得进目录，另一站的译名记入快照
  alt_names，避免同一实体在目录里出现两条译名。

快照：data/sites/guide_sites.json（消毒后的两站条目 + 抓取时间 + 来源标注）。

安全边界（全仓库第二个服务端外联点；2026-11-14 B2.5/XMOD-01 起与 wiki_fetch/
tajiduo_client 共用 app/url_guard.py 唯一实现，本模块只保留常量白名单与薄包装）：
- 仅允许 https；URL 全部来自本模块常量（不接受前端传 URL）；
- 主机白名单四域（ALLOWED_HOSTS：ntestrategy.com / wiki.mysqil.com /
  everness.info / nte.nanoka.cc，2026-11-11 增后两域；子域归一化校验，含前缀伪装拒绝）；
- 解析出的全部 IP 不得为私网/环回/链路本地/保留/组播/未指定段；空解析拒绝；
- 禁止跟随重定向（重定向目标无法预校验）；快照路径常量 + realpath 归属校验。

运行边界：只被 main.py 的 /api/sites/sync-guide 端点调用（手动触发，绝无定时/自动
同步）。解析与合并可离线自测（app/selftest.py）。
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path

from . import url_guard
from .state import DATA_DIR, save_json_atomic

# ---------- 常量白名单（URL 唯一来源；前端/请求体传入的 URL 一律不采信） ----------

# 2026-11-11 用户指定新增：everness.info（Everness Database，espers/<id> 数据 +
# api.everness.info 资产域，子域由 endswith 规则自动放行）、nte.nanoka.cc
ALLOWED_HOSTS = ("ntestrategy.com", "wiki.mysqil.com", "everness.info", "nte.nanoka.cc")

NTESTRATEGY_SHARED_JS = "https://ntestrategy.com/js/shared.js"
MYSQIL_CHARACTERS_PAGE = "https://wiki.mysqil.com/characters/"
MYSQIL_SITEMAP_XML = "https://wiki.mysqil.com/sitemap-0.xml"
MYSQIL_IMAGES_BASE = "https://wiki.mysqil.com/images/characters/"

# mysqil 拼音 slug → 中文实名对照表（25 项逐一核验，2026-11-10：wiki /characters/
# 页卡片 h3 显示名与 data/mysqil/upgrades.json 角色页 <title> 双源一致；仅作页面
# 解析失败时的兜底——正常路径以站方页面实时解析为准，绝不绕过站方实名）。
# 塔吉多（tajiduo）未在 upgrades.json（10-31 快照，角色当时未上线），以页面实抓为准。
MYSQIL_SLUG_NAMES = {
    "adeler": "阿德勒", "anhunqu": "安魂曲", "baizang": "白藏", "bohe": "薄荷",
    "canhong": "残虹", "dafutier": "达芙蒂尔", "edjia": "埃德嘉",
    "fadiya": "法帝娅", "haiyue": "海月", "haniya": "哈尼娅",
    "hasuoer": "哈索尔", "heiyu": "黑羽", "jiuyuan": "九原", "kaesi": "卡厄斯",
    "ling": "零", "lingke": "灵可", "mingyin": "明音凛", "nanali": "娜娜莉",
    "tajiduo": "塔吉多", "xiaozhi": "小吱", "xun": "浔", "yi": "翳",
    "yiluoyi": "伊洛伊", "zaowu": "早雾", "zhenhong": "真红",
}

SITES_DIR = os.path.join(DATA_DIR, "sites")
SNAPSHOT_JSON = os.path.join(SITES_DIR, "guide_sites.json")

# 材料名/实名消毒上限：与 _sanitize_libraries 的 32 字对齐（材料参考名同 cap）
NAME_CAP = 32


# ---------- JS 对象字面量解析（shared.js 内嵌数据；纯 stdlib，无需 JS 运行时） ----------
# 与 wiki_fetch 的解析器同思路，另做两件 shared.js 需要的事：
# 1) 解析前剥离 // 与 /* */ 注释（shared.js 数据数组内部就有注释行）；
# 2) 值位置的裸标识符引用（img: IMG.nanally）按不透明引用吞掉——图片地址不进目录。

class _ParseError(ValueError):
    pass


def strip_js_comments(text):
    """剥离 // 行注释与 /* */ 块注释；字符串（含反引号）内的 // 原样保留。"""
    out = []
    i, n = 0, len(text)
    in_str = None
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:  # 转义字符整体保留
                out.append(text[i + 1])
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in "\"'`":
            in_str = c
            out.append(c)
            i += 1
            continue
        if c == "/" and text[i:i + 2] == "//":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if text[i:i + 2] == "/*":
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _skip_ws(s, i):
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def _parse_string(s, i):
    quote = s[i]
    i += 1
    out = []
    while i < len(s):
        c = s[i]
        if c == "\\":
            if i + 1 >= len(s):
                raise _ParseError("字符串以反斜杠结尾")
            out.append({"n": "\n", "t": "\t", "r": "\r"}.get(s[i + 1], s[i + 1]))
            i += 2
            continue
        if c == quote:
            return "".join(out), i + 1
        out.append(c)
        i += 1
    raise _ParseError("字符串未闭合")


def _parse_literal(s, i):
    m = re.match(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", s[i:])
    if m:
        text = m.group(0)
        return (float(text) if any(ch in text for ch in ".eE") else int(text)), i + len(text)
    m = re.match(r"true|false|null|undefined", s[i:])
    if m:
        return {"true": True, "false": False, "null": None,
                "undefined": None}[m.group(0)], i + len(m.group(0))
    raise _ParseError(f"无法解析的字面量（位置 {i} 附近：{s[i:i + 20]!r}）")


_IDENT_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*")


def _parse_value(s, i):
    i = _skip_ws(s, i)
    if i >= len(s):
        raise _ParseError("意外结束")
    c = s[i]
    if c in "\"'`":
        return _parse_string(s, i)
    if c == "{":
        return _parse_object(s, i)
    if c == "[":
        return _parse_array(s, i)
    m = _IDENT_RE.match(s, i)  # 裸标识符（IMG.nanally 等）：吞成不透明引用占位
    if m:
        return {"__ref__": m.group(0)}, m.end()
    return _parse_literal(s, i)


def _parse_object(s, i):
    i = _skip_ws(s, i + 1)
    obj = {}
    if i < len(s) and s[i] == "}":
        return obj, i + 1
    while True:
        i = _skip_ws(s, i)
        if i >= len(s):
            raise _ParseError("对象未闭合")
        if s[i] in "\"'`":
            key, i = _parse_string(s, i)
        else:
            j = i
            while j < len(s) and s[j] not in ":,} \t\r\n":
                j += 1
            if j == i:
                raise _ParseError(f"对象键缺失（位置 {i}）")
            key, i = s[i:j], j
        i = _skip_ws(s, i)
        if i >= len(s) or s[i] != ":":
            raise _ParseError(f"对象键 {key!r} 后缺少冒号")
        value, i = _parse_value(s, i + 1)
        obj[key] = value
        i = _skip_ws(s, i)
        if i < len(s) and s[i] == ",":
            i = _skip_ws(s, i + 1)
            if i < len(s) and s[i] == "}":  # 尾逗号容忍
                return obj, i + 1
            continue
        if i < len(s) and s[i] == "}":
            return obj, i + 1
        raise _ParseError(f"对象应为 , 或 }}（位置 {i} 附近：{s[i:i + 20]!r}）")


def _parse_array(s, i):
    i = _skip_ws(s, i + 1)
    arr = []
    if i < len(s) and s[i] == "]":
        return arr, i + 1
    while True:
        value, i = _parse_value(s, i)
        arr.append(value)
        i = _skip_ws(s, i)
        if i < len(s) and s[i] == ",":
            i = _skip_ws(s, i + 1)
            if i < len(s) and s[i] == "]":  # 尾逗号容忍
                return arr, i + 1
            continue
        if i < len(s) and s[i] == "]":
            return arr, i + 1
        raise _ParseError(f"数组应为 , 或 ]（位置 {i} 附近：{s[i:i + 20]!r}）")


def _extract_const(js_text, name):
    """从（已去注释的）JS 文本中提取 `const <name> = <字面量>`；缺块抛 ValueError。"""
    m = re.search(rf"\bconst\s+{re.escape(name)}\s*=", js_text)
    if not m:
        raise ValueError(f"shared.js 缺少 {name} 数据块（页面结构可能已改版）")
    value, _ = _parse_value(js_text, m.end())
    return value


def _extract_optional(js_text, name):
    """同 _extract_const，但缺块返回 None（BIS 建议块为增量数据：缺失不阻断
    主目录抓取，计数为 0 即如实暴露）。"""
    m = re.search(rf"\bconst\s+{re.escape(name)}\s*=", js_text)
    if not m:
        return None
    value, _ = _parse_value(js_text, m.end())
    return value


# ---------- 消毒 ----------

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def clean_text(value, cap=NAME_CAP):
    """站点字符串消毒：仅接受 str；滤控制字符、折叠空白、截断；空串返回 ''。"""
    if not isinstance(value, str):
        return ""
    return _CTRL_RE.sub(" ", re.sub(r"\s+", " ", value)).strip()[:cap]


def _clean_str_list(value, cap=64, limit=12):
    """BIS 建议里的列表字段消毒：str → 单元素列表；list → 逐项 clean_text 去空；
    其余类型 → []（绝不编造）。"""
    if isinstance(value, str):
        text = clean_text(value, cap)
        return [text] if text else []
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:limit]:
        text = clean_text(item if isinstance(item, str) else "", cap)
        if text:
            out.append(text)
    return out


def _clean_trilingual(value, cap=200):
    """BIS 建议字段实抓形态（2026-11-07 shared.js 实测）：{en, zh, ja} 三语文本对象。
    消毒为同名三键（缺失/类型不符 → 空串；纯串 → 视为 zh 兜底）。"""
    if isinstance(value, str):
        text = clean_text(value, cap)
        return {"zh": text, "en": "", "ja": ""}
    node = value if isinstance(value, dict) else {}
    return {k: clean_text(node.get(k), cap) for k in ("zh", "en", "ja")}


def _clean_name_dict(node):
    """name:{en,zh,ja} 形态 → (zh, en)；非对象/缺 zh 视为无效。"""
    if not isinstance(node, dict):
        return "", ""
    return clean_text(node.get("zh")), clean_text(node.get("en"))


# ---------- 拉取（SSRF 防线：https + 主机白名单 + 私网 IP 阻断 + 禁重定向） ----------
# 2026-11-14 B2.5/XMOD-01：防线唯一实现收敛进 app/url_guard.py（六类错误形态 +
# 空解析 fail-closed + 传输期 IP 钉扎）；本模块只保留四域常量白名单（子域放行口径，
# api.everness.info 资产域靠 endswith）与薄包装（selftest/探针以模块内符号调用）。

def host_allowed(hostname):
    """主机归一化后是否在白名单：严格等于允许域，或为其子域（.域 结尾）。
    前缀伪装（evil-ntestrategy.com）与后缀伪装（ntestrategy.com.evil.com）都不匹配。"""
    return url_guard.host_allowed(hostname, allowed_hosts=ALLOWED_HOSTS,
                                  allow_subdomains=True)


def _validate_url(url):
    """委托共享层（返回 .url 字符串，保持既有返回类型）：仅允许 https + 白名单
    主机（含子域）+ 443 端口 + 无用户信息；解析出的全部 IP 都不得落在私网/环回/
    链路本地/保留/组播/未指定段，空解析一律拒绝（防引向内网/本机面板/云元数据）。"""
    return url_guard.validate_url(url, allowed_hosts=ALLOWED_HOSTS,
                                  allow_subdomains=True, action="抓取").url


_NoRedirect = url_guard._NoRedirect
_OPENER = url_guard.OPENER


def _http_get(url, timeout):
    raw = url_guard.open_pinned(
        url, timeout=timeout, opener=_OPENER, allowed_hosts=ALLOWED_HOSTS,
        allow_subdomains=True, action="抓取",
        headers={"User-Agent": "Mozilla/5.0 (OKntePlanner sites-fetch)"})
    return raw.decode("utf-8", errors="replace")


def _ensure_under(path, base):
    """路径穿越防线：realpath 归属校验，写目标必须落在 base 目录内。"""
    root = os.path.realpath(base)
    target = os.path.realpath(path)
    if os.path.commonpath([target, root]) != root:
        raise ValueError(f"拒绝写入允许目录之外的路径：{path}")
    return target


# ---------- 两站抓取+解析（单站失败抛错，由 sync_guide_sites 兜住不影响另一站） ----------

def _sanitize_builds(js):
    """BIS 建议三块消毒（字段实抓形态，2026-11-07 shared.js 实测：TRAINS=
    [{id, stats:{en,zh,ja}, arc:…, mod:…, awk:…, skill:…}]、CONSOLE_BUILDS=
    [{id, cartridge:{zh,en,ja}, driveReq:…, driveOpt:…, trait:…, mainstats:…,
    substats:…}]（trilingual 均为 {en,zh,ja} 文本对象）、CHAR_ARCS=[{id, picks:
    [{rank:"BIS"|"STRONG"|"OK", en, zh}]}]；字段缺失/类型不符一律空值兜底，绝不
    编造）。仅建议展示用——不参与目录合并，绝不写 stigma_groups。"""
    trains, consoles, char_arcs = [], [], []
    for entry in _extract_optional(js, "TRAINS") or []:
        if not isinstance(entry, dict):
            continue
        sid = clean_text(entry.get("id"), 64)
        if not sid:
            continue
        trains.append({"id": sid,
                       "stats": _clean_trilingual(entry.get("stats")),
                       "arc": _clean_trilingual(entry.get("arc")),
                       "mod": _clean_trilingual(entry.get("mod")),
                       "awk": _clean_trilingual(entry.get("awk")),
                       "skill": _clean_trilingual(entry.get("skill"))})
    for entry in _extract_optional(js, "CONSOLE_BUILDS") or []:
        if not isinstance(entry, dict):
            continue
        sid = clean_text(entry.get("id"), 64)
        if not sid:
            continue
        consoles.append({"id": sid,
                         "cartridge": _clean_trilingual(entry.get("cartridge")),
                         "driveReq": _clean_trilingual(entry.get("driveReq")),
                         "driveOpt": _clean_trilingual(entry.get("driveOpt")),
                         "trait": _clean_trilingual(entry.get("trait")),
                         "mainstats": _clean_trilingual(entry.get("mainstats")),
                         "substats": _clean_trilingual(entry.get("substats"))})
    for entry in _extract_optional(js, "CHAR_ARCS") or []:
        if not isinstance(entry, dict):
            continue
        sid = clean_text(entry.get("id"), 64)
        picks = []
        picks_src = entry.get("picks") if isinstance(entry.get("picks"), list) else []
        for pick in picks_src[:8]:
            if not isinstance(pick, dict):
                continue
            rank = str(pick.get("rank") or "").strip().upper()
            if rank not in ("BIS", "STRONG", "OK"):
                rank = ""
            zh, en = _clean_name_dict(pick.get("name")) if isinstance(pick.get("name"), dict) \
                else (clean_text(pick.get("zh")), clean_text(pick.get("en")))
            if not (zh or en):
                continue
            picks.append({"rank": rank, "en": en, "zh": zh})
        if sid and picks:
            char_arcs.append({"id": sid, "picks": picks[:8]})
    return {"trains": trains, "console_builds": consoles, "char_arcs": char_arcs}


def _merge_builds_by_slug(builds, chars):
    """三块建议按 slug 归并为逐角色条目（快照 builds 段形态）：
    [{id, name(CHARS zh 实名，查不到留空), source, train, console, arcs}]——
    只保留至少命中一块的 slug；name 仅作前端与本地角色对齐用（slug 才是关联键）。"""
    name_by_slug = {c.get("slug"): c.get("name")
                    for c in chars if isinstance(c, dict) and c.get("slug")}
    merged = {}
    for slug, train in ((b["id"], b) for b in builds.get("trains") or []):
        merged.setdefault(slug, {})["train"] = {k: v for k, v in train.items() if k != "id"}
    for slug, con in ((b["id"], b) for b in builds.get("console_builds") or []):
        merged.setdefault(slug, {})["console"] = {k: v for k, v in con.items() if k != "id"}
    for slug, arcs in ((b["id"], b) for b in builds.get("char_arcs") or []):
        merged.setdefault(slug, {})["arcs"] = arcs.get("picks") or []
    return [{"id": slug, "name": name_by_slug.get(slug, ""), "source": "ntestrategy",
             **parts} for slug, parts in merged.items()]


def fetch_ntestrategy(timeout=15):
    """抓取并解析 shared.js：返回 {characters, arcs, materials, builds}（已消毒）。

    characters: [{name, name_en, slug, rarity}]；arcs: [{name, name_en, rarity, type}]；
    materials: [材料原文名]（CHAR_MATS items.zh，供与现有材料别名比对）；
    builds: {trains, console_builds, char_arcs}（BIS 建议原始三块，未按 slug 归并——
    归并在 sync_guide_sites 落快照时做）。
    """
    js = strip_js_comments(_http_get(NTESTRATEGY_SHARED_JS, timeout))
    chars, arcs, char_mats = [], [], []
    for entry in _extract_const(js, "CHARS") or []:
        if not isinstance(entry, dict):
            continue
        zh, en = _clean_name_dict(entry.get("name"))
        if zh:
            chars.append({"name": zh, "name_en": en, "slug": clean_text(entry.get("id"), 64),
                          "rarity": clean_text(entry.get("rarity"), 8), "source": "ntestrategy"})
    for entry in _extract_const(js, "ARCS") or []:
        if not isinstance(entry, dict):
            continue
        zh, en = _clean_name_dict(entry.get("name"))
        if zh:
            arcs.append({"name": zh, "name_en": en,
                         "rarity": clean_text(entry.get("rarity"), 8),
                         "type": clean_text(entry.get("type"), 16), "source": "ntestrategy"})
    for entry in _extract_const(js, "CHAR_MATS") or []:
        if not isinstance(entry, dict):
            continue
        for item in entry.get("items") or []:
            if isinstance(item, dict):
                zh = clean_text(item.get("zh"))
                if zh and zh not in char_mats:
                    char_mats.append(zh)
    builds = _sanitize_builds(js)
    if not chars and not arcs:
        raise ValueError("shared.js 未解析出任何角色/弧盘条目（结构可能已改版）")
    return {"characters": chars, "arcs": arcs, "materials": char_mats, "builds": builds}


def _parse_character_cards(page_html):
    """/characters/ 页 HTML → [(拼音 slug, 中文名)]（站方卡片形态：每个角色一张
    <a href="/characters/<slug>" …> 卡，卡内 <h3>显示中文名</h3>；2026-11-10 实抓
    25 卡全解析）。拿不到 h3 的卡不进结果——实名绝不编造。解析为空返回 []。"""
    out, seen_slugs = [], []
    for m in re.finditer(r'<a href="/characters/([a-z0-9-]+)"', page_html):
        slug = m.group(1)
        if slug in seen_slugs:
            continue
        seen_slugs.append(slug)
        h3 = re.search(r"<h3[^>]*>([^<]+)</h3>", page_html[m.end():m.end() + 2000])
        name = clean_text(h3.group(1)) if h3 else ""
        if name:
            out.append((slug, name))
    return out


def fetch_mysqil_names(timeout=15):
    """抓取 mysqil 角色图鉴：返回 {characters, names_source, unmapped_slugs}（已消毒）。

    characters: [{name(站方中文实名), name_en(""), slug(拼音), rarity(""),
    image("/images/characters/<slug>.webp"), source: "mysqil"}]；
    首选 /characters/ 页实时解析（站方显示中文名，无需对照表）；页面抓取失败或
    解析为空时回落 sitemap-0.xml 的 slug 清单 + MYSQIL_SLUG_NAMES 已核对照表。
    对照表也覆盖不了的 slug 一律不编造实名——记入 unmapped_slugs 如实暴露。
    """
    chars, unmapped = [], []
    names_source = "page"
    try:
        page = _http_get(MYSQIL_CHARACTERS_PAGE, timeout)
        page_slugs = re.findall(r'<a href="/characters/([a-z0-9-]+)"', page)
        pairs = _parse_character_cards(page)
        if pairs:
            named = {slug for slug, _ in pairs}
            unmapped = [s for s in page_slugs if s not in named]
            for slug, name in pairs:
                chars.append({"name": name, "name_en": "", "slug": slug, "rarity": "",
                              "image": f"/images/characters/{slug}.webp",
                              "source": "mysqil"})
    except (OSError, ValueError):
        chars = []
    if not chars:  # 页面抓取/解析失败 → sitemap slug 清单 + 已核对照表兜底
        names_source = "sitemap+table"
        sitemap = _http_get(MYSQIL_SITEMAP_XML, timeout)
        slugs = sorted(set(re.findall(r"/characters/([^/<]+)/", sitemap)))
        if not slugs:
            raise ValueError("mysqil sitemap 未解析出任何角色 slug（结构可能已改版）")
        unmapped = []
        for slug in slugs:
            name = MYSQIL_SLUG_NAMES.get(slug)
            if name:
                chars.append({"name": name, "name_en": "", "slug": slug, "rarity": "",
                              "image": f"/images/characters/{slug}.webp",
                              "source": "mysqil"})
            else:
                unmapped.append(slug)
    if not chars:
        raise ValueError("mysqil 未解析出任何角色名条目（结构可能已改版）")
    return {"characters": chars, "names_source": names_source,
            "unmapped_slugs": unmapped}


# ---------- 目录合并（纯数据：传入现有目录，返回合并结果与统计） ----------

def _merge_key(entry):
    """跨源归并键：英文实名优先，slug 兜底（大小写不敏感）。"""
    return (entry.get("name_en") or entry.get("slug") or "").casefold().replace(" ", "")


def merge_catalogs(char_library, weapon_library, parsed):
    """按合并策略算出目录合并结果与统计（不覆盖已有实名，缺失补录，来源标注）。

    parsed: {"ntestrategy": {...} 或 None, "mysqil": {...} 或 None}（各站解析结果，
    单站失败为 None；mysqil 只提供角色名表，弧盘/材料两腿自然为空）。
    跨源同名实体（英文名/slug 归并）只让先到站点的译名进目录，后到站点的译名记入
    alt_names（来源标注在快照里，不产生重复目录条目）。
    返回 (merge, stats)：
      merge = {"char_library": [...], "weapon_library": [...]}（可直接写回 STATE）
      stats = {"char_library": {...}, "weapon_library": {...}, "materials": {...},
               "alt_names": [...]}
    """
    merge = {"alt_names": []}
    stats = {}
    # 目录合并：角色与弧盘同一套逻辑（existing 保留在前，新名追加，≤200 由调用方消毒）
    for kind, source_keys in (("char_library", ("characters", "characters")),
                              ("weapon_library", ("arcs", "weapons"))):
        existing = [str(n) for n in (char_library if kind == "char_library" else weapon_library)]
        present = set(existing)
        added, seen_keys = [], {}  # seen_keys: 归并键 → 该实体进目录所用名（首个译名）
        fetched = 0
        for src, key in zip(("ntestrategy", "mysqil"), source_keys):
            data = (parsed.get(src) or {}).get(key) or []
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                fetched += 1
                name = entry.get("name")
                if not name:
                    continue
                mkey = _merge_key(entry)
                if mkey and mkey in seen_keys:
                    # 跨源同实体：后到译名不进目录；与该实体已进目录之名相同的重复
                    # 记录无信息量不入 alt_names，不同译名记为别名（来源标注）
                    if name != seen_keys[mkey]:
                        merge["alt_names"].append({"kind": kind, "name": name,
                                                   "source": entry.get("source", src)})
                    continue
                if mkey:
                    seen_keys[mkey] = name
                if name in present:
                    continue  # 不覆盖已有实名
                present.add(name)
                added.append(name)
        stats[kind] = {"fetched": fetched, "added": added,
                       "added_count": len(added), "existing": len(existing),
                       "snapshot_entries": len(existing) + len(added)}
        merge[kind] = existing + added
    # 材料：只与现有别名比对；对不上的进 unmapped 清单（不造 mat_id）
    from . import planner  # 懒加载：避免模块导入期依赖
    known = set()
    for aliases in (planner.MATERIALS.get("item_names") or {}).values():
        known.update(a for a in (aliases or []) if isinstance(a, str))
    for group in ("targets", "extra_materials"):
        for entry in planner.MATERIALS.get(group) or []:
            if isinstance(entry, dict) and isinstance(entry.get("display_name"), str):
                known.add(entry["display_name"])
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                known.add(entry["name"])
    known.discard("")
    candidates = []
    for src in ("ntestrategy", "mysqil"):
        candidates.extend((parsed.get(src) or {}).get("materials") or [])
    seen = set()
    mapped, unmapped = 0, []
    for name in candidates:
        if name in seen:
            continue
        seen.add(name)
        if name in known:
            mapped += 1
        else:
            unmapped.append(name)
    stats["materials"] = {"candidates": len(seen), "known": mapped, "unmapped": unmapped}
    stats["alt_names"] = merge["alt_names"]  # 与 sync_guide_sites 的快照合并口径一致
    return merge, stats


# ---------- 手动同步入口（main.py /api/sites/sync-guide 唯一调用方） ----------

def _load_snapshot():
    try:
        with open(SNAPSHOT_JSON, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _strip_legacy_nteguide(items):
    """既往快照里的 nteguide 条目一次性剔除（2026-11-10 起该站全面禁用：
    数据/图片/链接一律不用，见仓库根 AGENTS.md）。非 dict/无 source 的条目保留。"""
    return [e for e in (items or []) if not (isinstance(e, dict) and e.get("source") == "nteguide")]


def _replace_source_entries(prev_list, source_tag, ok, new_list):
    """快照参考列表的按源替换：本源本次抓取成功 → 用新条目整体替换该源段；
    失败 → 原样保留上一次的完好条目（失败回退，空列表与抓取失败可区分）。"""
    kept = [e for e in (prev_list or []) if isinstance(e, dict) and e.get("source") != source_tag]
    return kept + list(new_list or []) if ok else (prev_list or [])


def sync_guide_sites(char_library, weapon_library, timeout=15):
    """抓取两站 → 解析消毒 → 与给定目录合并 → 快照落盘。返回 (merge, payload)。

    单站网络/解析失败不影响另一站（错误记入 payload.sources.*.error；快照里该源
    的参考条目沿用上一次的完好数据）。
    两站全挂时也正常返回（payload.ok=False 由调用方判断后回 502）。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    parsed, sources = {}, {}
    for src, fetcher in (("ntestrategy", fetch_ntestrategy),
                         ("mysqil", fetch_mysqil_names)):
        try:
            parsed[src] = fetcher(timeout)
            sources[src] = {"ok": True, "error": "", "fetched_at": now}
        except (OSError, ValueError) as e:
            parsed[src] = None
            sources[src] = {"ok": False, "error": str(e)[:200], "fetched_at": now}
    # 各源条目计数（统计口径：解析消毒后的有效条目）
    nte_builds = (parsed.get("ntestrategy") or {}).get("builds") or {}
    sources["ntestrategy"].update({
        "characters": len((parsed.get("ntestrategy") or {}).get("characters") or []),
        "arcs": len((parsed.get("ntestrategy") or {}).get("arcs") or []),
        "materials": len((parsed.get("ntestrategy") or {}).get("materials") or []),
        "builds": {"trains": len(nte_builds.get("trains") or []),
                   "console_builds": len(nte_builds.get("console_builds") or []),
                   "char_arcs": len(nte_builds.get("char_arcs") or [])}})
    sources["mysqil"].update({
        "characters": len((parsed.get("mysqil") or {}).get("characters") or []),
        "names_source": str((parsed.get("mysqil") or {}).get("names_source") or ""),
        "unmapped_slugs": list((parsed.get("mysqil") or {}).get("unmapped_slugs") or [])})

    merge, stats = merge_catalogs(char_library, weapon_library, parsed)

    # 快照：上一份快照保留 alt_names/unmapped 已有项（幂等，不随单次抓取抖动丢失）；
    # nteguide 既往条目在装载时一次性剔除（站方禁用，不再参与任何合并/回退）
    prev = _load_snapshot()
    prev_alt = prev.get("alt_names") if isinstance(prev.get("alt_names"), list) else []
    prev_unmapped = prev.get("guide_unmapped_materials") \
        if isinstance(prev.get("guide_unmapped_materials"), list) else []
    prev_chars = _strip_legacy_nteguide(
        prev.get("characters") if isinstance(prev.get("characters"), list) else [])
    prev_arcs = _strip_legacy_nteguide(
        prev.get("arcs") if isinstance(prev.get("arcs"), list) else [])
    alt_names = [a for a in prev_alt
                 if isinstance(a, dict) and a.get("source") != "nteguide"]
    for a in stats["alt_names"]:
        if a not in alt_names:
            alt_names.append(a)
    unmapped = [u for u in prev_unmapped if isinstance(u, dict)]
    known_unmapped = {u.get("name") for u in unmapped}
    for name in stats["materials"]["unmapped"]:
        if name not in known_unmapped:
            unmapped.append({"name": name, "source": "ntestrategy CHAR_MATS",
                             "first_seen": now})
    stats["alt_names"] = alt_names
    stats["materials"]["unmapped"] = unmapped

    # 参考列表按源替换：抓取成功的源用本次条目，失败的源沿用上次完好条目
    chars = _replace_source_entries(prev_chars, "ntestrategy",
                                    sources["ntestrategy"]["ok"],
                                    (parsed.get("ntestrategy") or {}).get("characters") or [])
    chars = _replace_source_entries(chars, "mysqil",
                                    sources["mysqil"]["ok"],
                                    (parsed.get("mysqil") or {}).get("characters") or [])
    arcs = _replace_source_entries(prev_arcs, "ntestrategy",
                                   sources["ntestrategy"]["ok"],
                                   (parsed.get("ntestrategy") or {}).get("arcs") or [])
    # BIS 建议段（独立于目录；按 slug 归并，仅建议展示——不进 char/weapon_library，
    # 绝不写 stigma_groups）：ntestrategy 抓取成功才整段替换，失败沿用上次完好段
    prev_builds = prev.get("builds") if isinstance(prev.get("builds"), list) else []
    builds = _replace_source_entries(
        prev_builds, "ntestrategy", sources["ntestrategy"]["ok"],
        _merge_builds_by_slug(nte_builds,
                              (parsed.get("ntestrategy") or {}).get("characters") or []))

    payload = {
        "fetched_at": now,
        "ok": bool(sources["ntestrategy"]["ok"] or sources["mysqil"]["ok"]),
        "sources": sources,
        "merge": stats,
        "characters": chars,
        "arcs": arcs,
        "builds": builds,
        "alt_names": alt_names,
        "guide_unmapped_materials": unmapped,
    }
    if any(s["ok"] for s in sources.values()):  # 至少一站成功才刷新快照
        _ensure_under(SNAPSHOT_JSON, SITES_DIR)
        os.makedirs(SITES_DIR, exist_ok=True)
        save_json_atomic(SNAPSHOT_JSON, payload)
    return merge, payload


def load_snapshot():
    """读取最近一次同步快照（无则 None；供后续展示/人工核对材料清单）。"""
    data = _load_snapshot()
    return data or None


# ---------- 角色头像（2026-11-10：mysqil /images/characters/ → 本机落盘，手动同步顺带触发） ----------
# 只下载 wiki.mysqil.com 白名单域内的固定路径头像；文件名 = <slug>.webp（slug 已按
# 消毒口径小写连字符）。缓存目录与 manifest 均为本机运行时数据。
# 选型口径（2026-11-10 实测）：优先 <slug>.webp 立绘（2160x2560 竖版），像素解析
# 判竖版（h>w）才采用；横图/解析失败/404 回落 <slug>-icon.webp（256x256 方图）；
# 两路都失败该 slug 计入 failed 名单（单张失败静默不抛——头像非关键数据）。

AVATARS_DIR = os.path.join(DATA_DIR, "avatars")
AVATAR_MANIFEST_JSON = os.path.join(AVATARS_DIR, "manifest.json")
SLUG_RE = re.compile(r"^[a-z0-9-]+$")


def _webp_pixel_size(raw):
    """WEBP 字节流 → (宽, 高)；无法解析返回 None（纯 stdlib，selftest 真机夹具覆盖）。

    支持三种 chunk 形态（站方实测两类文件全部命中）：
    - VP8X：扩展容器（含 ALPH/动画位），canvas 尺寸在 24bit width-1 + 24bit height-1；
    - VP8 ：简单有损帧（0x9d012a 起始码后 14bit 宽 + 14bit 高，低 14 位有效）；
    - VP8L：无损帧（签名字节 0x2F 后 14bit width-1 + 14bit height-1 位包）。
    非 RIFF/WEBP 容器或截断一律 None（调用方按横图处理回落图标，绝不猜尺寸）。"""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 30:
        return None
    raw = bytes(raw)
    if raw[:4] != b"RIFF" or raw[8:12] != b"WEBP":
        return None
    chunk = raw[12:16]
    if chunk == b"VP8X":
        return (int.from_bytes(raw[24:27], "little") + 1,
                int.from_bytes(raw[27:30], "little") + 1)
    if chunk == b"VP8 ":
        if len(raw) < 30 or raw[23:26] != b"\x9d\x01\x2a":
            return None
        return (int.from_bytes(raw[26:28], "little") & 0x3FFF,
                int.from_bytes(raw[28:30], "little") & 0x3FFF)
    if chunk == b"VP8L" and raw[20] == 0x2F:
        bits = int.from_bytes(raw[21:25], "little")
        return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    return None


def _http_get_bytes(url, timeout):
    """二进制版 _http_get（同一条 SSRF 防线：共享层校验 + 禁重定向 + IP 钉扎）。"""
    return url_guard.open_pinned(
        url, timeout=timeout, opener=_OPENER, allowed_hosts=ALLOWED_HOSTS,
        allow_subdomains=True, action="抓取",
        headers={"User-Agent": "Mozilla/5.0 (OKntePlanner sites-fetch)"})


def _load_avatar_manifest():
    try:
        with open(AVATAR_MANIFEST_JSON, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _fetch_mysqil_avatar_bytes(slug, timeout):
    """单 slug 头像字节与实际源 URL：立绘优先、竖版才用，横图/失败回落图标。
    返回 (raw, source_url)；两路都拿不到抛 (OSError, ValueError)。"""
    portrait_url = f"{MYSQIL_IMAGES_BASE}{slug}.webp"
    icon_url = f"{MYSQIL_IMAGES_BASE}{slug}-icon.webp"
    try:
        raw = _http_get_bytes(portrait_url, timeout)
        size = _webp_pixel_size(raw)
        if size and size[1] > size[0]:  # 竖版立绘（实测 2160x2560）
            return raw, portrait_url
    except (OSError, ValueError):
        pass
    raw = _http_get_bytes(icon_url, timeout)  # 横图/解析失败/404 → 方形图标兜底
    if not raw:
        raise OSError(f"mysqil 头像下载为空：{slug}")
    return raw, icon_url


def download_avatars(entries, timeout=15):
    """mysqil 角色条目（slug）→ 下载头像到 data/avatars/<slug>.webp。

    选型：优先 <slug>.webp 立绘，_webp_pixel_size 判竖版才采用；非竖版/横图/
    解析失败/404 回落 <slug>-icon.webp；落盘命名统一 <slug>.webp，manifest 的
    source 记实际使用的完整源 URL（manifest 记 source URL）。
    安全线：URL 由本模块常量拼接（slug 必须 ^[a-z0-9-]+$，与 /avatars 路由正则
    同款，防路径拼接逃逸）+ _validate_url 白名单/私网阻断/禁重定向。原子写：
    Path(tmp).write_bytes → os.replace（二进制不落入任何 JSON 链）。既往 manifest
    登记的 source 未变且文件在磁盘 → 跳过下载（幂等：立绘 ~400KB/张，不重复拉）。
    manifest 合并语义（2026-11-10 修复「全量重写致失败条目掉出」）：读既有 manifest，
    既往条目全量保留，本轮成功条目追加/更新——单张下载失败不再把既往条目掉出
    manifest；不在本轮 feed 里的既往成功条目也保留（站点下架不影响已落盘头像的
    登记）。登记但文件已不在磁盘的死条目同样保留（avatar_files() 侧有 manifest∩
    磁盘把关，不产生悬空服务；该 slug 下次成功下载时条目自然被更新覆盖）。
    返回 (downloaded, skipped, failed)：failed 为本轮失败 slug 名单（由调用方透出到
    同步响应，便于核对缺口）。单张失败静默不抛——头像非关键数据，绝不阻断同步。
    """
    os.makedirs(AVATARS_DIR, exist_ok=True)
    prev_doc = _load_avatar_manifest()
    prev = prev_doc.get("avatars") if isinstance(prev_doc.get("avatars"), dict) else {}
    # 合并语义：既往条目全量保留（仅丢类型不合法项），失败不掉出
    manifest = {slug: item for slug, item in prev.items() if isinstance(item, dict)}
    downloaded = skipped = 0
    failed = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        slug = clean_text(entry.get("slug"), 64).lower()
        if not slug or not SLUG_RE.fullmatch(slug):
            continue
        fname = f"{slug}.webp"
        target = _ensure_under(os.path.join(AVATARS_DIR, fname), AVATARS_DIR)
        prev_source = str(prev.get(slug, {}).get("source") or "")
        if prev.get(slug, {}).get("file") == fname and os.path.exists(target) \
                and (prev_source == f"{MYSQIL_IMAGES_BASE}{slug}.webp"
                     or prev_source == f"{MYSQIL_IMAGES_BASE}{slug}-icon.webp"):
            # 源未变且文件在磁盘：跳过下载（manifest 逐字段保留）
            manifest[slug] = {"file": fname, "source": prev_source,
                              "fetched_at": prev[slug].get("fetched_at") or ""}
            skipped += 1
            continue
        try:
            raw, source_url = _fetch_mysqil_avatar_bytes(slug, timeout)
            tmp = target + ".tmp"
            Path(tmp).write_bytes(raw)
            os.replace(tmp, target)
            manifest[slug] = {"file": fname, "source": source_url,
                              "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            downloaded += 1
        except (OSError, ValueError):
            failed.append(slug)  # 单张失败静默跳过（同步主链不受影响）；既往条目已保留
    # manifest 落盘（合并集）；写失败不影响已落盘文件
    try:
        _ensure_under(AVATAR_MANIFEST_JSON, AVATARS_DIR)
        save_json_atomic(AVATAR_MANIFEST_JSON, {"schema": 1, "updated_at":
                          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                          "avatars": manifest})
    except OSError:
        pass
    return downloaded, skipped, failed


def avatar_files():
    """slug → 已落盘头像文件名（2026-11-11 起以磁盘枚举为准：任何 <slug>.webp|avif
    放进 data/avatars/ 即可用，手动投放无需 manifest 登记——同步只按 manifest 名单
    下载，不会覆盖手动文件；manifest 仍作下载追踪/来源留痕）。"""
    out = {}
    try:
        names = os.listdir(AVATARS_DIR)
    except OSError:
        return out
    for fname in sorted(names):
        m = re.fullmatch(r"([a-z0-9-]+)\.(webp|avif)", str(fname))
        if not m:
            continue
        out[m.group(1)] = fname
    return out
