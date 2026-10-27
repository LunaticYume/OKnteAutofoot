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
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request

try:  # Windows 控制台中文输出保险
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(ROOT, "data", "state.json")
EMBEDDED_PY = r"D:\GamingAssistant\ok-nte\data\apps\ok-nte\python\python.exe"
PY = EMBEDDED_PY if os.path.isfile(EMBEDDED_PY) else sys.executable
PORT = 18765
BASE = f"http://127.0.0.1:{PORT}"

SAMPLE_CHARACTER = {
    "name": "冒烟测试角色",
    "enabled": True,
    "mats": {"ability": 3, "arc": 2, "console": 1},
    "tracks": {},
    "extra_needs": {"exp_char": 30},
}


class _StateFileGuard:
    """备份 data/state.json 与 data/yields.json，退出时恢复（原本不存在则删除）。"""

    def __enter__(self):
        self._files = {}
        for path in (STATE_FILE,
                     os.path.join(os.path.dirname(STATE_FILE), "yields.json"),
                     os.path.join(os.path.dirname(STATE_FILE), "requirements.json")):
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
                with open(path, "wb") as f:
                    f.write(raw)
            elif os.path.isfile(path):
                os.remove(path)
        return False


def _request(path, payload=None, timeout=30):
    """GET（payload=None）/ POST JSON，非 2xx 抛 HTTPError。返回解析后的 JSON。"""
    if payload is None:
        req = urllib.request.Request(BASE + path, method="GET")
    else:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            BASE + path, data=body, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


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
    assert exported.get("schema") == 2 and isinstance(exported.get("settings"), dict) \
        and isinstance(exported.get("characters"), list) and "yields" in exported \
        and "char_library" in exported and "weapon_library" in exported, \
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


<<<<<<< HEAD
def _step_common_costs():
    data = _request("/api/common-costs")
    assert data.get("costs", {}).get("ascension_boss") == 86 \
        and isinstance(data.get("mapping"), list) and data["mapping"], \
        f"/api/common-costs 结构异常: {sorted(data.keys())}"
    saved = _request("/api/common-costs/save", {"ascension_boss": 86})
    assert saved.get("ok") is True and saved["costs"]["ascension_boss"] == 86, saved
    applied = _request("/api/common-costs/apply", {"name": "全部"})
    assert applied.get("ok") is True and isinstance(applied.get("applied"), list), applied


=======
>>>>>>> 53d44889c0d699d37fd730b0019e617da1b66bb4
def _step_stop():
    data = _request("/api/stop", {})
    assert "message" in data, f"/api/stop 响应异常: {data}"


STEPS = [
    ("bootstrap 就绪且含 compat", _wait_bootstrap),
    ("POST /api/characters 保存样例角色", _step_characters),
    ("POST /api/inventory 保存库存", _step_inventory),
    ("POST /api/plan 返回200且含 plan", _step_plan),
    ("POST /api/dry-run 返回计划与补丁差异", _step_dry_run),
    ("GET /api/preview 未来3天推演", _step_preview),
    ("产量校准三端点（建议/保存/确认404）", _step_yield_calibration_endpoints),
    ("角色库/武器库 + 追猎计划预览", _step_libraries_and_hunter_plan),
<<<<<<< HEAD
    ("通用材料表（读取/保存/应用）", _step_common_costs),
=======
>>>>>>> 53d44889c0d699d37fd730b0019e617da1b66bb4
    ("导出/导入回环 + 备份列表", _step_export_import_backups),
    ("POST /api/stop 可达", _step_stop),
]


def main():
    print("=== app.smoke_test 冒烟测试开始 ===")
    assert os.path.isfile(PY), f"未找到内嵌 Python: {PY}"
    with _StateFileGuard():
        env = dict(os.environ)
        env["OKNTE_PLANNER_PORT"] = str(PORT)
        env["OKNTE_PLANNER_NO_BROWSER"] = "1"
        out = tempfile.TemporaryFile()
        proc = subprocess.Popen(
            [PY, "-m", "app.main"], cwd=ROOT, env=env,
            stdout=out, stderr=subprocess.STDOUT,
        )
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
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
            out.seek(0)
            server_log = out.read().decode("utf-8", errors="replace")
            out.close()
        print(f"=== 冒烟测试结果: 通过 {passed}/{len(STEPS)}（面板退出码 {proc.returncode}）===")
        if failed:
            print("--- 面板进程输出（末尾）---")
            print("\n".join(server_log.splitlines()[-15:]))
            sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
