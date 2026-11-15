"""冒烟测试（纯标准库）：真实拉起面板进程，用 urllib 走一遍 API 主链路。

链路：GET /api/bootstrap（含 compat）→ POST /api/characters → POST /api/inventory
→ POST /api/plan（200 且含 plan）→ POST /api/stop → 杀干净子进程。

运行方式（项目根目录）：
    D:/GamingAssistant/ok-nte/data/apps/ok-nte/python/python.exe -m app.smoke_test

约束：
- 绝不写入 D:/GamingAssistant/ok-nte 下任何文件（bootstrap/plan 只读检测）。
- 绝不启动游戏、绝不调 /api/start（不会真正拉起 ok-nte 任务）。
- data/state.json 会被子进程写入：测试前备份、finally 恢复（原本不存在则删除）。
- 结束时必须把面板子进程杀干净。

每个步骤 print 一行 PASS/FAIL；全部通过退出码 0，否则 1。
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

try:  # Windows 控制台中文输出保险
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(ROOT, "data", "state.json")
# SEC-05 面板令牌文件：源码态 DATA_DIR=仓库 data/（与子进程面板写入口径一致）
PANEL_TOKEN_FILE = os.path.join(ROOT, "data", "panel_token.json")
EMBEDDED_PY = r"D:\GamingAssistant\ok-nte\data\apps\ok-nte\python\python.exe"
PY = EMBEDDED_PY if os.path.isfile(EMBEDDED_PY) else sys.executable
PORT = 18765
BASE = f"http://127.0.0.1:{PORT}"
# 直连：系统代理/TUN 可能截获 127.0.0.1 请求导致假超时（与 build_exe 探针同口径）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
# 错 token 场景用非真实长串字面量（非凭据；与真实 token 撞不上）
WRONG_TOKEN = "x" * 64 + "-smoke-wrong-token-not-a-real-credential"

_panel_token_cache = {"key": None, "token": None}


def _panel_token():
    """读面板令牌（PANEL_TOKEN_FILE）。按 (mtime,size) 缓存：面板重启重新生成
    token 后 (mtime,size) 变化即自动跟进；文件缺失/损坏返回 None（调用方不带头
    → 轮询期自然 401 重试）。返回值绝不 print/断言进消息（token 不落任何输出）。"""
    try:
        st = os.stat(PANEL_TOKEN_FILE)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        _panel_token_cache.update(key=None, token=None)
        return None
    if _panel_token_cache["key"] != key:
        try:
            with open(PANEL_TOKEN_FILE, encoding="utf-8") as f:
                token = json.load(f).get("token")
        except (OSError, ValueError):
            _panel_token_cache.update(key=None, token=None)
            return None
        _panel_token_cache.update(key=key, token=token)
    return _panel_token_cache["token"]

SAMPLE_CHARACTER = {
    "name": "冒烟测试角色",
    "enabled": True,
    "mats": {"ability": 3, "arc": 2, "console": 1},
    "tracks": {},
    "extra_needs": {"exp_char": 30},
}


# B2.7A：panel_token uuid 双形态 tmp 匹配（save_json_atomic 命名，state.py:147
# 主 tmp / :154 bak tmp）：panel_token.json.<32位hex>.tmp 与
# panel_token.json.bak.<32位hex>.tmp——面板进程若在写中途被杀会残留这两种形态
# （含令牌明文），固定名单覆盖不到，按此正则随守卫一并快照/恢复；32 位 hex
# 严格收窄不过宽 glob：旧固定名 panel_token.json.tmp 与无关文件名不命中。
_PANEL_TOKEN_UUID_TMP_RE = re.compile(
    r"^panel_token\.json(?:\.bak)?\.[0-9a-f]{32}\.tmp$")


class _StateFileGuard:
    """备份 data/state.json 与 data/yields.json，退出时恢复（原本不存在则删除）。
    panel_token.json 主文件与 .bak 同管（SEC-05：面板每次启动重写主文件，二次写
    会轮转出 .bak；原本不存在则测后删除，绝不把测试期令牌留在仓库 data/）。
    B2.7A：panel_token 的 uuid 双形态 tmp（save_json_atomic 命名 state.py:147/:154）
    按形态扫描后纳入同一份快照——快照/恢复逻辑按 uuid 形态扩展，守卫语义不变
    （进守卫时存在→退出恢复原内容，进守卫时不存在→退出删除）。"""

    def __enter__(self):
        self._files = {}
        data_dir = os.path.dirname(STATE_FILE)
        for path in (STATE_FILE,
                     os.path.join(data_dir, "yields.json"),
                     os.path.join(data_dir, "requirements.json"),
                     os.path.join(data_dir, "panel_token.json"),
                     os.path.join(data_dir, "panel_token.json.bak")):
            existed = os.path.isfile(path)
            raw = None
            if existed:
                with open(path, "rb") as f:
                    raw = f.read()
            self._files[path] = (existed, raw)
        # uuid 双形态 tmp（B2.7A）：与固定名同一快照/恢复语义
        try:
            names = os.listdir(data_dir)
        except OSError:
            names = []
        for name in names:
            if _PANEL_TOKEN_UUID_TMP_RE.fullmatch(name):
                path = os.path.join(data_dir, name)
                existed = os.path.isfile(path)
                raw = None
                if existed:
                    with open(path, "rb") as f:
                        raw = f.read()
                self._files[path] = (existed, raw)
        return self

    def __exit__(self, *exc):
        for path, (existed, raw) in self._files.items():
            if existed:
                Path(path).write_bytes(raw)
            elif os.path.isfile(path):
                os.remove(path)
        # B2.7A：uuid 形态守卫期新增残留（进守卫时不存在、不在快照内）→
        # 一并删除，同固定名「原本不存在则测后删除」语义——绝不把测试期
        # 令牌 tmp 留在仓库 data/（面板写中途被杀的残留形态）。
        data_dir = os.path.dirname(STATE_FILE)
        try:
            names = os.listdir(data_dir)
        except OSError:
            names = []
        for name in names:
            path = os.path.join(data_dir, name)
            if _PANEL_TOKEN_UUID_TMP_RE.fullmatch(name) \
                    and path not in self._files and os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        return False


def _request(path, payload=None, timeout=30):
    """GET（payload=None）/ POST JSON，非 2xx 抛 HTTPError。返回解析后的 JSON。
    统一附 X-Panel-Token 头（SEC-05）：token 文件未生成（面板未起）时不带——
    轮询期自然 401 重试，文件出现后下一次请求即通过。"""
    url = BASE + path
    host = urllib.parse.urlsplit(url).hostname
    assert host == "127.0.0.1", f"冒烟测试只允许访问本机面板，拒绝: {url}"
    token = _panel_token()
    headers = {"X-Panel-Token": token} if token else {}
    if payload is None:
        req = urllib.request.Request(url, method="GET", headers=headers)
    else:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    with _OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _raw_request(path, payload=None, token=None, host=None, origin=None, timeout=30,
                 raw_body=None, headers=None):
    """鉴权场景低层请求：返回 (status, body_bytes)，非 2xx 不抛（401/403 是被测
    结果）。token=None 不带 X-Panel-Token；host/origin 用于伪造 DNS 重绑定形态
    （连接仍是回环 URL，Host/Origin 头是攻击者域）。raw_body 非 None 时直接以
    该 bytes 作 POST data（不 json.dumps、不走 GET 分支——SRV-01 原始体构造）；
    headers 为额外头（后写覆盖：传 "Content-length" 键可抑制 urllib 自动补写，
    原样伪造坏 CL / 超限声明 CL）。"""
    url = BASE + path
    assert urllib.parse.urlsplit(url).hostname == "127.0.0.1", f"只允许本机: {url}"
    headers = dict(headers) if headers else {}
    if token:
        headers["X-Panel-Token"] = token
    if host:
        headers["Host"] = host
    if origin:
        headers["Origin"] = origin
    if raw_body is not None:
        req = urllib.request.Request(url, data=raw_body, method="POST",
                                     headers=headers)
    elif payload is None:
        req = urllib.request.Request(url, method="GET", headers=headers)
    else:
        headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST", headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _json_body(raw):
    """鉴权场景 401/403 body → dict（非 JSON → {}），断言 panel_auth 字段用。"""
    try:
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def _wait_bootstrap(deadline_seconds=60):
    """轮询 GET /api/bootstrap 直到面板就绪，并断言响应含 compat 字段。"""
    last_err = None
    deadline = time.time() + deadline_seconds
    while time.time() < deadline:
        try:
            data = _request("/api/bootstrap")
            assert isinstance(data.get("compat"), dict), \
                f"bootstrap 响应缺少 compat 字段: {sorted(data.keys())}"
            return
        except AssertionError:
            raise
        except Exception as e:  # noqa: BLE001 — 未就绪时继续轮询
            last_err = e
            time.sleep(0.3)
    raise TimeoutError(f"面板 {BASE} {deadline_seconds} 秒内未就绪，最后一次错误: {last_err}")


def _step_characters():
    data = _request("/api/characters", {"characters": [SAMPLE_CHARACTER]})
    assert data.get("ok") is True, f"/api/characters 返回非 ok: {data}"
    assert data["characters"][0]["name"] == "冒烟测试角色", "角色未被保存"


def _step_inventory():
    data = _request("/api/inventory", {"inventory": {"exp_char": 5}})
    assert data.get("ok") is True, f"/api/inventory 返回非 ok: {data}"
    assert data["inventory"]["exp_char"] == 5, "库存未被保存"


def _step_plan():
    data = _request("/api/plan", {})
    plan = data.get("plan")
    assert isinstance(plan, dict), f"/api/plan 响应缺少 plan 字段: {sorted(data.keys())}"
    assert isinstance(plan.get("steps"), list), "plan.steps 应为数组"
    # 计划结构升级：空计划必须带 no_op_reason 枚举与中文文案；步骤必须带结构化 reason
    assert "no_op_reason" in plan, f"plan 应含 no_op_reason 字段: {sorted(plan.keys())}"
    if not plan["steps"]:
        assert plan.get("no_op_reason") and plan.get("reason"), \
            f"空计划必须带枚举原因与中文文案: {plan}"
    for s in plan["steps"]:
        assert isinstance(s.get("reason"), dict) and s["reason"].get("text"), \
            f"每个步骤应带结构化 reason（含 text）: {s}"


def _step_dry_run():
    data = _request("/api/dry-run", {})
    assert "compat" in data and "plan" in data, f"/api/dry-run 响应结构异常: {sorted(data.keys())}"
    if data.get("ok"):
        if data["plan"].get("steps"):
            patch = data.get("patch")
            assert patch and isinstance(patch.get("diff"), list) and patch["diff"], \
                f"有步骤时 dry-run 应返回补丁差异: {sorted(data.keys())}"
        else:
            assert data.get("patch") is None, "无步骤时不应有补丁"


def _step_yield_calibration_endpoints():
    data = _request("/api/yield-suggestions")
    assert isinstance(data.get("suggestions"), list), \
        f"/api/yield-suggestions 结构异常: {sorted(data.keys())}"
    # 校准建议的"应用"走产量表通道：回写当前值（内容不变，且 yields.json 有备份恢复兜底）
    boot = _request("/api/bootstrap")
    per_run = boot["tables"]["yields"]["per_run"]
    saved = _request("/api/tables/yields", {"per_run": per_run})
    assert saved.get("ok") is True and saved["yields"]["per_run"] == per_run, \
        f"产量表保存异常: {saved.get('error')}"
    # 确认接口：不存在的记录必须 404（不能凭空造账）
    try:
        _request("/api/history/confirm",
                 {"day": "1999-01-01", "time": "00:00:00", "mat_id": "coin",
                  "actual_gain": 1})
        raise AssertionError("不存在的记录应返回 404")
    except urllib.error.HTTPError as e:
        assert e.code == 404, f"预期 404，实际 {e.code}"


def _step_preview():
    data = _request("/api/preview?days=3")
    assert isinstance(data.get("days"), list) and len(data["days"]) == 3, \
        f"/api/preview 结构异常: {sorted(data.keys())}"
    assert "complete_on" in data and isinstance(data.get("warnings"), list), data


def _step_export_import_backups():
    exported = _request("/api/export")
    assert exported.get("schema") == 3 and isinstance(exported.get("settings"), dict) \
        and isinstance(exported.get("characters"), list) and "yields" in exported \
        and "char_library" in exported and "weapon_library" in exported \
        and "teams" in exported, \
        f"/api/export 结构异常: {sorted(exported.keys())}"
    # 回环导入：改一个库存值导回（守卫会恢复三个数据文件）
    payload = dict(exported)
    payload["inventory"] = {**payload.get("inventory", {}), "exp_char": 9}
    imported = _request("/api/import", payload)
    assert imported.get("ok") is True and imported.get("characters") is not None, imported
    boot = _request("/api/bootstrap")
    assert boot["state"]["inventory"].get("exp_char") == 9, "导入未生效"
    # 损坏导入必须 400 整体拒绝
    try:
        _request("/api/import", {"schema": 99})
        raise AssertionError("schema 不符应 400")
    except urllib.error.HTTPError as e:
        assert e.code == 400, f"预期 400，实际 {e.code}"
    backups = _request("/api/backups")
    assert isinstance(backups.get("backups"), list), backups


def _step_libraries_and_hunter_plan():
    libs = _request("/api/libraries",
                    {"char_library": ["浔", "灵可"], "weapon_library": ["远行者之声"]})
    assert libs.get("ok") is True and libs.get("char_library") == ["浔", "灵可"], libs
    plan = _request("/api/hunter/plan?boss=%E6%B5%B7%E5%9B%9A&fixed_runs=2")  # 海囚×2
    assert plan.get("mode") == "hunter" and isinstance(plan.get("steps"), list) \
        and plan.get("boss") == "海囚", f"/api/hunter/plan 结构异常: {sorted(plan.keys())}"


def _step_common_costs():
    data = _request("/api/common-costs")
    assert data.get("costs", {}).get("ascension_boss") == 86 \
        and isinstance(data.get("mapping"), list) and data["mapping"], \
        f"/api/common-costs 结构异常: {sorted(data.keys())}"
    saved = _request("/api/common-costs/save", {"ascension_boss": 86})
    assert saved.get("ok") is True and saved["costs"]["ascension_boss"] == 86, saved
    applied = _request("/api/common-costs/apply", {"name": "全部"})
    assert applied.get("ok") is True and isinstance(applied.get("applied"), list), applied


def _step_compat_report():
    url = BASE + "/api/compat-report"
    assert urllib.parse.urlsplit(url).hostname == "127.0.0.1", f"只允许本机: {url}"
    token = _panel_token()
    req = urllib.request.Request(url, headers={"X-Panel-Token": token} if token else {})
    with _OPENER.open(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
        assert resp.status == 200 and "兼容报告" in body and "首查清单" in body \
            and "兼容指纹" in body, f"报告内容异常: {body[:200]}"


def _step_stop():
    data = _request("/api/stop", {})
    assert "message" in data, f"/api/stop 响应异常: {data}"


def _step_auth_gate():
    """SEC-05 鉴权闸场景 A/B/C/E/F/G/H/I/J/K（D/L 重启场景在 main() 重启 harness）。

    A 带 token GET /api/bootstrap→200；B 无 token POST /api/settings→401+panel_auth；
    C 错 token→401+panel_auth；E evil Origin+合法 token→403（Host/Origin 闸）；
    F 合法 token + 空 ok-nte 结构目录→400 且 error 含「缺少必需结构」，随后
    bootstrap 断言 settings.oknte_root 未被污染（伪造载荷零执行：临时目录无
    python.exe，compat_check 纯文件检查；全程绝不 POST /api/start）；
    G 无 token GET→401；H 无 token POST /api/tajiduo/send-captcha 与 /api/tajiduo/
    logout→401（闸层拒绝、零短信零删凭证）；I evil Host GET /→403（静态 Host 闸，
    封 DNS 重绑定读注入 token 页）；J evil Host GET /avatars→403；K 回环 Host
    GET /avatars/不存在白名单名→404 而非 403（avatar 免 token 口径回归）。"""
    token = _panel_token()
    assert token, "鉴权场景前置：panel_token.json 应已生成（bootstrap 步已过）"

    # A: 带 token GET /api/bootstrap → 200
    data = _request("/api/bootstrap")
    assert isinstance(data.get("compat"), dict), "A: bootstrap 响应缺少 compat 字段"

    # B: 无 token POST /api/settings → 401 且 body.panel_auth==true（登记册验证法）
    status, raw = _raw_request("/api/settings", payload={})
    assert status == 401, f"B: 无 token POST /api/settings 预期 401，实际 {status}"
    assert _json_body(raw).get("panel_auth") is True, \
        f"B: 401 body 应含 panel_auth=true: {_json_body(raw)}"

    # C: 错 token（非真实长串）POST /api/settings → 401 + panel_auth
    status, raw = _raw_request("/api/settings", payload={}, token=WRONG_TOKEN)
    assert status == 401, f"C: 错 token 预期 401，实际 {status}"
    assert _json_body(raw).get("panel_auth") is True, \
        f"C: 401 body 应含 panel_auth=true: {_json_body(raw)}"

    # E: evil Origin + 合法 token POST /api/settings → 403（Host/Origin 闸先于 token 闸）
    status, raw = _raw_request("/api/settings", payload={}, token=token,
                               origin="https://evil.example.com")
    assert status == 403, f"E: evil Origin 预期 403（Host/Origin 闸），实际 {status}"

    # F: 合法 token + 伪造 oknte_root（空临时目录，无任何 ok-nte 结构）→ 400
    fake_roots = []
    with tempfile.TemporaryDirectory(prefix="smoke_fake_root_") as fake_root:
        fake_roots.append(fake_root)
        status, raw = _raw_request("/api/settings", payload={"oknte_root": fake_root},
                                   token=token)
        body = _json_body(raw)
        assert status == 400, f"F: 空 ok-nte 结构目录应 400，实际 {status}: {body}"
        assert "缺少必需结构" in (body.get("error") or ""), \
            f"F: 400 error 应含「缺少必需结构」: {body}"
    boot = _request("/api/bootstrap")
    assert boot["state"]["settings"]["oknte_root"] != fake_roots[0], \
        "F: 400 后 settings.oknte_root 被污染（state 原值必须不动）"

    # G: 无 token GET /api/bootstrap → 401（GET 闸覆盖）
    status, raw = _raw_request("/api/bootstrap")
    assert status == 401, f"G: 无 token GET /api/bootstrap 预期 401，实际 {status}"
    assert _json_body(raw).get("panel_auth") is True, "G: 401 body 应含 panel_auth=true"

    # H: 无 token POST /api/tajiduo/send-captcha 与 /api/tajiduo/logout → 401
    # （闸层拒绝：不触短信网关、不动凭证文件）
    for path in ("/api/tajiduo/send-captcha", "/api/tajiduo/logout"):
        status, raw = _raw_request(path, payload={})
        assert status == 401, f"H: {path} 无 token 预期 401，实际 {status}"
        assert _json_body(raw).get("panel_auth") is True, \
            f"H: {path} 401 body 应含 panel_auth=true"

    # I: evil Host GET / → 403（静态 Host 闸，封 DNS 重绑定读注入 token 页）
    status, raw = _raw_request("/", host="evil.example.com")
    assert status == 403, f"I: evil Host GET / 预期 403，实际 {status}"

    # J: evil Host GET /avatars/<白名单形文件名> → 403
    status, raw = _raw_request("/avatars/smoke-not-a-real-avatar-file.webp",
                               host="evil.example.com")
    assert status == 403, f"J: evil Host GET /avatars 预期 403，实际 {status}"

    # K: 回环 Host GET /avatars/不存在白名单名 → 404 而非 403（avatar 免 token 口径）
    status, raw = _raw_request("/avatars/definitely-no-such-avatar-0000.webp")
    assert status == 404, f"K: 回环 GET /avatars 不存在文件预期 404，实际 {status}"


def _step_elev_helper_lifecycle():
    """SEC-10（方案B）受限提权辅助进程冒烟（测试模式拉起，无 UAC；UAC 层列
    Manual）：ensure → ping 握手 → 错 token 拒绝 → shutdown 随退不留孤儿提权
    进程 → 安全事件审计落盘且绝不含令牌 → 端点文件清理。
    临时根放仓库 .tmp/sec10（AGENTS.md 卫生规则），finally 自清。"""
    from app import elev_client
    base = os.path.join(ROOT, ".tmp", "sec10")
    os.makedirs(base, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="smoke_elev_", dir=base)
    elev_client.set_test_spawn(True)
    try:
        root = os.path.join(tmp, "root")
        log_dir = os.path.join(tmp, "paneldata")
        os.makedirs(os.path.join(root, "data", "apps", "ok-nte", "python"))
        os.makedirs(log_dir)
        Path(root, "data", "apps", "ok-nte", "python",
             "python.exe").write_text("smoke-placeholder", encoding="utf-8")
        E = elev_client.ELEV
        assert E.ensure(root, log_dir=log_dir, game_process="HTGame.exe"), \
            E.reason()
        pid, port, token = E._pid, E._port, E._token
        assert elev_client._pid_alive(pid), "辅助进程应存活"
        resp = elev_client.request_once(port, token, "ping", timeout=8)
        assert resp.get("ok") is True, resp
        try:
            elev_client.request_once(port, "f" * 64, "ping", timeout=8)
            raise AssertionError("错 token 应被拒绝")
        except elev_client.ElevRejected:
            pass
        E.shutdown()
        deadline = time.time() + 6
        while time.time() < deadline and elev_client._pid_alive(pid):
            time.sleep(0.2)
        assert not elev_client._pid_alive(pid), "shutdown 后辅助进程应退出（不留孤儿）"
        audit = Path(log_dir, "elev_helper_audit.log")
        assert audit.is_file(), "安全事件审计未落盘"
        content = audit.read_text(encoding="utf-8")
        assert "reject" in content, "拒绝事件应进审计"
        assert token not in content and ("f" * 64) not in content, "令牌泄漏进审计"
        assert not Path(log_dir, "elev_helper_endpoint-%d.json" % pid).exists(), \
            "端点文件应随退出清理"
    finally:
        elev_client.set_test_spawn(False)
        elev_client.ELEV.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


def _spawn_panel(env):
    """拉起面板子进程（复用既有 Popen 形态：stdout 镜像进临时文件供失败回看）。"""
    out = tempfile.TemporaryFile()
    proc = subprocess.Popen(
        [PY, "-m", "app.main"], cwd=ROOT, env=env,
        stdout=out, stderr=subprocess.STDOUT,
    )
    return proc, out


def _kill_panel(proc):
    """杀干净面板子进程：taskkill /T /F 优先（连孙进程一起，重启场景旧面板
    必须彻底退出），兜底 terminate→kill（非 Windows/已退出）。"""
    if proc is not None and proc.poll() is None:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, creationflags=0x08000000)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


def _run_restart_scenarios(env):
    """SEC-05 重启失效场景 D/L：
    D 重启面板后旧 token GET /api/bootstrap→401 且 token 文件内容已变
      （断言变化、绝不打印值）；L 新 token（重读文件）GET /api/bootstrap→200。
    旧面板先 taskkill /T /F（main() 已 terminate 过第一代，这里对第二代收尾）；
    以 token 文件 (mtime,size) 变化为新面板就绪信号——裸连接成功不可靠
    （连接拒绝≠401，无法区分「未起」与「闸拒绝」）。"""
    old_token = _panel_token()
    assert old_token, "D/L 前置失败：panel_token.json 不存在（第一代面板未生成 token）"
    try:
        old_key = (os.stat(PANEL_TOKEN_FILE).st_mtime_ns,
                   os.stat(PANEL_TOKEN_FILE).st_size)
    except OSError:
        old_key = None
    proc, out = _spawn_panel(env)
    try:
        # 就绪信号：token 文件 (mtime,size) 变化（_ensure_panel_token 在 bind 前重写）
        deadline = time.time() + 30
        new_token = None
        while time.time() < deadline and new_token is None:
            try:
                st = os.stat(PANEL_TOKEN_FILE)
                key = (st.st_mtime_ns, st.st_size)
            except OSError:
                key = None
            if key is not None and key != old_key:
                new_token = _panel_token()
            if new_token is None:
                time.sleep(0.2)
        assert new_token, "D/L: 重启后 token 文件未变化（新面板未生成新 token）"
        assert new_token != old_token, "D/L: 重启后 token 内容应已变化（旧值绝不打印）"
        # D: 旧 token → 401（轮询容忍端口绑定前的连接拒绝）
        status = None
        deadline = time.time() + 30
        while status is None and time.time() < deadline:
            try:
                status, raw = _raw_request("/api/bootstrap", token=old_token)
            except OSError:  # 端口未绑定：连接拒绝，继续等
                time.sleep(0.3)
        assert status == 401, f"D: 重启后旧 token GET /api/bootstrap 预期 401，实际 {status}"
        # L: 新 token（重读文件）→ 200
        status, raw = _raw_request("/api/bootstrap", token=new_token)
        assert status == 200, f"L: 新 token GET /api/bootstrap 预期 200，实际 {status}"
    finally:
        _kill_panel(proc)
        out.close()


def _step_body_contract_srv01_srv05():
    """SRV-01 体契约 + SRV-05 teams 显式键（真实面板子进程全链路）：
    ① 原始非对象体 b"[1,2,3]"→400 且 error 可解析；② 空体 raw_body=b""（urllib
    自动补 CL:0）POST /api/stop→200（_NO_BODY_ROUTES 白名单不误伤）；③ 空体打
    读体路由→400「缺少请求体」；④ 伪造 Content-length: abc→400；⑤ 伪造
    Content-length: 3145728（实发 b"{}"）→413；⑥ 无 token+非法体→401 而非 400
    （非法体不得绕过鉴权，B2.1 契约）；⑦ 连续 5 次非法请求后 bootstrap→200
    （服务不退出）；⑧ teams 保存→缺键 400→队不丢（写盘均落 _StateFileGuard
    覆盖的 data/state.json，与 _step_characters 同口径）。"""
    token = _panel_token()
    assert token, "体契约场景前置：panel_token.json 应已生成（bootstrap 步已过）"

    # ①: 带 token 原始非对象体 → 400「请求体必须是 JSON 对象」
    status, raw = _raw_request("/api/characters", raw_body=b"[1,2,3]", token=token)
    assert status == 400 and _json_body(raw).get("error") == "请求体必须是 JSON 对象", \
        f"①: 预期 400+统一文案，实际 {status}, {raw[:120]!r}"

    # ②: 空体（urllib 自动补 Content-Length: 0）POST /api/stop → 200 白名单放行
    status, raw = _raw_request("/api/stop", raw_body=b"", token=token)
    assert status == 200 and "ok" in _json_body(raw), \
        f"②: 白名单路由空体预期 200，实际 {status}, {raw[:120]!r}"

    # ③: 空体打读体路由 → 400「缺少请求体」
    status, raw = _raw_request("/api/characters", raw_body=b"", token=token)
    assert status == 400 and _json_body(raw).get("error") == "缺少请求体", \
        f"③: 预期 400 缺少请求体，实际 {status}, {raw[:120]!r}"

    # ④: 伪造坏 Content-Length（headers 传 Content-length 键抑制自动补写）→ 400
    status, raw = _raw_request("/api/characters", raw_body=b"{}", token=token,
                               headers={"Content-length": "abc"})
    assert status == 400 and _json_body(raw).get("error") == "Content-Length 头无效", \
        f"④: 坏 CL 预期 400，实际 {status}, {raw[:120]!r}"

    # ⑤: 伪造超限声明 CL 3MB（实发 2 字节）→ 413（读体之前判）
    status, raw = _raw_request("/api/characters", raw_body=b"{}", token=token,
                               headers={"Content-length": "3145728"})
    assert status == 413 and "超过大小上限" in _json_body(raw).get("error", ""), \
        f"⑤: 超限声明 CL 预期 413，实际 {status}, {raw[:120]!r}"

    # ⑥: 无 token + 非法体 → 401 而非 400（鉴权先于体闸）
    status, raw = _raw_request("/api/characters", raw_body=b"[1,2,3]")
    assert status == 401 and _json_body(raw).get("panel_auth") is True, \
        f"⑥: 非法体不得绕过鉴权，预期 401+panel_auth，实际 {status}, {raw[:120]!r}"

    # ⑦: 连续 5 次非法请求后服务存活（不退出、不挂死）
    for _ in range(5):
        status, raw = _raw_request("/api/characters", raw_body=b"[1,2,3]", token=token)
        assert status == 400, f"⑦: 非法请求预期 400，实际 {status}, {raw[:120]!r}"
    boot = _request("/api/bootstrap")
    assert isinstance(boot.get("compat"), dict), "⑦: 非法请求后 bootstrap 必须仍可用"

    # ⑧: SRV-05 —— 保存→缺键 400→预置队不被清空
    saved = _request("/api/teams/save", {"teams": [{"name": "冒烟队",
                                                    "members": ["冒烟测试角色"]}]})
    assert saved.get("ok") is True, f"⑧: teams 保存预期 200，实际 {saved}"
    status, raw = _raw_request("/api/teams/save", raw_body=b"{}", token=token)
    assert status == 400 and _json_body(raw).get("error") == "teams 必须是数组", \
        f"⑧: 缺 teams 键预期 400，实际 {status}, {raw[:120]!r}"
    teams = _request("/api/teams").get("teams")
    assert isinstance(teams, list) and any(
        t.get("name") == "冒烟队" for t in teams), f"⑧: 冒烟队不得被缺键体清空: {teams}"


STEPS = [
    ("bootstrap 就绪且含 compat", _wait_bootstrap),
    ("SEC-05 鉴权闸场景 A-K（401/403/静态闸/root 指纹闸）", _step_auth_gate),
    ("SRV-01/05 体契约场景（非对象/空体/坏CL/超限/鉴权序/teams 键）",
     _step_body_contract_srv01_srv05),
    ("POST /api/characters 保存样例角色", _step_characters),
    ("POST /api/inventory 保存库存", _step_inventory),
    ("POST /api/plan 返回200且含 plan", _step_plan),
    ("POST /api/dry-run 返回计划与补丁差异", _step_dry_run),
    ("GET /api/preview 未来3天推演", _step_preview),
    ("产量校准三端点（建议/保存/确认404）", _step_yield_calibration_endpoints),
    ("角色库/武器库 + 追猎计划预览", _step_libraries_and_hunter_plan),
    ("通用材料表（读取/保存/应用）", _step_common_costs),
    ("GET /api/compat-report 兼容报告", _step_compat_report),
    ("导出/导入回环 + 备份列表", _step_export_import_backups),
    ("SEC-10 提权辅助进程生命周期（拉起/鉴权/随退/审计）", _step_elev_helper_lifecycle),
    ("POST /api/stop 可达", _step_stop),
]


def main():
    print("=== app.smoke_test 冒烟测试开始 ===")
    assert os.path.isfile(PY), f"未找到内嵌 Python: {PY}"
    with _StateFileGuard():
        env = dict(os.environ)
        env["OKNTE_PLANNER_PORT"] = str(PORT)
        env["OKNTE_PLANNER_NO_BROWSER"] = "1"
        proc, out = _spawn_panel(env)  # 复用既有 Popen 拉起形态
        try:
            passed = 0
            failed = 0
            for name, fn in STEPS:
                try:
                    fn()
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    print(f"FAIL {name}: {type(e).__name__}: {e}")
                    traceback.print_exc()
                else:
                    passed += 1
                    print(f"PASS {name}")
        finally:
            # 必须杀干净子进程
            _kill_panel(proc)
            out.seek(0)
            server_log = out.read().decode("utf-8", errors="replace")
            out.close()
        # ---- SEC-05 重启失效场景 D/L（重启 harness：复用既有 Popen 拉起代码）----
        try:
            _run_restart_scenarios(env)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL 鉴权重启场景 D/L: {type(e).__name__}: {e}")
            traceback.print_exc()
        else:
            passed += 1
            print("PASS 鉴权重启场景 D/L（旧 token 401 · token 文件已变 · 新 token 200）")
        total = len(STEPS) + 1
        print(f"=== 冒烟测试结果: 通过 {passed}/{total}（面板退出码 {proc.returncode}）===")
        if failed:
            print("--- 面板进程输出（末尾）---")
            print("\n".join(server_log.splitlines()[-15:]))
            sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
