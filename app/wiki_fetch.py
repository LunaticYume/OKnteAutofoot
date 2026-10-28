"""wiki.mysqil.com 养成分组数据的拉取、解析与角色预填（纯标准库）。

数据源：养成计算器页 https://wiki.mysqil.com/tools/cultivation 内嵌的 DetailedCalc.js
（Astro 静态资源，/_astro/DetailedCalc.*.js），其中 charSkillBook/charBossMat/
charWeeklyMat/charFieldMat 四组为 {材料或书系名: [角色名...]} 的权威分组。

本模块同时是 mysqil 材料名映射表（BOSS_MAT_ID/SKILL_BOOK_TO_N 等）的唯一真相，
tools/mysqil_ingest.py 懒加载复用。

安全边界（本模块是全仓库第一个服务端外联点）：
- 仅允许 https://wiki.mysqil.com（主机白名单 + 解析 IP 不得为私网/环回/链路本地/保留段）；
- 禁止跟随重定向（重定向目标无法预校验）；
- 快照路径为本模块常量（DATA_DIR/mysqil 下字面拼接），写入前再做 realpath 归属校验。

运行边界：只被 main.py 的 /api/game-data/wiki-fetch 端点调用（服务端拉取；前端受
同源策略与面板回环校验限制不能直连 wiki）。解析器可离线对仓库内
data/mysqil/DetailedCalc.fresh.js 夹具自测（selftest）。
"""
import ipaddress
import json
import os
import re
import socket
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from .state import DATA_DIR, save_json_atomic

WIKI_PAGE = "https://wiki.mysqil.com/tools/cultivation"
WIKI_HOST = "wiki.mysqil.com"
GROUP_KEYS = ("charSkillBook", "charBossMat", "charWeeklyMat", "charFieldMat")
_MYSQIL_DIR = Path(DATA_DIR) / "mysqil"
FRESH_JS = _MYSQIL_DIR / "DetailedCalc.fresh.js"
FRESH_JSON = _MYSQIL_DIR / "cultivation_data.fresh.json"

# —— mysqil 材料名映射表（唯一真相；ingest 与图鉴拉取共用）——
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

# 养成总量口径（每角色相同；阶段17 断言值：charBreak v0 合计 86、周本 8+3、小怪白当量 206+184）
BOSS_TOTAL = 86
WEEKLY_TOTAL = 8 + 3
MOB_BASE_TOTAL = 206 + 184


# ---------- JS 对象字面量解析（纯 stdlib，无需 JS 运行时） ----------

class _ParseError(ValueError):
    pass


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
            nxt = s[i + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
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
        if any(ch in text for ch in ".eE"):
            return float(text), i + len(text)
        return int(text), i + len(text)
    m = re.match(r"true|false|null|undefined", s[i:])
    if m:
        return {"true": True, "false": False, "null": None, "undefined": None}[m.group(0)], \
            i + len(m.group(0))
    raise _ParseError(f"无法解析的字面量（位置 {i} 附近：{s[i:i + 20]!r}）")


def _parse_value(s, i):
    i = _skip_ws(s, i)
    if i >= len(s):
        raise _ParseError("意外结束")
    c = s[i]
    if c in "\"'`":  # DetailedCalc.js 的字符串大量使用反引号字面量
        return _parse_string(s, i)
    if c == "{":
        return _parse_object(s, i)
    if c == "[":
        return _parse_array(s, i)
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
            i += 1
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


def parse_groupings(js_text):
    """从 DetailedCalc.js 文本提取四组权威分组；缺任一组抛 ValueError。"""
    groups = {}
    for key in GROUP_KEYS:
        m = re.search(rf"\b{key}\s*:\s*\{{", js_text)
        if not m:
            raise ValueError(f"wiki 数据缺少 {key}（页面结构可能已改版）")
        value, _ = _parse_value(js_text, m.end() - 1)
        if not isinstance(value, dict):
            raise ValueError(f"wiki {key} 不是对象")
        groups[key] = value
    return groups


# ---------- 拉取（SSRF 防线：主机白名单 + 私网 IP 阻断 + 禁重定向） ----------

def _validate_url(url):
    """仅允许 https 且主机严格等于 wiki.mysqil.com；解析出的全部 IP 都不得落在
    私网/环回/链路本地/保留/组播段（防把服务端请求引向内网、本机面板或云元数据）。"""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != WIKI_HOST:
        raise ValueError(f"仅允许抓取 https://{WIKI_HOST} 的资源，拒绝：{url}")
    for info in socket.getaddrinfo(parsed.hostname, 443, proto=socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise ValueError(f"{WIKI_HOST} 解析到受限地址 {ip}，拒绝抓取")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """重定向目标无法预校验，一律拒绝跟随。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError(f"拒绝跟随重定向（{code}）：{newurl}")


_OPENER = urllib.request.build_opener(_NoRedirect())


def _http_get(url, timeout):
    _validate_url(url)
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (OKntePlanner wiki-fetch)"})
    with _OPENER.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _ensure_under(path, base):
    """路径穿越防线：realpath 归属校验，写目标必须落在 base 目录内。"""
    root = os.path.realpath(base)
    target = os.path.realpath(path)
    if os.path.commonpath([target, root]) != root:
        raise ValueError(f"拒绝写入允许目录之外的路径：{path}")
    return target


def fetch_wiki_groupings(timeout=10):
    """抓取并解析 wiki 养成分组；成功后刷新本地 fresh 快照（内容未变则文件不变）。

    返回 {组名: {材料名: [角色名...]}}；网络/解析失败抛 (OSError, ValueError)。
    """
    html = _http_get(WIKI_PAGE, timeout)
    m = re.search(r'src="([^"]*DetailedCalc[^"]*\.js)"', html)
    if not m:
        raise ValueError("页面中未找到 DetailedCalc.js 资源链接（页面结构可能已改版）")
    js_text = _http_get(urllib.parse.urljoin(WIKI_PAGE, m.group(1)), timeout)
    groups = parse_groupings(js_text)
    _ensure_under(FRESH_JS, _MYSQIL_DIR)
    _ensure_under(FRESH_JSON, _MYSQIL_DIR)
    _MYSQIL_DIR.mkdir(parents=True, exist_ok=True)
    FRESH_JS.write_text(js_text, encoding="utf-8")
    fresh = {}
    if FRESH_JSON.is_file():
        try:
            fresh = json.loads(FRESH_JSON.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            fresh = {}
    if not isinstance(fresh, dict):
        fresh = {}
    fresh.update(groups)
    save_json_atomic(str(FRESH_JSON), fresh)
    return groups


# ---------- 角色反查与预填 ----------

def reverse_lookup(groups, name):
    """name 在四组中的归属 {组名: 材料或书系名}；未命中的组不出现在结果里。"""
    lookup = f"「{name}」" if name == "零" else name
    hit = {}
    for key in GROUP_KEYS:
        group = groups.get(key) or {}
        for material, members in group.items():
            if isinstance(members, list) and (lookup in members or name in members):
                hit[key] = material
                break
    return hit


def build_prefill(groups, name):
    """四组归属 → 图鉴录入的养成字段预填；分组不全或映射未登记时 found=False。"""
    hit = reverse_lookup(groups, name)
    missing = [k for k in GROUP_KEYS if k not in hit]
    if missing:
        return {"found": False, "missing": missing,
                "note": "wiki 暂未收录该角色的完整养成分组（缺：" + "、".join(missing) + "）"}
    book = hit["charSkillBook"]
    n = SKILL_BOOK_TO_N.get(book)
    if n is None:
        return {"found": False, "missing": ["charSkillBook"],
                "note": f"书系『{book}』未登记编号映射，请先在 wiki_fetch.SKILL_BOOK_TO_N 补充"}
    boss = hit["charBossMat"]
    weekly = hit["charWeeklyMat"]
    field = hit["charFieldMat"]
    if boss not in BOSS_MAT_ID or boss not in BOSS_NAME:
        return {"found": False, "missing": ["charBossMat"],
                "note": f"BOSS 材料『{boss}』未登记映射，请先在 wiki_fetch 补充"}
    if weekly not in WEEKLY_ID:
        return {"found": False, "missing": ["charWeeklyMat"],
                "note": f"周本材料『{weekly}』未登记映射，请先在 wiki_fetch 补充"}
    if field not in FIELD_FAMILY_SHORT:
        return {"found": False, "missing": ["charFieldMat"],
                "note": f"小怪族群『{field}』未登记映射，请先在 wiki_fetch 补充"}
    return {"found": True, "name": name, "prefill": {
        "skill_series": f"ability_{n}",
        "skill_book_name": book,
        "boss_material": {"mat_id": BOSS_MAT_ID[boss], "boss": BOSS_NAME[boss],
                          "total": BOSS_TOTAL, "confidence": "manual"},
        "weekly_material": {"mat_id": WEEKLY_ID[weekly], "total": WEEKLY_TOTAL,
                            "confidence": "manual"},
        "mob_family": FIELD_FAMILY_SHORT[field],
        "mob_base_total": MOB_BASE_TOTAL,
    }}


def build_prefill_for(name, timeout=10):
    """端点入口：拉取 → 反查 → 预填；附四组规模概览与抓取时间供 UI 展示。"""
    groups = fetch_wiki_groupings(timeout)
    result = build_prefill(groups, name)
    result["wiki_summary"] = {
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "groups": {key: sum(len(v) for v in groups[key].values() if isinstance(v, list))
                   for key in GROUP_KEYS},
    }
    return result
