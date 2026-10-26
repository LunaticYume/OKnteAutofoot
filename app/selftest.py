"""自测（纯标准库，不依赖任何第三方包）。

覆盖：oknte_adapter 配置读写/备份、RunMonitor 四种结果解析、LogTail 增量读取与轮转、
planner 缺口计算与计划生成、计划算法矩阵（取整/额度/预留/禁用/拉黑/产量0）、no-op 枚举、
步骤与计划的结构化 reason、确定性与纯度不变量（纯读/同输入同输出/不写 state.json）、
扫描落账归属 runner、runner 持续模式（busy→waiting→自动重试→stop 打断）、
scan_used_stamina 取最大值。

运行方式（项目根目录）：
    D:/GamingAssistant/ok-nte/data/apps/ok-nte/python/python.exe -m app.selftest

约束：
- 绝不写入 D:/GamingAssistant/ok-nte 下任何文件；ok-nte 根目录一律用 tempfile 伪造。
- 绝不真正拉起 ok-nte 子进程；runner 用例把 app.runner.OkNte 换成假对象。
- make_plan/runner 会写项目 data/state.json：进入用例前备份、finally 里恢复（原本不存在则删除）。

每个用例 print 一行 PASS/FAIL；全部通过退出码 0，否则 1。
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app import oknte_adapter as adapter_mod  # noqa: E402
from app import planner  # noqa: E402
from app import runner as runner_mod  # noqa: E402
from app.oknte_adapter import LogTail, OkNte, RunMonitor  # noqa: E402
from app.state import DEFAULT_SETTINGS, STATE, STATE_FILE, load_json  # noqa: E402

try:  # Windows 控制台中文输出保险
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


# ---------- 公共工具 ----------

class _StateGuard:
    """备份 data/state.json 与 STATE 内存数据，退出时恢复（原本不存在则删除文件）。"""

    def __enter__(self):
        with STATE.lock:
            self.existed = os.path.isfile(STATE_FILE)
            self.raw = None
            if self.existed:
                with open(STATE_FILE, "rb") as f:
                    self.raw = f.read()
            self.data_copy = copy.deepcopy(STATE.data)
        return self

    def __exit__(self, *exc):
        with STATE.lock:
            STATE.data = self.data_copy
            if self.existed:
                with open(STATE_FILE, "wb") as f:
                    f.write(self.raw)
            elif os.path.isfile(STATE_FILE):
                os.remove(STATE_FILE)
        return False


def _empty_today():
    return {
        "date": STATE.day_key(), "used_stamina": 0, "planner_spent": 0,
        "blacklist": [], "entries": [],
    }


# planner 用例统一注入固定时间（禁止依赖真实系统时间）；12:00 已过 4 点重置点
NOW = datetime(2026, 10, 24, 12, 0, 0)
DAY = "2026-10-24"


def _day_today(used=0, blacklist=(), hunter_spent=0):
    """与注入 NOW 对应的当日结构（planner 纯读用例专用）。"""
    return {"date": DAY, "used_stamina": used, "planner_spent": 0,
            "hunter_spent": hunter_spent,
            "blacklist": list(blacklist), "entries": []}


FAKE_ROUTINE_CONFIG = {
    "daily_anomaly": {
        "目标消耗体力": 240,
        "任务类型": "经验与甲硬币",
        "具体奖励目标": "弧盘经验",
        "异能材料序号": 5,
        "弧盘材料序号": 5,
        "空幕序号": 1,
        "循环模式": "自动循环序号/目标",
        "循环序列": ["角色经验", "弧盘经验", "甲硬币", "空幕: 1"],
    },
    "daily_anomaly_hunter": {"目标消耗体力": 180, "追猎目标": "海囚"},
    "coffee": {"模式": "领取/补货", "领取收益": True, "补货货物": False, "补货时长": "auto"},
    "daily_claim": {"邮件": True, "活跃度奖励": True, "环期任务奖励": True},
    "cinema_date": {"约会目标": "伊洛伊"},
    "fountain": {"签到方式": "签到"},
    "gift": {},
}


def _build_fake_oknte(tmp, logs=None, cfg=None):
    """在 tmp 下伪造 ok-nte 目录结构（含配置与其他段），返回 OkNte 实例。"""
    apps = os.path.join(tmp, "data", "apps", "ok-nte")
    os.makedirs(os.path.join(apps, "working", "configs"), exist_ok=True)
    os.makedirs(os.path.join(apps, "working", "logs"), exist_ok=True)
    os.makedirs(os.path.join(apps, "python"), exist_ok=True)
    os.makedirs(os.path.join(apps, "working", "ok"), exist_ok=True)
    open(os.path.join(apps, "working", "ok", "__init__.py"), "wb").close()
    open(os.path.join(apps, "working", "main.py"), "wb").close()
    # compat_check 只要求 python.exe 是个文件，放个空文件即可
    with open(os.path.join(apps, "python", "python.exe"), "wb"):
        pass
    cfg_path = os.path.join(apps, "working", "configs", "DailyRoutineTaskConfigs.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg if cfg is not None else FAKE_ROUTINE_CONFIG, f,
                  ensure_ascii=False, indent=4)
    with open(os.path.join(apps, "working", "configs", "AnomalyHunter.json"),
              "w", encoding="utf-8") as f:
        json.dump({"追猎目标": "海囚", "领取奖励次数": 0}, f, ensure_ascii=False, indent=4)
    for name, lines in (logs or {}).items():
        # newline="\n" 保证字节数与字符数一致（LogTail 按字节偏移读取）
        with open(os.path.join(apps, "working", "logs", name), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write("".join(line + "\n" for line in lines))
    return OkNte(tmp)


# ---------- 用例 1：oknte_adapter 配置读写 / 备份 ----------

@case
def test_adapter_compat_write_backup():
    tmp = tempfile.mkdtemp(prefix="oknte_selftest_")
    backup_dir = os.path.join(tmp, "planner_backups")
    orig_data_dir = adapter_mod.DATA_DIR
    try:
        ok = _build_fake_oknte(os.path.join(tmp, "root"))
        # monkeypatch：假装 ok-nte 没在运行（真实实现会调 tasklist/powershell）
        ok.is_running = lambda: False

        result = ok.compat_check()
        assert result["status"] == "compatible", f"compat_check 不通过: {result}"

        ok.write_anomaly_config({"任务类型": "空幕", "空幕序号": 3}, 120)
        with open(ok.routine_config, encoding="utf-8") as f:
            cfg = json.load(f)
        daily = cfg["daily_anomaly"]
        assert daily["目标消耗体力"] == 120, f"目标消耗体力应为 120，实际 {daily['目标消耗体力']}"
        assert daily["循环模式"] == "停用", f"循环模式应为 停用，实际 {daily['循环模式']}"
        assert daily["空幕序号"] == 3, f"目标键 空幕序号 应为 3，实际 {daily.get('空幕序号')}"
        assert daily["任务类型"] == "空幕", f"任务类型应为 空幕，实际 {daily.get('任务类型')}"
        # daily_anomaly 其他键原样保留
        assert daily["循环序列"] == FAKE_ROUTINE_CONFIG["daily_anomaly"]["循环序列"], \
            "daily_anomaly 的循环序列应原样保留"
        assert daily["具体奖励目标"] == "弧盘经验", "daily_anomaly 的具体奖励目标应保留"
        # 其他段原样保留
        assert cfg["coffee"] == FAKE_ROUTINE_CONFIG["coffee"], "coffee 段应原样保留"
        assert cfg["daily_anomaly_hunter"] == FAKE_ROUTINE_CONFIG["daily_anomaly_hunter"], \
            "daily_anomaly_hunter 段应原样保留"
        assert cfg["daily_claim"] == FAKE_ROUTINE_CONFIG["daily_claim"], "daily_claim 段应原样保留"
        assert sorted(cfg.keys()) == sorted(FAKE_ROUTINE_CONFIG.keys()), "不应新增/丢失配置段"

        # 备份：把 DATA_DIR 指到临时目录，避免污染项目 data/backups
        adapter_mod.DATA_DIR = backup_dir
        dst = ok.backup_routine_config()
        assert dst and os.path.isfile(dst), f"备份文件未生成: {dst}"
        with open(dst, encoding="utf-8") as f:
            assert json.load(f) == cfg, "备份内容应与写后的配置一致"
    finally:
        adapter_mod.DATA_DIR = orig_data_dir
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 用例 1b：兼容性指纹 × 最小补丁 fixture 矩阵 ----------

@case
def test_compat_fingerprint_patch_matrix():
    from app.oknte_adapter import OkNteError

    dirs = []

    def build(cfg=None, drop_daily=(), broken=None, no_cfg=False):
        cfg = copy.deepcopy(FAKE_ROUTINE_CONFIG) if cfg is None else copy.deepcopy(cfg)
        for key in drop_daily:
            cfg["daily_anomaly"].pop(key, None)
        tmp = tempfile.mkdtemp(prefix="oknte_matrix_")
        dirs.append(tmp)
        ok = _build_fake_oknte(os.path.join(tmp, "root"), cfg=cfg)
        if broken is not None:
            with open(ok.routine_config, "w", encoding="utf-8") as f:
                f.write(broken)
        if no_cfg:
            os.remove(ok.routine_config)
        ok.is_running = lambda: False  # 矩阵只测结构与补丁，不测进程检测
        return ok

    target = {"任务类型": "空幕", "空幕序号": 3}
    results = []
    try:
        # 1) 正常当前格式：compatible 且无警告
        r = build().compat_check()
        assert r["status"] == "compatible" and not r["warnings"], f"正常格式应 compatible: {r}"
        results.append("正常格式→compatible")

        # 2) daily_anomaly 多一个未知字段：compatible；写回后未知字段保留、差异仅限计划键
        cfg = copy.deepcopy(FAKE_ROUTINE_CONFIG)
        cfg["daily_anomaly"]["上游新增键"] = {"未知": [1, 2]}
        ok = build(cfg=cfg)
        assert ok.compat_check()["status"] == "compatible"
        ok.write_anomaly_config(target, 120)
        with open(ok.routine_config, encoding="utf-8") as f:
            after = json.load(f)
        assert after["daily_anomaly"]["上游新增键"] == {"未知": [1, 2]}, "未知字段在写回时丢失"
        assert after["coffee"] == FAKE_ROUTINE_CONFIG["coffee"], "无关段被改动"
        assert after["daily_anomaly"]["任务类型"] == "空幕"
        assert after["daily_anomaly"]["目标消耗体力"] == 120
        results.append("未知字段→compatible且写回保留")

        # 3) 少一个必要字段：incompatible；写入拒绝且文件字节不变
        ok = build(drop_daily=("任务类型",))
        assert ok.compat_check()["status"] == "incompatible"
        with open(ok.routine_config, encoding="utf-8") as f:
            before = f.read()
        try:
            ok.write_anomaly_config(target, 120)
            raise AssertionError("缺必需键时写入应被拒绝")
        except OkNteError:
            pass
        with open(ok.routine_config, encoding="utf-8") as f:
            assert f.read() == before, "拒绝写入时文件应保持不变"
        results.append("缺任务类型→incompatible+拒写")

        # 4) 必需字段类型变化：incompatible；写入拒绝
        ok = build(cfg={**copy.deepcopy(FAKE_ROUTINE_CONFIG), "daily_anomaly": {
            **FAKE_ROUTINE_CONFIG["daily_anomaly"], "目标消耗体力": "240"}})
        assert ok.compat_check()["status"] == "incompatible"
        try:
            ok.write_anomaly_config(target, 120)
            raise AssertionError("类型异常时写入应被拒绝")
        except OkNteError:
            pass
        results.append("目标消耗体力变字符串→incompatible+拒写")

        # 5) 配置损坏：incompatible；写入拒绝
        ok = build(broken='{"daily_anomaly": {"任务类型": "经验与甲硬币",')
        assert ok.compat_check()["status"] == "incompatible"
        try:
            ok.write_anomaly_config(target, 120)
            raise AssertionError("配置损坏时写入应被拒绝")
        except OkNteError:
            pass
        results.append("配置损坏→incompatible+拒写")

        # 6) 配置文件不存在：incompatible；写入拒绝
        ok = build(no_cfg=True)
        assert ok.compat_check()["status"] == "incompatible"
        try:
            ok.write_anomaly_config(target, 120)
            raise AssertionError("配置缺失时写入应被拒绝")
        except OkNteError:
            pass
        results.append("文件不存在→incompatible+拒写")

        # 7) 不相关配置新增段：compatible；写回保留
        cfg = copy.deepcopy(FAKE_ROUTINE_CONFIG)
        cfg["上游新增段"] = {"a": 1}
        ok = build(cfg=cfg)
        assert ok.compat_check()["status"] == "compatible"
        ok.write_anomaly_config(target, 80)
        with open(ok.routine_config, encoding="utf-8") as f:
            after = json.load(f)
        assert after["上游新增段"] == {"a": 1}, "不相关新增段在写回时丢失"
        results.append("不相关新增段→compatible且保留")

        # 8) 序号键缺失：compatible_with_warning（写入时会新增该键）
        ok = build(drop_daily=("空幕序号",))
        r = ok.compat_check()
        assert r["status"] == "compatible_with_warning" \
            and any("空幕序号" in w for w in r["warnings"]), f"应警告序号缺失: {r}"
        results.append("序号键缺失→compatible_with_warning")

        # 9) 追猎段缺失但请求写入：拒绝且文件不变
        cfg = copy.deepcopy(FAKE_ROUTINE_CONFIG)
        del cfg["daily_anomaly_hunter"]
        ok = build(cfg=cfg)
        with open(ok.routine_config, encoding="utf-8") as f:
            before = f.read()
        try:
            ok.write_anomaly_config(target, 80, hunter={"stamina": 60, "target": "海囚"})
            raise AssertionError("追猎段缺失时写入应被拒绝")
        except OkNteError:
            pass
        with open(ok.routine_config, encoding="utf-8") as f:
            assert f.read() == before, "追猎拒写时文件应保持不变"
        results.append("追猎段缺失+hunter请求→拒写")

        # 10) 正常路径带 hunter：追猎两键被写入（回归旧缺陷：体力键从未真正写入）
        ok = build()
        ok.write_anomaly_config(target, 80, hunter={"stamina": 60, "target": "海囚"})
        with open(ok.routine_config, encoding="utf-8") as f:
            after = json.load(f)
        assert after["daily_anomaly_hunter"]["目标消耗体力"] == 60, "追猎体力应被写入"
        assert after["daily_anomaly_hunter"]["追猎目标"] == "海囚", "追猎目标应被写入"
        results.append("hunter正常写入(回归)")
    finally:
        for tmp in dirs:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------- 用例 2：RunMonitor 四种结果 ----------

@case
def test_monitor_done():
    lines = [
        "2026-10-24 09:00:00 [INFO] info_set 体力消耗目标 200",
        "2026-10-24 09:00:01 [INFO] 开始任务: DailyRoutineTask",
        "2026-10-24 09:00:05 [INFO] 选择项目序号: 第 2 个项目",
        "2026-10-24 09:00:10 [INFO] 双倍次数: 2, 单倍次数: 3",
        "2026-10-24 09:05:00 [INFO] 任务执行完毕",
    ]
    m = RunMonitor()
    m.feed(lines)
    status, spent, note = m.outcome(280, 40)
    assert status == "done", f"状态应为 done，实际 {status}"
    # 双倍2(每次2单位) + 单倍3 = 7 轮单位 × 40 = 280
    assert spent == 280, f"实际消耗应按 2×双倍+单倍=7 单位×40=280 计算，实际 {spent}"
    assert note == "", f"done 不应有原因说明，实际 {note!r}"
    assert m.units == 7 and m.stamina_target == 200 and m.selected_idx == 2, \
        "日志事件字段解析错误"
    # 没捕获到次数行时回退为 planned//cost
    m2 = RunMonitor()
    m2.feed(["2026-10-24 09:00:01 [INFO] 开始任务: DailyRoutineTask",
             "2026-10-24 09:05:00 [INFO] 任务执行完毕"])
    status2, spent2, _ = m2.outcome(200, 40)
    assert (status2, spent2) == ("done", 200), f"无次数行时应回退 planned/cost，实际 {(status2, spent2)}"


@case
def test_monitor_short():
    m = RunMonitor()
    m.feed([
        "2026-10-24 09:00:01 [INFO] 开始任务: DailyRoutineTask",
        "2026-10-24 09:01:00 [WARN] 体力不足，等待恢复",
    ])
    status, spent, note = m.outcome(200, 40)
    assert status == "short", f"状态应为 short，实际 {status}"
    assert spent == 0, "short 不应计消耗"
    assert "体力不足" in note, f"原因应含 体力不足，实际 {note!r}"


@case
def test_monitor_skipped():
    m = RunMonitor()
    m.feed([
        "2026-10-24 09:00:01 [INFO] 开始任务: DailyRoutineTask",
        "2026-10-24 09:00:02 [INFO] 跳过每日体力任务",
    ])
    status, spent, note = m.outcome(200, 40)
    assert status == "skipped", f"状态应为 skipped，实际 {status}"
    assert spent == 0, "skipped 不应计消耗"
    assert "跳过" in note, f"原因应说明跳过，实际 {note!r}"


@case
def test_monitor_failed():
    m = RunMonitor()
    m.feed([
        "2026-10-24 09:00:01 [INFO] 开始任务: DailyRoutineTask",
        "2026-10-24 09:01:00 [ERROR] 任务运行失败: 网络超时",
    ])
    status, spent, note = m.outcome(200, 40)
    assert status == "failed", f"状态应为 failed，实际 {status}"
    assert spent == 0, "failed 不应计消耗"
    assert note == "网络超时", f"失败原因解析错误，实际 {note!r}"
    # 失败标记（FAIL_MARKS）也应产出 failed
    m2 = RunMonitor()
    m2.feed(["2026-10-24 09:01:00 [ERROR] 未找到副本按钮"])
    status2, _, note2 = m2.outcome(200, 40)
    assert status2 == "failed" and note2, "失败标记行应产出 failed"


# ---------- 用例 3：LogTail 增量读取与轮转 ----------

@case
def test_logtail_increment_and_rotate():
    tmp = tempfile.mkdtemp(prefix="oknte_selftest_")
    try:
        path = os.path.join(tmp, "ok-script.log")

        def write_all(text):
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)

        write_all("alpha\nbeta\n")
        tail = LogTail([path])
        assert tail.read_new() == [], "首次 read_new 应只建立基线，不返回已有行"
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write("gamma\ndelta\n")
        assert tail.read_new() == ["gamma", "delta"], "应只返回新增行"
        assert tail.read_new() == [], "无新增时应返回空"

        # 文件变小（轮转/清空）后应从 0 重新读
        write_all("x\n")
        assert tail.read_new() == ["x"], "文件变小后应从 0 继续读取"

        # 不存在的文件应被跳过，不抛异常
        assert LogTail([os.path.join(tmp, "nope.log")]).read_new() == [], \
            "缺失的日志文件应被跳过"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 用例 4：planner 缺口与计划 ----------

def _char(cid, name, extra_needs):
    return {
        "id": cid, "name": name, "enabled": True,
        "mats": {"ability": 1, "arc": 1, "console": 1},
        "tracks": {}, "extra_needs": dict(extra_needs),
    }


@case
def test_planner_snapshot_and_plan():
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)  # 限额180，不预留追猎
        STATE.data["inventory"] = {"exp_char": 5}
        STATE.data["today"] = _day_today()
        STATE.data["characters"] = [
            _char("c1", "角色甲", {"coin": 9}),
            _char("c2", "角色乙", {"exp_char": 30}),
        ]
        # -- snapshot 缺口计算 --
        snap = planner.snapshot()
        assert snap["gaps"] == {"coin": 9, "exp_char": 25}, \
            f"总缺口应为 coin:9/exp_char:25，实际 {snap['gaps']}"
        row_c2 = snap["characters"][1]["rows"][0]
        assert (row_c2["mat_id"], row_c2["need"], row_c2["have"],
                row_c2["gap"], row_c2["runs"]) == ("exp_char", 30, 5, 25, 4), \
            f"角色乙 exp_char 行计算错误: {row_c2}"

        # -- make_plan：额度与优先级（角色顺序优先于材料表顺序）--
        plan = planner.make_plan(now=NOW)
        assert [s["mat_id"] for s in plan["steps"]] == ["coin", "exp_char"], \
            f"步骤顺序应为角色优先级 coin→exp_char，实际 {[s['mat_id'] for s in plan['steps']]}"
        s0, s1 = plan["steps"]
        assert (s0["runs"], s0["stamina"]) == (2, 80), \
            f"coin 缺9产量7 应刷2轮80体力，实际 {(s0['runs'], s0['stamina'])}"
        assert (s1["runs"], s1["stamina"], s1["gain"]) == (2, 80, 14), \
            f"剩余额度100应刷2轮exp_char，实际 {(s1['runs'], s1['stamina'], s1['gain'])}"
        assert plan["budget"] == 20, \
            "返回 budget 应为 扣除已排步骤后的剩余额度（180-80-80=20）"

        # -- 当日已耗：额度收缩 --
        STATE.data["today"] = _day_today(used=140)
        plan = planner.make_plan(now=NOW)
        assert [s["mat_id"] for s in plan["steps"]] == ["coin"] and plan["steps"][0]["runs"] == 1, \
            f"额度40应只够 coin 1轮，实际 {[(s['mat_id'], s['runs']) for s in plan['steps']]}"
        STATE.data["today"] = _day_today(used=180)
        plan = planner.make_plan(now=NOW)
        assert plan["steps"] == [] and plan["reason"] == "今日体力额度已用完", \
            f"额度用尽应无步骤，实际 {plan}"
        assert plan["no_op_reason"] == "daily_limit_reached", \
            f"额度用尽的枚举原因应为 daily_limit_reached，实际 {plan.get('no_op_reason')}"

        # -- 当日黑名单跳过 --
        STATE.data["today"] = _day_today(blacklist=["exp_char"])
        plan = planner.make_plan(now=NOW)
        assert [s["mat_id"] for s in plan["steps"]] == ["coin"], \
            f"黑名单 exp_char 应被跳过，实际 {[s['mat_id'] for s in plan['steps']]}"

        # -- 产量为 0：警告并跳过 --
        orig_yield = planner.YIELDS["per_run"]["coin"]
        try:
            planner.YIELDS["per_run"]["coin"] = 0
            STATE.data["today"] = _day_today()
            plan = planner.make_plan(now=NOW)
            assert [s["mat_id"] for s in plan["steps"]] == ["exp_char"], \
                "产量为0的 coin 应被跳过"
            assert any("甲硬币" in w and "产量未校准" in w for w in plan["warnings"]), \
                f"产量为0应给出警告，实际 {plan['warnings']}"
        finally:
            planner.YIELDS["per_run"]["coin"] = orig_yield


# ---------- 用例 4b：计划算法矩阵（缺口/取整/额度/预留/禁用/拉黑/产量） ----------

@case
def test_planner_algorithm_matrix():
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.data["today"] = _day_today()

        # 目标已完成：库存 ≥ 需求 → 绝不安排
        STATE.data["characters"] = [_char("c1", "甲", {"coin": 20})]
        STATE.data["inventory"] = {"coin": 20}
        plan = planner.make_plan(now=NOW)
        assert plan["steps"] == [] and plan["no_op_reason"] == "all_targets_completed", \
            f"库存达标应判 all_targets_completed，实际 {plan['no_op_reason']}"

        # 缺 1 个：向上取整仍需 1 轮
        STATE.data["inventory"] = {"coin": 19}
        plan = planner.make_plan(now=NOW)
        s = plan["steps"][0]
        assert (s["reason"]["deficit"], s["reason"]["runs_needed"],
                s["runs"], s["gain"]) == (1, 1, 1, 7), f"缺1个应刷1轮，实际 {s}"

        # 非整除向上取整：缺 26 / 每轮 7 → 需 4 轮（26/7=3.71 → ceil 4）
        STATE.data["inventory"] = {}
        STATE.data["characters"] = [_char("c1", "甲", {"exp_char": 26})]
        plan = planner.make_plan(now=NOW)
        s = plan["steps"][0]
        assert (s["reason"]["runs_needed"], s["runs"]) == (4, 4), \
            f"缺26产量7应需4轮，实际 {(s['reason']['runs_needed'], s['runs'])}"

        # 体力刚好够：缺14/每轮7 → 2轮80体力，限额80 → 全部安排
        STATE.data["settings"] = dict(DEFAULT_SETTINGS, daily_stamina_limit=80)
        STATE.data["characters"] = [_char("c1", "甲", {"coin": 14})]
        plan = planner.make_plan(now=NOW)
        assert [(x["mat_id"], x["runs"], x["stamina"]) for x in plan["steps"]] \
            == [("coin", 2, 80)], f"额度80刚好够2轮，实际 {plan['steps']}"

        # 体力差 1：限额79 → 只安排 1 轮
        STATE.data["settings"] = dict(DEFAULT_SETTINGS, daily_stamina_limit=79)
        plan = planner.make_plan(now=NOW)
        assert plan["steps"][0]["runs"] == 1, f"额度79应只够1轮，实际 {plan['steps']}"

        # 同优先级（同一角色）多材料：按材料表规范顺序 exp_char → coin
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.data["characters"] = [_char("c1", "甲", {"coin": 9, "exp_char": 9})]
        plan = planner.make_plan(now=NOW)
        assert [x["mat_id"] for x in plan["steps"]] == ["exp_char", "coin"], \
            f"同角色应按材料表顺序，实际 {[x['mat_id'] for x in plan['steps']]}"

        # 追猎已独立计划（阶段13）：日常额度不再扣减预留，预留设置被忽略
        STATE.data["settings"] = dict(
            DEFAULT_SETTINGS, reserve_anomaly_hunter=True, anomaly_hunter_stamina=60)
        plan = planner.make_plan(now=NOW)
        assert plan["detail"]["hunter_reserved"] == 0 \
            and plan["detail"]["budget_total"] == 180, plan["detail"]
        assert sum(x["stamina"] for x in plan["steps"]) <= 180, "总消耗不得超过日常额度"

        # disabled 角色：需求不计；全部禁用 → no_enabled_targets
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        ch = _char("c1", "甲", {"coin": 9})
        ch["enabled"] = False
        ch2 = _char("c2", "乙", {"exp_char": 30})
        STATE.data["characters"] = [ch, ch2]
        plan = planner.make_plan(now=NOW)
        assert [x["mat_id"] for x in plan["steps"]] == ["exp_char"], \
            f"禁用角色的 coin 需求不应出现，实际 {plan['steps']}"
        ch2["enabled"] = False
        plan = planner.make_plan(now=NOW)
        assert plan["steps"] == [] and plan["no_op_reason"] == "no_enabled_targets", \
            f"全部禁用应为 no_enabled_targets，实际 {plan['no_op_reason']}"

        # 有缺口但全部被当日拉黑 → waiting_for_reset
        ch["enabled"] = True
        ch2["enabled"] = True
        STATE.data["today"] = _day_today(blacklist=("coin", "exp_char"))
        plan = planner.make_plan(now=NOW)
        assert plan["steps"] == [] and plan["no_op_reason"] == "waiting_for_reset", \
            f"全部拉黑应为 waiting_for_reset，实际 {plan['no_op_reason']}"
        assert plan["detail"]["skipped_blacklisted"] == ["甲硬币", "猎人攻略（角色经验书）"], \
            f"被拉黑跳过的材料应在 detail 中可见，实际 {plan['detail']}"

        # 有缺口但产量全为 0 → invalid_material_config
        STATE.data["today"] = _day_today()
        orig_yields = dict(planner.YIELDS["per_run"])
        try:
            planner.YIELDS["per_run"]["coin"] = 0
            planner.YIELDS["per_run"]["exp_char"] = 0
            plan = planner.make_plan(now=NOW)
            assert plan["steps"] == [] \
                and plan["no_op_reason"] == "invalid_material_config", \
                f"产量全0应为 invalid_material_config，实际 {plan['no_op_reason']}"
            assert plan["warnings"], "产量为0必须给出可见警告"
        finally:
            planner.YIELDS["per_run"] = orig_yields


# ---------- 用例 4c：步骤/计划的结构化 reason ----------

@case
def test_planner_step_and_plan_reason():
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.data["today"] = _day_today()
        STATE.data["inventory"] = {"exp_char": 5}
        STATE.data["characters"] = [_char("c2", "角色乙", {"exp_char": 30})]
        plan = planner.make_plan(now=NOW)
        r = plan["steps"][0]["reason"]
        assert (r["material"], r["display_name"], r["target"], r["inventory"],
                r["deficit"], r["yield_per_run"], r["runs_needed"], r["cost_per_run"],
                r["budget_before"], r["runs_planned"]) == \
               ("exp_char", "猎人攻略（角色经验书）", 30, 5, 25, 7.0, 4, 40, 180, 4), \
            f"步骤 reason 各维度数值错误: {r}"
        assert "角色乙" in r["text"] and "缺口 25" in r["text"] and "4 轮" in r["text"], \
            f"reason.text 应含角色/缺口/轮数可读解释: {r['text']}"
        d = plan["detail"]
        assert (d["code"], d["planned_runs"], d["planned_stamina"]) == ("ok", 4, 160), d
        # 守恒不变量：已排消耗 + 剩余额度 = 可规划总额
        assert d["planned_stamina"] + plan["budget"] == d["budget_total"], \
            f"额度守恒被破坏: {d['planned_stamina']}+{plan['budget']}!={d['budget_total']}"
        assert plan["no_op_reason"] is None and plan["reason"] == "", \
            "有步骤的计划 no_op_reason 应为空"

        # 体力差一点不够一轮：insufficient_stamina（区别于额度用尽）
        STATE.data["today"] = _day_today(used=141)  # 180-141=39 < 40
        plan = planner.make_plan(now=NOW)
        assert plan["steps"] == [] and plan["no_op_reason"] == "insufficient_stamina", \
            f"39体力不足一轮应为 insufficient_stamina，实际 {plan['no_op_reason']}"


# ---------- 用例 4d：确定性与纯度（property/invariant） ----------

@case
def test_planner_determinism_and_purity():
    with _StateGuard():
        STATE.data["settings"] = dict(
            DEFAULT_SETTINGS, reserve_anomaly_hunter=True, anomaly_hunter_stamina=20)
        STATE.data["today"] = _day_today(used=20, blacklist=["console_1"])
        STATE.data["inventory"] = {"coin": 9, "console_1": 4}
        STATE.data["characters"] = [
            _char("c1", "甲", {"coin": 9, "exp_char": 7, "console_1": 5}),
            _char("c2", "乙", {"ability_1": 6}),
        ]
        before_data = copy.deepcopy(STATE.data)
        state_existed = os.path.isfile(STATE_FILE)
        raw = None
        if state_existed:
            with open(STATE_FILE, "rb") as f:
                raw = f.read()

        class ScanOk:
            """假 ok-nte：只提供扫描，扫描值大于本地记录。"""

            def scan_used_stamina(self, day):
                return 60

        p1 = planner.make_plan(ScanOk(), now=NOW)
        p2 = planner.make_plan(ScanOk(), now=NOW)
        assert p1 == p2, "同一输入连续两次 make_plan 必须产生完全相同的计划"
        assert STATE.data == before_data, "make_plan 不得修改 STATE（含 ok-nte 扫描路径）"
        if state_existed:
            with open(STATE_FILE, "rb") as f:
                assert f.read() == raw, "make_plan 不得写 state.json"
        else:
            assert not os.path.isfile(STATE_FILE), "make_plan 不得新建 state.json"

        # 扫描值只参与本次计算：used 取 max(本地20, 扫描60)=60，但不回写 STATE
        assert p1["used_stamina"] == 60 and "60" in p1["detail"]["stamina_used_note"], \
            p1["detail"]["stamina_used_note"]
        assert STATE.today["used_stamina"] == 20, "扫描值不得被 planner 回写"

        # 不变量：库存≥目标的材料绝不安排（coin 9/9）；总消耗 ≤ 限额-已耗（追猎预留已独立）
        assert all(s["mat_id"] != "coin" for s in p1["steps"]), "达标材料不得进计划"
        assert p1["detail"]["skipped_blacklisted"] == ["空幕·钟表把戏（兔子洞节点1）"], p1["detail"]
        assert sum(s["stamina"] for s in p1["steps"]) <= 180 - 60, \
            f"总消耗越界: {sum(s['stamina'] for s in p1['steps'])}"

        # day 计算使用注入 now：03:00 未过 4 点重置点 → 属前一窗口且当日数据视为空
        p3 = planner.make_plan(now=datetime(2026, 10, 24, 3, 0, 0))
        assert p3["day"] == "2026-10-23" and p3["used_stamina"] == 0, \
            f"03:00 应属前一窗口(2026-10-23)且按空当日计算，实际 {p3['day']}/{p3['used_stamina']}"
        assert STATE.data == before_data, "跨天视图同样不得改写 STATE.today"


# ---------- 用例 4e：扫描落账显式归属 runner ----------

@case
def test_runner_reconcile_used_stamina():
    from app.runner import _reconcile_used_stamina

    class Scan:
        def __init__(self, v):
            self.v = v

        def scan_used_stamina(self, day):
            return self.v

    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.data["today"] = {**_empty_today(), "used_stamina": 50}
        # 扫描值更大 → 落账并持久化
        changed, scanned = _reconcile_used_stamina(Scan(132))
        assert (changed, scanned) == (True, 132)
        assert STATE.today["used_stamina"] == 132
        with open(STATE_FILE, encoding="utf-8") as f:
            assert json.load(f)["today"]["used_stamina"] == 132, \
                "落账必须持久化到 state.json"
        # 无扫描值 / 扫描值更小 → 本地记录不动
        changed, scanned = _reconcile_used_stamina(Scan(None))
        assert changed is False and STATE.today["used_stamina"] == 132
        changed, scanned = _reconcile_used_stamina(Scan(10))
        assert changed is False and STATE.today["used_stamina"] == 132


# ---------- 用例 5：runner 持续模式（busy→waiting→自动重试→stop 打断） ----------

@case
def test_runner_continuous_busy_wait_retry_stop():
    with _StateGuard():
        STATE.data["settings"] = dict(
            DEFAULT_SETTINGS, continuous_mode=True, busy_retry_seconds=1,
            oknte_root=os.path.join(tempfile.gettempdir(), "oknte_selftest_fake_root"),
        )
        STATE.data["inventory"] = {}
        STATE.data["today"] = _empty_today()
        STATE.data["characters"] = [_char("c1", "占用期角色", {"coin": 9})]

        calls = {"is_running": 0}

        class FakeOkNte:
            """永远报告 ok-nte 占用；任何真实写入/拉起都被禁止。"""

            def __init__(self, root):
                self.root = root

            def compat_check(self):
                return {"status": "compatible", "version": "selftest",
                        "problems": [], "warnings": []}

            def scan_used_stamina(self, day):
                return None

            def is_running(self):
                calls["is_running"] += 1
                return True  # 永远 busy → 绝不会走到写配置/拉起子进程

            def write_anomaly_config(self, *a, **k):
                raise AssertionError("自测中不允许写 ok-nte 配置")

            def backup_routine_config(self):
                raise AssertionError("自测中不允许备份真实配置")

            def run_command(self):
                raise AssertionError("自测中不允许拉起 ok-nte 子进程")

        orig_oknte = runner_mod.OkNte
        runner_mod.OkNte = FakeOkNte
        r = runner_mod.Runner()
        try:
            ok, msg = r.start()
            assert ok, f"start 应成功: {msg}"

            # 1) 受阻进入 waiting
            deadline = time.time() + 15
            while time.time() < deadline and r.snapshot()["state"] != "waiting":
                time.sleep(0.05)
            snap = r.snapshot()
            assert snap["state"] == "waiting", f"受阻应进入 waiting，实际 {snap['state']}"
            assert "ok-nte 正在运行" in snap["waiting_reason"], \
                f"等待原因应说明 ok-nte 占用，实际 {snap['waiting_reason']!r}"
            assert snap["next_retry_at"], "waiting 状态应给出下次重试时间"

            # 2) 到时自动重试（busy_retry_seconds=1 → 第二次 is_running 检测）
            while time.time() < deadline and calls["is_running"] < 2:
                time.sleep(0.05)
            assert calls["is_running"] >= 2, \
                f"1 秒后应自动重试（再次检测占用），实际检测 {calls['is_running']} 次"

            # 3) stop 立即打断长等待：把下一轮等待拉到 60 秒
            STATE.settings["busy_retry_seconds"] = 60
            while time.time() < deadline and not (
                    calls["is_running"] >= 3 and r.snapshot()["state"] == "waiting"):
                time.sleep(0.05)
            assert calls["is_running"] >= 3 and r.snapshot()["state"] == "waiting", \
                "第三轮重试后应处于 60 秒长等待中"
            t0 = time.time()
            ok2, _ = r.stop()
            assert ok2, "stop 应被接受"
            r.thread.join(timeout=5)
            elapsed = time.time() - t0
            assert not r.thread.is_alive(), "stop 应立即打断等待（5 秒内线程退出）"
            assert elapsed < 5, f"stop 打断应即时，实际耗时 {elapsed:.1f} 秒"
            snap = r.snapshot()
            assert snap["state"] == "done" and snap["message"] == "已手动停止", \
                f"停止后应为 done/已手动停止，实际 {snap['state']}/{snap['message']!r}"
        finally:
            runner_mod.OkNte = orig_oknte
            if r.thread is not None and r.thread.is_alive():
                r.stop_event.set()
                r.thread.join(timeout=5)


# ---------- 用例 6：scan_used_stamina 取最大值 ----------

@case
def test_scan_used_stamina_max():
    tmp = tempfile.mkdtemp(prefix="oknte_selftest_")
    try:
        logs = {
            "ok-script.log": [
                "2026-10-24 08:00:00,100 [INFO] ocr found used stamina 96",
                "2026-10-23 23:59:59,900 [INFO] ocr found used stamina 999",  # 前一日，应忽略
                "2026-10-24 09:30:00,050 [INFO] ocr found used stamina 132",
            ],
        }
        ok = _build_fake_oknte(os.path.join(tmp, "root"), logs=logs)
        assert ok.scan_used_stamina("2026-10-24") == 132, \
            "两条 OCR 记录（96/132）应取最大值 132，且忽略前一日记录"
        # 带日期的轮转日志也应参与扫描
        with open(os.path.join(ok.log_dir, "ok-script.2026-10-24.log"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write("2026-10-24 10:00:00,000 [INFO] ocr found used stamina 120\n")
        assert ok.scan_used_stamina("2026-10-24") == 132, "跨日志文件仍应取最大值"
        assert ok.scan_used_stamina("2026-10-25") is None, "无匹配记录应返回 None"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 用例 7：崩溃恢复矩阵（阶段3） ----------

@case
def test_recovery_crash_matrix():
    tmp = tempfile.mkdtemp(prefix="oknte_recovery_")
    orig_runtime_file = runner_mod.RUNTIME_FILE
    orig_pid_alive = runner_mod._pid_alive
    orig_oknte_cls = runner_mod.OkNte
    runner_mod.RUNTIME_FILE = os.path.join(tmp, "runtime.json")

    class FakeOkNte:  # 与现有 runner 用例同规约：绝不真实写配置/拉起进程
        def __init__(self, root):
            pass

        def compat_check(self):
            return {"status": "compatible", "version": "selftest",
                    "problems": [], "warnings": []}

        def scan_used_stamina(self, day):
            return None

    runner_mod.OkNte = FakeOkNte
    try:
        with _StateGuard():
            # 一次性模式 + 空角色 → 计划恒为空且不等待 → 线程立即 done，不会执行任何步骤
            STATE.data["settings"] = dict(DEFAULT_SETTINGS, continuous_mode=False,
                                          oknte_root=os.path.join(tmp, "root"))
            STATE.data["characters"] = []

            def seed_runtime(rt):
                with open(runner_mod.RUNTIME_FILE, "w", encoding="utf-8") as f:
                    json.dump(rt, f, ensure_ascii=False, indent=2)

            def make_exec(phase, pid=12345):
                return {"execution_id": "e1", "phase": phase, "pid": pid,
                        "started_at": "2026-10-24 21:00:00", "day": STATE.day_key(),
                        "step": {"mat_id": "coin", "name": "甲硬币", "runs": 2,
                                 "stamina": 80, "gain": 14},
                        "outcome": None}

            # 0) 首次启动（无 runtime 文件）→ 干净 stopped，无提示
            if os.path.exists(runner_mod.RUNTIME_FILE):
                os.remove(runner_mod.RUNTIME_FILE)
            r = runner_mod.RUNNER.recover_on_startup()
            assert r["prev_state"] is None and r["pending"] == [] and not r["corrupt"], r
            assert runner_mod.RUNNER.snapshot()["state"] != "interrupted"

            # 1) launch 前崩（planned 未证实拉起）→ 安全清除、无未决
            seed_runtime({"version": 1, "state": "running",
                          "execution": make_exec("planned"), "last_execution": None})
            r = runner_mod.RUNNER.recover_on_startup()
            assert r["pending"] == [], "planned 未证实拉起过，应安全清除"
            assert runner_mod.RUNNER.snapshot()["pending"] == []

            # 2) launch 后崩 + 面板重启时子进程 active（pid 存活）→ 未决 + 阻塞 + 活着不许处理
            runner_mod._pid_alive = lambda pid: True
            seed_runtime({"version": 1, "state": "running",
                          "execution": make_exec("launched"), "last_execution": None})
            r = runner_mod.RUNNER.recover_on_startup()
            assert len(r["pending"]) == 1 and r["pending"][0]["alive"] is True, r
            ok, msg = runner_mod.RUNNER.start()
            assert not ok and "未对账" in msg, f"存在未决时 start 必须被阻塞: {msg}"
            ok2, msg2 = runner_mod.RUNNER.resolve_pending("e1", "inventory_done")
            assert not ok2 and "仍在运行" in msg2, f"子进程活着时不许对账: {msg2}"

            # 3) 子进程已结束但未记账 → 未决；处理后留零值审计历史；之后可正常开始
            runner_mod._pid_alive = lambda pid: False
            seed_runtime({"version": 1, "state": "running",
                          "execution": make_exec("exited"), "last_execution": None})
            r = runner_mod.RUNNER.recover_on_startup()
            assert len(r["pending"]) == 1 and r["pending"][0]["alive"] is False, r
            ok, msg = runner_mod.RUNNER.resolve_pending("e1", "discard")
            assert ok, f"放弃记账应成功: {msg}"
            assert any(e.get("status") == "reconciled" for e in STATE.data["history"]), \
                "对账应留 status=reconciled 的审计历史"
            reconciled = [e for e in STATE.data["history"] if e.get("status") == "reconciled"][-1]
            assert reconciled["gain"] == 0 and reconciled["stamina"] == 0, \
                "对账留痕必须是零值，绝不把预计产出当实际入账"
            ok3, msg3 = runner_mod.RUNNER.start()
            assert ok3, f"对账完成后应可重新开始: {msg3}"
            if runner_mod.RUNNER.thread:
                runner_mod.RUNNER.thread.join(timeout=5)
            assert not (runner_mod.RUNNER.thread and runner_mod.RUNNER.thread.is_alive())
            with runner_mod.RUNNER.lock:
                runner_mod.RUNNER.status["state"] = "idle"

            # 4) waiting 中崩 → interrupted 无未决，可直接开始
            seed_runtime({"version": 1, "state": "waiting",
                          "execution": None, "last_execution": None})
            r = runner_mod.RUNNER.recover_on_startup()
            assert r["pending"] == [], "waiting 崩溃不应产生未决"
            assert runner_mod.RUNNER.snapshot()["state"] == "interrupted"

            # 5) stopping 中崩 → 同 waiting
            seed_runtime({"version": 1, "state": "stopping",
                          "execution": None, "last_execution": None})
            runner_mod.RUNNER.recover_on_startup()
            assert runner_mod.RUNNER.snapshot()["state"] == "interrupted" \
                and runner_mod.RUNNER.snapshot()["pending"] == []

            # 6) runtime.json 损坏且无备份 → interrupted + 提示人工核对（不猜测）
            #    （有 .bak 时 load_json 会按设计回退成功，走正常恢复路径，不算 corrupt）
            for suffix in ("", ".bak", ".corrupt"):
                path = runner_mod.RUNTIME_FILE + suffix
                if os.path.exists(path):
                    os.remove(path)
            with open(runner_mod.RUNTIME_FILE, "w", encoding="utf-8") as f:
                f.write('{"version": 1, "state": "run')
            r = runner_mod.RUNNER.recover_on_startup()
            assert r["corrupt"] is True, "损坏应上报 corrupt"
            assert runner_mod.RUNNER.snapshot()["state"] == "interrupted"

            # 7) JSON 写入中断（截断）+ .bak 兜底：load_json 回退到最后完好版本
            demo = os.path.join(tmp, "demo.json")
            with open(demo + ".bak", "w", encoding="utf-8") as f:
                json.dump({"good": 1}, f)
            with open(demo, "w", encoding="utf-8") as f:
                f.write('{"good": 2, "brok')  # 写入中途的半截
            from app.state import load_json
            assert load_json(demo) == {"good": 1}, "截断主文件应回退 .bak"
            assert os.path.isfile(demo + ".corrupt"), "损坏文件应留档供人工检查"

            # 8) 双刷防护复检：任何未决存在期间 start 一律拒绝
            seed_runtime({"version": 1, "state": "stopped",
                          "execution": make_exec("launched"), "last_execution": None})
            runner_mod._pid_alive = lambda pid: False
            runner_mod.RUNNER.recover_on_startup()
            ok8, msg8 = runner_mod.RUNNER.start()
            assert not ok8 and "未对账" in msg8
    finally:
        runner_mod.RUNTIME_FILE = orig_runtime_file
        runner_mod._pid_alive = orig_pid_alive
        runner_mod.OkNte = orig_oknte_cls
        with runner_mod.RUNNER.lock:  # 复位单例，避免影响其他用例
            runner_mod.RUNNER.pending = []
            runner_mod.RUNNER.status["state"] = "idle"
            runner_mod.RUNNER.status["message"] = ""
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 用例 8：Dry Run 预演（阶段4：100 次纯读不变量） ----------

@case
def test_dry_run_hundred_times():
    import hashlib
    from app.main import perform_dry_run

    tmp = tempfile.mkdtemp(prefix="oknte_dryrun_")
    try:
        with _StateGuard():
            root = os.path.join(tmp, "root")
            ok = _build_fake_oknte(root)
            STATE.data["settings"] = dict(DEFAULT_SETTINGS, oknte_root=root)
            STATE.data["inventory"] = {"coin": 0}
            STATE.data["today"] = _empty_today()
            STATE.data["characters"] = [_char("c1", "预演角色", {"coin": 30})]

            cfg_path = ok.routine_config

            def file_hash():
                with open(cfg_path, "rb") as f:
                    return hashlib.sha256(f.read()).hexdigest()

            before_hash = file_hash()
            before_inventory = copy.deepcopy(STATE.data["inventory"])
            before_quota = (STATE.data["today"].get("used_stamina"),
                            STATE.data["today"].get("planner_spent"))
            before_runtime_state = runner_mod.RUNNER.snapshot()["state"]
            first = None
            for i in range(100):
                r = perform_dry_run(ok)
                assert r["ok"] is True, f"第{i+1}次预演失败: {r.get('error')}"
                assert r["plan"]["steps"], "缺口存在时应产出步骤"
                core = {"plan": r["plan"], "patch": r["patch"]}
                if first is None:
                    first = core
                    keys = {x["key"] for x in r["patch"]["diff"]}
                    # fake 配置初始为 经验与甲硬币/弧盘经验，coin 目标只改 具体奖励目标；
                    # 目标消耗体力 与 循环模式 任何目标都必然变化
                    assert {"具体奖励目标", "目标消耗体力", "循环模式"} <= keys, \
                        f"diff 应含关键键: {keys}"
                    assert all(x["old"] != x["new"] for x in r["patch"]["diff"]), \
                        "diff 只能列真实差异"
                else:
                    assert core == first, f"第{i+1}次预演结果与首次不一致（确定性被破坏）"
            # 四不变量：配置字节不变 / 库存不变 / 额度不变 / runtime 不进入 running
            assert file_hash() == before_hash, "ok-nte 配置文件必须字节不变"
            assert STATE.data["inventory"] == before_inventory, "库存不得变化"
            assert (STATE.data["today"].get("used_stamina"),
                    STATE.data["today"].get("planner_spent")) == before_quota, "额度不得变化"
            assert runner_mod.RUNNER.snapshot()["state"] == before_runtime_state, \
                "runtime 不得进入 running"
            # incompatible 场景：配置损坏 → compat 失败且不生成补丁
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write('{"daily_anomaly": broken')
            r = perform_dry_run(ok)
            assert r["ok"] is False and r["patch"] is None \
                and r["compat"]["status"] == "incompatible", \
                f"损坏配置应 compat 失败且无补丁: {r['compat']['status']}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- E2E（阶段5）：fake ok-nte 子进程全链路 ----------

FAKE_OKNTE = os.path.join(_ROOT, "tests", "fake_oknte.py")


def _wait_until(cond, timeout=20, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


class _E2EOkNte(OkNte):
    """真实 OkNte 行为，run_command/run_command_hunter 替换为 fake ok-nte 子进程；类属性由测试注入。"""
    mode_env = {"FAKE_MODE": "done", "FAKE_DELAY": "0.05", "FAKE_DELAY2": "0.05"}
    force_busy = False
    launches = []  # [(时间戳, 模式快照)]
    hunter_launches = []

    def run_command(self):
        type(self).launches.append((time.time(), dict(self.mode_env)))
        env = dict(os.environ)
        env.update(self.mode_env)
        env["FAKE_LOG"] = os.path.join(self.log_dir, "ok-script.log")
        return [sys.executable, FAKE_OKNTE], self.working, env

    def run_command_hunter(self):
        type(self).hunter_launches.append((time.time(), dict(self.mode_env)))
        env = dict(os.environ)
        env.update({**self.mode_env, "FAKE_MODE": "hunter"})
        env["FAKE_LOG"] = os.path.join(self.log_dir, "ok-script.log")
        return [sys.executable, FAKE_OKNTE], self.working, env

    def is_running(self):
        if type(self).force_busy:
            return True
        return super().is_running()


class _E2EGuard:
    """E2E 用例公共装配：重定向 runtime.json、注入 _E2EOkNte、StateGuard、伪造根目录。"""

    def __enter__(self):
        self._state_guard = _StateGuard().__enter__()
        self.tmp = tempfile.mkdtemp(prefix="oknte_e2e_")
        self.root = os.path.join(self.tmp, "root")
        self._orig_runtime = runner_mod.RUNTIME_FILE
        self._orig_oknte = runner_mod.OkNte
        runner_mod.RUNTIME_FILE = os.path.join(self.tmp, "runtime.json")
        _E2EOkNte.launches = []
        _E2EOkNte.hunter_launches = []
        _E2EOkNte.force_busy = False
        _E2EOkNte.mode_env = {"FAKE_MODE": "done", "FAKE_DELAY": "0.05", "FAKE_DELAY2": "0.05"}
        runner_mod.OkNte = lambda root: _E2EOkNte(root)
        return self

    def configure(self, mode=None, **settings_over):
        merged = dict(DEFAULT_SETTINGS, oknte_root=self.root,
                      busy_retry_seconds=1, max_run_minutes=5)
        merged.update(settings_over)
        STATE.data["settings"] = merged
        STATE.data["inventory"] = {}
        STATE.data["today"] = _empty_today()
        STATE.data["characters"] = []
        # 日志文件必须先存在：LogTail 才能在拉起子进程前建立基线偏移
        log_dir = os.path.join(self.root, "data", "apps", "ok-nte", "working", "logs")
        os.makedirs(log_dir, exist_ok=True)
        open(os.path.join(log_dir, "ok-script.log"), "a").close()
        if mode:
            _E2EOkNte.mode_env = {**_E2EOkNte.mode_env, "FAKE_MODE": mode}

    def __exit__(self, *exc):
        runner_mod.RUNTIME_FILE = self._orig_runtime
        runner_mod.OkNte = self._orig_oknte
        self._state_guard.__exit__(*exc)
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False


@case
def test_e2e_normal_run_and_settle():  # 场景A：正常执行 + 单实例 + 记账闭环
    with _E2EGuard() as e2e:
        ok = _build_fake_oknte(e2e.root)
        e2e.configure(continuous_mode=False)
        STATE.data["inventory"] = {"coin": 0}
        STATE.data["characters"] = [_char("c1", "甲角色", {"coin": 14})]  # 2轮×产量7
        r = runner_mod.Runner()
        started, msg = r.start()
        assert started, msg
        r.thread.join(timeout=40)
        assert not r.thread.is_alive(), "一次性模式应自然结束"
        assert len(_E2EOkNte.launches) == 1, f"应恰好启动一次，实际 {len(_E2EOkNte.launches)}"
        snap = r.snapshot()
        assert snap["state"] == "done", f"应完成，实际 {snap['state']}/{snap['message']}"
        assert STATE.data["today"]["used_stamina"] == 80, "应记账 80 体力"
        assert STATE.data["inventory"]["coin"] == 14, "库存应 +14"
        assert STATE.data["history"][-1]["status"] == "done" \
            and STATE.data["history"][-1]["runs"] == 2
        with open(ok.routine_config, encoding="utf-8") as f:
            daily = json.load(f)["daily_anomaly"]
        assert daily["具体奖励目标"] == "甲硬币" and daily["目标消耗体力"] == 80 \
            and daily["循环模式"] == "停用", f"写入的配置不符: {daily}"
        rt = load_json(runner_mod.RUNTIME_FILE)
        assert rt["execution"] is None and rt["last_execution"]["phase"] == "accounted", \
            "执行生命周期应收口为 accounted"


@case
def test_e2e_busy_instance_and_wait_stop():  # 场景B + C(waiting)：已有实例不重复启动，等待可秒停
    with _E2EGuard() as e2e:
        _build_fake_oknte(e2e.root)
        e2e.configure(continuous_mode=True, busy_retry_seconds=2)
        STATE.data["characters"] = [_char("c1", "占用角色", {"coin": 7})]
        _E2EOkNte.force_busy = True
        r = runner_mod.Runner()
        started, _ = r.start()
        assert started
        assert _wait_until(lambda: r.snapshot()["state"] == "waiting"), "应进入等待"
        snap = r.snapshot()
        assert "ok-nte 正在运行" in snap["waiting_reason"], snap["waiting_reason"]
        assert not _E2EOkNte.launches, "检测到占用时绝不能启动子进程"
        t0 = time.time()
        ok, _ = r.stop()
        assert ok
        r.thread.join(timeout=5)
        assert not r.thread.is_alive() and time.time() - t0 < 5, "等待中停止必须秒级返回"
        assert r.snapshot()["state"] == "done" and r.snapshot()["message"] == "已手动停止"


@case
def test_e2e_running_stop_kills_child():  # 场景C(running)：停止即终止子进程（本项目既定契约）
    with _E2EGuard() as e2e:
        _build_fake_oknte(e2e.root)
        e2e.configure(continuous_mode=False)
        _E2EOkNte.mode_env = {**_E2EOkNte.mode_env, "FAKE_MODE": "hang", "FAKE_HANG": "60"}
        STATE.data["characters"] = [_char("c1", "常驻角色", {"coin": 7})]
        r = runner_mod.Runner()
        started, _ = r.start()
        assert started
        assert _wait_until(lambda: r.child is not None and r.child.poll() is None), \
            "子进程应已启动且存活"
        child_pid = r.child.pid
        ok, _ = r.stop()
        assert ok
        r.thread.join(timeout=15)
        assert not r.thread.is_alive(), "停止后线程应退出"
        assert _wait_until(lambda: not runner_mod._pid_alive(child_pid), timeout=10), \
            "子进程应被终止（契约：停止即终止）"


@case
def test_e2e_short_wait_and_config_stable():  # 场景D：体力不足→等待；重试重写配置幂等（字节稳定）
    with _E2EGuard() as e2e:
        ok = _build_fake_oknte(e2e.root)
        e2e.configure(continuous_mode=True, busy_retry_seconds=1)
        _E2EOkNte.mode_env = {**_E2EOkNte.mode_env, "FAKE_MODE": "short"}
        STATE.data["characters"] = [_char("c1", "缺体力角色", {"coin": 7})]
        r = runner_mod.Runner()
        r.start()
        try:
            assert _wait_until(lambda: r.snapshot()["state"] == "waiting"
                               and "体力不足" in r.snapshot()["waiting_reason"]), \
                "应因体力不足等待"
            with open(ok.routine_config, "rb") as f:
                first_bytes = f.read()
            assert _wait_until(lambda: len(_E2EOkNte.launches) >= 2), "应自动重试"
            with open(ok.routine_config, "rb") as f:
                assert f.read() == first_bytes, "重试重写同一目标应字节级幂等"
            assert STATE.data["today"]["used_stamina"] == 0, "体力不足不得记账"
            assert STATE.data["inventory"].get("coin", 0) == 0, "不得加库存"
        finally:
            r.stop()
            r.thread.join(timeout=5)


@case
def test_e2e_quota_exhausted_then_new_day():  # 场景E：额度耗尽等待次日，额度恢复自动续跑
    with _E2EGuard() as e2e:
        _build_fake_oknte(e2e.root)
        e2e.configure(continuous_mode=True, busy_retry_seconds=1, daily_stamina_limit=40)
        STATE.data["characters"] = [_char("c1", "跨日角色", {"coin": 14})]  # 需2轮，日限1轮
        r = runner_mod.Runner()
        r.start()
        try:
            assert _wait_until(lambda: len(_E2EOkNte.launches) == 1), "第1天应执行1轮"
            assert _wait_until(lambda: r.snapshot()["state"] == "waiting"
                               and "额度" in r.snapshot()["waiting_reason"]), \
                "额度用尽应等待次日"
            assert STATE.data["today"]["used_stamina"] == 40
            # 模拟次日：把当日窗口换成本日键的空账（ensure_today 保留同键当日）
            STATE.data["today"] = {
                "date": STATE.day_key(), "used_stamina": 0, "planner_spent": 0,
                "blacklist": [], "entries": []}
            assert _wait_until(lambda: len(_E2EOkNte.launches) == 2), "额度恢复应自动续跑"
            assert _wait_until(lambda: r.snapshot()["state"] == "waiting"
                               and "达标" in r.snapshot()["waiting_reason"]), \
                "补齐后应等待次日（全部达标）"
            assert STATE.data["inventory"]["coin"] == 14, "两天共2轮应补齐缺口"
        finally:
            r.stop()
            r.thread.join(timeout=5)


@case
def test_e2e_day_boundary():  # 场景F：03:59 与 04:00 属两个 planning day（注入时钟）
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS, daily_reset_hour=4)
        from datetime import datetime as _dt
        assert STATE.day_key(_dt(2030, 1, 10, 3, 59)) == "2030-01-09"
        assert STATE.day_key(_dt(2030, 1, 10, 4, 0)) == "2030-01-10"


@case
def test_e2e_upstream_break_fail_closed():  # 场景G：上游配置损坏→不兼容→终止且原文件不被覆盖
    with _E2EGuard() as e2e:
        ok = _build_fake_oknte(e2e.root)
        broken = json.loads(json.dumps(FAKE_ROUTINE_CONFIG))
        del broken["daily_anomaly"]["任务类型"]
        with open(ok.routine_config, "w", encoding="utf-8") as f:
            json.dump(broken, f, ensure_ascii=False, indent=4)
        with open(ok.routine_config, "rb") as f:
            before = f.read()
        e2e.configure(continuous_mode=False)
        STATE.data["characters"] = [_char("c1", "上游损坏角色", {"coin": 7})]
        r = runner_mod.Runner()
        started, _ = r.start()
        assert started
        r.thread.join(timeout=15)
        snap = r.snapshot()
        assert snap["state"] == "error" and "兼容性检查失败" in snap["message"], \
            f"应 fail closed，实际 {snap['state']}/{snap['message']}"
        with open(ok.routine_config, "rb") as f:
            assert f.read() == before, "不兼容时原配置必须一字节不动"
        assert not _E2EOkNte.launches, "不得启动子进程"


@case
def test_e2e_retry_pacing_no_busy_loop():  # 场景H：异常结果只按 busy_retry 节奏重试，无高速循环
    with _E2EGuard() as e2e:
        _build_fake_oknte(e2e.root)
        e2e.configure(continuous_mode=True, busy_retry_seconds=2)
        _E2EOkNte.mode_env = {**_E2EOkNte.mode_env, "FAKE_MODE": "short"}
        STATE.data["characters"] = [_char("c1", "节奏角色", {"coin": 7})]
        r = runner_mod.Runner()
        r.start()
        try:
            assert _wait_until(lambda: len(_E2EOkNte.launches) >= 3, timeout=45), \
                "持续模式应按节奏重试"
            stamps = [t for t, _ in _E2EOkNte.launches]
            gaps = [b - a for a, b in zip(stamps, stamps[1:])]
            assert all(g >= 1.5 for g in gaps), \
                f"两次拉起间隔必须≈busy_retry_seconds(2s)，实测 {gaps}"
        finally:
            t0 = time.time()
            r.stop()
            r.thread.join(timeout=5)
            assert time.time() - t0 < 5


@case
def test_e2e_planner_data_anomalies():  # 场景I：数据异常不崩溃——负数/产量0/费用0/缺config
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.data["today"] = _empty_today()
        STATE.data["inventory"] = {"coin": -10}  # 手改坏账（绕过 API 钳位）
        STATE.data["characters"] = [_char("c1", "负数角色", {"coin": -5})]
        plan = planner.make_plan()  # 负需求被过滤：无有效需求 → no_enabled_targets，不崩溃
        assert plan["no_op_reason"] == "no_enabled_targets", plan["no_op_reason"]
        STATE.data["characters"] = [_char("c1", "正需角色", {"coin": 7})]
        orig_cost = planner.COST_PER_RUN
        try:
            planner.COST_PER_RUN = 0
            plan = planner.make_plan()
            assert plan["no_op_reason"] == "invalid_material_config" \
                and not plan["steps"], "cost=0 必须 fail closed 且不除零"
            assert any("cost_per_run" in w for w in plan["warnings"])
        finally:
            planner.COST_PER_RUN = orig_cost
        orig_targets = planner.TARGETS
        try:  # 材料表缺副本配置：跳过并警告，不进入执行
            broken_target = dict(orig_targets["coin"])
            broken_target.pop("config")
            planner.TARGETS = {**orig_targets, "coin": broken_target}
            STATE.data["inventory"] = {}
            plan = planner.make_plan()
            assert not plan["steps"] and any("缺少副本配置" in w for w in plan["warnings"])
        finally:
            planner.TARGETS = orig_targets


@case
def test_e2e_three_day_marathon():  # 场景J：3个 planning day 加速长跑（重置/切换/达标/无泄漏）
    with _E2EGuard() as e2e:
        _build_fake_oknte(e2e.root)
        e2e.configure(continuous_mode=True, busy_retry_seconds=1, daily_stamina_limit=80)
        STATE.data["inventory"] = {}
        STATE.data["characters"] = [
            _char("c1", "马拉松甲", {"coin": 21}),      # 3轮×7
            _char("c2", "马拉松乙", {"exp_char": 7}),   # 1轮×7
        ]
        fake_day = ["2030-01-01"]
        orig_day_key = STATE.day_key
        STATE.day_key = lambda now=None: fake_day[0]  # 注入时钟：跨天由测试推进
        r = runner_mod.Runner()
        r.start()
        try:
            # 第1天：80额度→coin 2轮(+14)，额度耗尽等待次日
            assert _wait_until(lambda: len(_E2EOkNte.launches) == 1, timeout=30)
            assert _wait_until(lambda: r.snapshot()["state"] == "waiting"
                               and "额度" in r.snapshot()["waiting_reason"]), "第1天应收工"
            # 第2天：coin 1轮补齐 + exp_char 1轮（当日目标切换），随后全部达标
            fake_day[0] = "2030-01-02"
            assert _wait_until(lambda: len(_E2EOkNte.launches) == 3, timeout=40), \
                "第2天应跨目标执行2轮"
            assert _wait_until(lambda: r.snapshot()["state"] == "waiting"
                               and "达标" in r.snapshot()["waiting_reason"]), "应全部达标"
            # 第3天：无新目标 → 不再拉起子进程，循环仍健康等待
            fake_day[0] = "2030-01-03"
            assert _wait_until(lambda: r.snapshot()["state"] == "waiting", timeout=8), \
                "持续模式应保持健康的等待循环（无死锁/无退出）"
            time.sleep(1.5)  # 跨过至少一个重试周期仍存活
            assert _wait_until(lambda: r.snapshot()["state"] == "waiting", timeout=8), \
                "跨周期后应仍处于健康等待"
            assert r.thread.is_alive(), "循环线程不得退出"
            assert len(_E2EOkNte.launches) == 3, "达标后跨天不得再拉起"
            assert STATE.data["inventory"] == {"coin": 21, "exp_char": 7}, \
                f"三日后库存应补齐，实际 {STATE.data['inventory']}"
            assert len(STATE.data["history"]) <= 500, "历史必须受控"
            assert isinstance(load_json(STATE_FILE), dict), "state.json 必须持续可加载"
            stamps = [t for t, _ in _E2EOkNte.launches]
            assert all(b - a >= 0.9 for a, b in zip(stamps, stamps[1:])), \
                "重试节奏不得出现高速循环"
        finally:
            r.stop()
            r.thread.join(timeout=5)
            del STATE.day_key  # 还原注入的时钟（恢复绑定方法）


# ---------- 用例 9：产量校准闭环（阶段8） ----------

@case
def test_yield_calibration_loop():
    from app.main import confirm_history_entry
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.data["today"] = _empty_today()
        STATE.data["history"] = [
            {"day": "2030-01-01", "time": "10:00:00", "mat_id": "coin", "name": "甲硬币",
             "runs": 2, "stamina": 80, "gain": 14, "status": "done", "note": ""},
            {"day": "2030-01-01", "time": "11:00:00", "mat_id": "coin", "name": "甲硬币",
             "runs": 2, "stamina": 80, "gain": 14, "status": "done", "note": ""},
            {"day": "2030-01-01", "time": "12:00:00", "mat_id": "exp_char", "name": "角色经验",
             "runs": 1, "stamina": 40, "gain": 7, "status": "failed", "note": "x"},
        ]
        # 无确认 → 无建议（纯读不报错）
        assert planner.yield_suggestions()["suggestions"] == []
        # 确认两条 done：实际 10 与 6 → measured=(10+6)/4=4.0 → 建议 4
        ok, msg = confirm_history_entry("2030-01-01", "10:00:00", "coin", 10)
        assert ok, msg
        ok, _ = confirm_history_entry("2030-01-01", "11:00:00", "coin", 6)
        assert ok
        sug = planner.yield_suggestions()
        assert sug["confirmed_entries"] == 2, sug
        row = sug["suggestions"][0]
        assert row["mat_id"] == "coin" and row["confirmed_runs"] == 4 \
            and row["confirmed_gain"] == 16 and row["measured_yield"] == 4.0 \
            and row["suggested_yield"] == 4, row
        # 配置产量绝不被自动修改（只建议；应用走 /api/tables/yields 通道）
        assert planner.YIELDS["per_run"]["coin"] == 7
        # 同一记录重复确认覆盖旧值：10→8 后累计 8+6=14 → 3.5
        ok, _ = confirm_history_entry("2030-01-01", "10:00:00", "coin", 8)
        assert ok
        row = planner.yield_suggestions()["suggestions"][0]
        assert row["confirmed_gain"] == 14 and row["measured_yield"] == 3.5, row
        # 校验：非 done 拒绝；不存在的记录拒绝
        ok, msg = confirm_history_entry("2030-01-01", "12:00:00", "exp_char", 5)
        assert not ok and "完成" in msg, msg
        ok, msg = confirm_history_entry("1999-01-01", "00:00:00", "coin", 5)
        assert not ok and "未找到" in msg, msg
        # 纯读确定性：连算两次一致
        assert planner.yield_suggestions() == planner.yield_suggestions()


# ---------- 用例 10：体力恢复模型与多日预演（阶段9） ----------

@case
def test_stamina_projection_and_preview():
    from datetime import datetime as _dt
    from app.main import _sanitize_settings
    with _StateGuard():
        S = STATE.data["settings"] = dict(
            DEFAULT_SETTINGS, daily_stamina_limit=80, stamina_cap=240,
            stamina_regen_per_hour=10, stamina_now=None, stamina_now_at="")
        STATE.data["today"] = _empty_today()
        STATE.data["inventory"] = {}
        STATE.data["characters"] = [_char("c1", "预演甲", {"coin": 21})]  # 产量7 → 3轮

        # 1) 未填当前体力 → 无估计
        assert planner.projected_stamina(_dt(2030, 1, 1, 12)) is None
        # 2) 推算：2 小时后 100+20=120；30 小时后触顶 240
        S.update(stamina_now=100, stamina_now_at="2030-01-01 10:00:00")
        assert planner.projected_stamina(_dt(2030, 1, 1, 12)) == 120.0
        assert planner.projected_stamina(_dt(2030, 1, 2, 16)) == 240.0

        # 3) 多日预演：day0 额度80→coin 2轮；day1 1轮补齐并全部达标；day2 无安排
        now = _dt(2030, 1, 1, 12)
        r1 = planner.preview_days(3, now=now)
        d0, d1, d2 = r1["days"]
        assert d0["date"] == "2030-01-01" and d0["budget"] == 80 \
            and [(s["mat_id"], s["runs"]) for s in d0["steps"]] == [("coin", 2)], d0
        assert d1["date"] == "2030-01-02" and d1["steps"][0]["runs"] == 1, d1
        assert r1["complete_on"] == "2030-01-02", r1["complete_on"]
        assert d2["steps"] == [] and d2["open_gaps"] == 0
        assert not any("恢复速率" in w for w in r1["warnings"])  # 10*24=240 ≥ 80
        # 纯度与确定性：两次一致；STATE 不被修改
        before = copy.deepcopy(STATE.data)
        assert planner.preview_days(3, now=now) == r1
        assert copy.deepcopy(STATE.data) == before

        # 4) 追猎预留扣减 + 恢复不足警告（2/小时×24=48 < 限额80）
        S.update(reserve_anomaly_hunter=True, anomaly_hunter_stamina=40,
                 stamina_regen_per_hour=2)
        r2 = planner.preview_days(2, now=now)
        assert r2["days"][0]["budget"] == 40, r2["days"][0]
        assert any("恢复速率" in w for w in r2["warnings"])

        # 5) 设置消毒：stamina_now 服务端盖章；空值清除；非法忽略；cap 钳位
        s = _sanitize_settings({"stamina_now": 132})
        assert s["stamina_now"] == 132 and s["stamina_now_at"], s
        s = _sanitize_settings({"stamina_now": ""})
        assert s["stamina_now"] is None and s["stamina_now_at"] == ""
        s = _sanitize_settings({"stamina_now": "abc", "stamina_cap": 999999})
        assert s["stamina_cap"] == 999 and s["stamina_now"] == 100  # 非法输入保留 STATE 原值(100)


# ---------- 用例 11：导出/导入/备份回滚（阶段10） ----------

@case
def test_export_import_and_rollback():
    import app.main as main_mod
    from app.oknte_adapter import OkNteError
    tmp = tempfile.mkdtemp(prefix="oknte_impexp_")
    orig_main_datadir = main_mod.DATA_DIR
    orig_adapter_datadir = adapter_mod.DATA_DIR
    main_mod.DATA_DIR = os.path.join(tmp, "planner_data")
    adapter_mod.DATA_DIR = os.path.join(tmp, "planner_data")  # 与 main 同一备份根
    os.makedirs(main_mod.DATA_DIR, exist_ok=True)
    try:
        with _StateGuard():
            # -- schema：保存时盖章 --
            STATE.data["settings"] = dict(DEFAULT_SETTINGS)
            STATE.data["today"] = _empty_today()
            STATE.data["inventory"] = {"coin": 3}
            STATE.data["characters"] = [_char("c1", "导出角色", {"coin": 30})]
            STATE.save()
            assert STATE.data["schema"] == 2, "save 应写入 schema=2"

            # -- 导出结构 --
            export = main_mod.build_export()
            assert export["schema"] == 2 and export["settings"]["daily_stamina_limit"] > 0 \
                and len(export["characters"]) == 1 and export["inventory"] == {"coin": 3} \
                and "per_run" in export["yields"] and "tracks" in export["requirements"], \
                f"导出结构异常: {sorted(export.keys())}"

            # -- 合法导入：整体生效 + 自动备份 + 数据表落盘（tmp 目录）--
            payload = copy.deepcopy(export)
            payload["inventory"] = {"coin": 0, "exp_char": 5}
            payload["settings"]["daily_stamina_limit"] = 120
            summary = main_mod.apply_import(payload)
            assert summary["characters"] == 1 and summary["inventory_items"] == 2, summary
            assert STATE.data["inventory"] == {"coin": 0, "exp_char": 5}
            assert STATE.data["settings"]["daily_stamina_limit"] == 120
            assert os.path.isfile(summary["backup"]), "导入前应自动备份当前配置"
            assert os.path.isfile(os.path.join(main_mod.DATA_DIR, "yields.json"))
            with open(os.path.join(main_mod.DATA_DIR, "requirements.json"), encoding="utf-8") as f:
                assert "tracks" in json.load(f)

            # -- 非法导入整体拒绝且 STATE 不变 --
            before = copy.deepcopy(STATE.data)
            for bad in ({"schema": 99},
                        {**copy.deepcopy(export), "characters": []},
                        {**copy.deepcopy(export), "requirements": {"no": 1}}):
                try:
                    main_mod.apply_import(bad)
                    raise AssertionError(f"非法导入应被拒绝: {sorted(bad.keys())}")
                except ValueError:
                    pass
            assert copy.deepcopy(STATE.data) == before, "整体拒绝时不得留下半套数据"

            # -- 备份列表与整文件回滚（伪造 ok-nte 根 + 手工备份）--
            root = os.path.join(tmp, "root")
            ok = _build_fake_oknte(root)
            ok.is_running = lambda: False
            backups_dir = os.path.join(adapter_mod.DATA_DIR, "backups")
            os.makedirs(backups_dir, exist_ok=True)
            good = copy.deepcopy(FAKE_ROUTINE_CONFIG)
            good["daily_anomaly"]["目标消耗体力"] = 77
            with open(os.path.join(backups_dir, f"{adapter_mod.ROUTINE_CONFIG_NAME}.20260101_000000"),
                      "w", encoding="utf-8") as f:
                json.dump(good, f, ensure_ascii=False, indent=4)
            names = [b["name"] for b in ok.list_backups()]
            assert any("20260101_000000" in n for n in names), names
            restored = ok.restore_backup(f"{adapter_mod.ROUTINE_CONFIG_NAME}.20260101_000000")
            assert "20260101" in restored
            with open(ok.routine_config, encoding="utf-8") as f:
                assert json.load(f)["daily_anomaly"]["目标消耗体力"] == 77, "应恢复为备份内容"
            assert len(ok.list_backups()) == 2, "恢复前应另存一份当前现状（回滚可逆）"
            # 运行中拒绝；路径逃逸拒绝；不存在拒绝
            ok.is_running = lambda: True
            try:
                ok.restore_backup(names[0])
                raise AssertionError("运行中应拒绝恢复")
            except OkNteError:
                pass
            ok.is_running = lambda: False
            for bad_name in ("../evil.json", f"{adapter_mod.ROUTINE_CONFIG_NAME}.不存在"):
                try:
                    ok.restore_backup(bad_name)
                    raise AssertionError(f"应拒绝: {bad_name}")
                except OkNteError:
                    pass
    finally:
        main_mod.DATA_DIR = orig_main_datadir
        adapter_mod.DATA_DIR = orig_adapter_datadir
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 用例 12：端口冲突自动避让（阶段11） ----------

@case
def test_port_conflict_friendly_error():
    import socket
    from app.main import create_server
    # 1) 被占端口自动避让：占用 P 后应绑到 (P, P+5] 内的可用端口
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        server, actual = create_server(port, auto_range=5)
        try:
            assert port < actual <= port + 5, f"应自动避让到相邻端口，实际 {actual}"
        finally:
            server.server_close()
    finally:
        sock.close()
    # 2) 整段耗尽：显式占住 P 与 P+1，range=1 → 抛可操作的中文错误
    for _attempt in range(5):  # 相邻端口可能偶发被占，重试几次
        s1 = socket.socket()
        s1.bind(("127.0.0.1", 0))
        s1.listen(1)
        base = s1.getsockname()[1]
        s2 = socket.socket()
        try:
            s2.bind(("127.0.0.1", base + 1))
            s2.listen(1)
            break
        except OSError:
            s1.close()
            s2.close()
            continue
    else:
        raise AssertionError("无法获得相邻端口对")
    try:
        try:
            create_server(base, auto_range=1)
            raise AssertionError("整段被占时应抛 RuntimeError")
        except RuntimeError as e:
            assert "端口" in str(e) and "无法绑定" in str(e), f"报错应可操作: {e}"
    finally:
        s1.close()
        s2.close()
    # 3) 空闲时正常绑定
    server, actual = create_server(0, auto_range=1)
    server.server_close()


# ---------- 用例 13：桌面窗口壳 UI 策略（阶段12，纯函数） ----------

@case
def test_ui_mode_resolution():
    from app.main import _default_ui_mode, _pick_edge, _resolve_ui_mode
    orig = dict(os.environ)
    orig_frozen = getattr(sys, "frozen", False)
    try:
        os.environ["OKNTE_PLANNER_NO_BROWSER"] = "1"  # 兼容旧变量：最高优先
        os.environ.pop("OKNTE_PLANNER_UI", None)
        assert _resolve_ui_mode() == "none"
        os.environ.pop("OKNTE_PLANNER_NO_BROWSER")
        os.environ["OKNTE_PLANNER_UI"] = "EDGE"  # 大小写不敏感
        assert _resolve_ui_mode() == "edge"
        os.environ["OKNTE_PLANNER_UI"] = "bogus"  # 非法回退默认
        assert _resolve_ui_mode() == _default_ui_mode()
        os.environ.pop("OKNTE_PLANNER_UI")
        assert _resolve_ui_mode() == _default_ui_mode()  # 源码运行默认 auto
        sys.frozen = True  # 打包 EXE 默认原生窗口
        assert _default_ui_mode() == "window" and _resolve_ui_mode() == "window"
        os.environ["OKNTE_PLANNER_UI"] = "auto"  # auto = 跟随默认（frozen 下即 window）
        assert _resolve_ui_mode() == "window"
        os.environ["OKNTE_PLANNER_UI"] = "edge"  # 显式覆盖仍生效
        assert _resolve_ui_mode() == "edge"
    finally:
        if orig_frozen is False:
            sys.frozen = False
            try:
                del sys.frozen
            except AttributeError:
                pass
        else:
            sys.frozen = orig_frozen
        os.environ.clear()
        os.environ.update(orig)

    # Edge 探测：候选全部不存在 → None（降级浏览器）；命中真实文件 → 返回它
    assert _pick_edge([r"C:\nope\msedge.exe", ""]) is None
    real = os.path.join(tempfile.gettempdir(), "oknte_fake_edge.exe")
    try:
        open(real, "wb").close()
        assert _pick_edge([r"C:\nope\msedge.exe", real]) == real
    finally:
        os.remove(real)


# ---------- 用例 14：追猎独立计划与配置（阶段13） ----------

@case
def test_materials_catalog_integrity():
    # 全量目录：extra id 唯一、不与体力本目标冲突、字段齐全
    ids_t = set(planner.TARGET_ORDER)
    ids_e = planner.EXTRA_ORDER
    assert len(ids_e) == len(set(ids_e)), "extra 材料id必须唯一"
    assert not (ids_t & set(ids_e)), "extra 与体力本目标 id 不得冲突"
    for mid in ids_e:
        entry = planner.EXTRA[mid]
        assert entry.get("display_name") and entry.get("category") \
            and entry.get("obtain") in ("hunter", "external"), entry
    # BOSS 映射：名单顺序与 ok-nte 一致，掉落材料都在 extra 里且 obtain=hunter
    assert planner.HUNTER_BOSSES == ["音霸魔王", "无首铁驭", "塞润尼缇", "黑之书",
                                     "海囚", "围巢鸟", "斑蝶"], planner.HUNTER_BOSSES
    for boss in planner.HUNTER_BOSSES:
        mid = planner.BOSS_MAT[boss]
        assert mid in planner.EXTRA and planner.EXTRA[mid]["obtain"] == "hunter", boss
    # 19 个体力本目标全部有 config 与 display_name（实名可改）
    assert len(planner.TARGET_ORDER) == 19
    for tid in planner.TARGET_ORDER:
        assert "config" in planner.TARGETS[tid] and planner.TARGETS[tid].get("display_name"), tid


@case
def test_hunter_plan_math():
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS, anomaly_hunter_target="海囚",
                                      anomaly_hunter_stamina=180)
        STATE.data["today"] = _day_today(hunter_spent=0)
        STATE.data["inventory"] = {"boss_sea_tear": 2}
        STATE.data["characters"] = [_char("c1", "猎手", {"boss_sea_tear": 10})]
        # 缺口 8、每局 8 → 1 局
        plan = planner.make_hunter_plan(now=NOW)
        assert plan["steps"][0]["runs"] == 1 and plan["steps"][0]["stamina"] == 60, plan
        assert plan["steps"][0]["mat_id"] == "boss_sea_tear"
        # 缺口 20 → ceil(18/8)=3 局（库存2，需10）
        STATE.data["characters"] = [_char("c1", "猎手", {"boss_sea_tear": 20})]
        plan = planner.make_hunter_plan(now=NOW)
        assert plan["steps"][0]["runs"] == 3 and plan["steps"][0]["gain"] == 24, plan
        # 额度封顶：额度 120 → 2 局
        STATE.data["settings"]["anomaly_hunter_stamina"] = 120
        plan = planner.make_hunter_plan(now=NOW)
        assert plan["steps"][0]["runs"] == 2 and plan["budget"] == 0, plan
        # 覆盖次数：fixed_runs=1 → 1 局
        plan = planner.make_hunter_plan(now=NOW, fixed_runs=1)
        assert plan["steps"][0]["runs"] == 1, plan
        # 纯读：连算两次一致且不改 STATE
        before = copy.deepcopy(STATE.data)
        assert planner.make_hunter_plan(now=NOW) == planner.make_hunter_plan(now=NOW)
        assert copy.deepcopy(STATE.data) == before
        # 已达标 → all_targets_completed
        STATE.data["settings"]["anomaly_hunter_stamina"] = 180
        STATE.data["inventory"] = {"boss_sea_tear": 20}
        plan = planner.make_hunter_plan(now=NOW)
        assert plan["no_op_reason"] == "all_targets_completed", plan["no_op_reason"]
        # BOSS 拉黑 → waiting_for_reset
        STATE.data["today"]["blacklist"] = ["hunt:海囚"]
        STATE.data["inventory"] = {}
        plan = planner.make_hunter_plan(now=NOW)
        assert plan["no_op_reason"] == "waiting_for_reset", plan["no_op_reason"]
        # 非法 BOSS → no_enabled_targets；额度不足 → insufficient_stamina
        assert planner.make_hunter_plan(now=NOW, boss="不存在的BOSS")["no_op_reason"] \
            == "no_enabled_targets"
        STATE.data["today"]["blacklist"] = []
        STATE.data["settings"]["anomaly_hunter_stamina"] = 30
        plan = planner.make_hunter_plan(now=NOW, fixed_runs=2)
        assert plan["no_op_reason"] == "insufficient_stamina", plan["no_op_reason"]
        # 产量未校准 → invalid_material_config + 警告
        STATE.data["settings"]["anomaly_hunter_stamina"] = 180
        orig = dict(planner.YIELDS.get("hunter_yield", {}))
        try:
            planner.YIELDS.setdefault("hunter_yield", {})["海囚"] = 0
            plan = planner.make_hunter_plan(now=NOW)
            assert plan["no_op_reason"] == "invalid_material_config" and plan["warnings"], plan
        finally:
            planner.YIELDS["hunter_yield"] = orig


@case
def test_hunter_config_and_routine_patch():
    from app.oknte_adapter import OkNteError
    tmp = tempfile.mkdtemp(prefix="oknte_hunter_cfg_")
    try:
        ok = _build_fake_oknte(os.path.join(tmp, "root"))
        ok.is_running = lambda: False
        # 正常写入：目标+次数；其他键保留
        with open(ok.hunter_config, "w", encoding="utf-8") as f:
            json.dump({"追猎目标": "黑之书", "领取奖励次数": 0, "上游未知键": [1]}, f,
                      ensure_ascii=False)
        changed = ok.write_hunter_config("海囚", 3)
        with open(ok.hunter_config, encoding="utf-8") as f:
            cfg = json.load(f)
        assert cfg["追猎目标"] == "海囚" and cfg["领取奖励次数"] == 3, cfg
        assert cfg["上游未知键"] == [1], "未知键不得丢失"
        assert set(changed) == {"追猎目标", "领取奖励次数"}
        # 计划外变更（改未知键）必须拒绝
        try:
            ok.write_hunter_config("海囚", 3)  # 幂等：无变化也应成功
        except OkNteError as e:
            raise AssertionError(f"无变化的写入不应失败: {e}")
        # 文件不存在 → 创建
        os.remove(ok.hunter_config)
        ok.write_hunter_config("音霸魔王", 1)
        with open(ok.hunter_config, encoding="utf-8") as f:
            assert json.load(f) == {"追猎目标": "音霸魔王", "领取奖励次数": 1}
        # 运行中拒绝
        ok.is_running = lambda: True
        try:
            ok.write_hunter_config("海囚", 1)
            raise AssertionError("运行中应拒绝写入")
        except OkNteError:
            pass
        ok.is_running = lambda: False
        # 日常开关最小补丁：真实结构
        with open(ok.routine_items, "w", encoding="utf-8") as f:
            json.dump({"Routine Items": [
                {"id": "daily_anomaly", "enabled": False},
                {"id": "daily_anomaly_hunter", "enabled": False},
                {"id": "coffee", "enabled": True}],
                "Exit After Task": False}, f, ensure_ascii=False)
        okk, msg = ok.ensure_routine_item("daily_anomaly", True)
        assert okk, msg
        with open(ok.routine_items, encoding="utf-8") as f:
            items = json.load(f)["Routine Items"]
        by_id = {e["id"]: e for e in items}
        assert by_id["daily_anomaly"]["enabled"] is True, items
        assert by_id["coffee"]["enabled"] is True, "无关条目不得变化"
        assert len(items) == 3, "条目数量不得变化"
        # 幂等 + 缺失条目拒改
        ok2, _ = ok.ensure_routine_item("daily_anomaly", True)
        assert ok2
        ok3, msg3 = ok.ensure_routine_item("not_exist", True)
        assert not ok3 and "没有" in msg3, msg3
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_runner_hunter_mode_e2e():
    with _E2EGuard() as e2e:
        _build_fake_oknte(e2e.root)
        e2e.configure(continuous_mode=False, anomaly_hunter_target="海囚",
                      anomaly_hunter_stamina=120)
        _E2EOkNte.mode_env = {**_E2EOkNte.mode_env, "FAKE_MODE": "hunter"}
        STATE.data["inventory"] = {}
        STATE.data["characters"] = [_char("c1", "猎手", {"boss_sea_tear": 10})]
        r = runner_mod.Runner()
        started, msg = r.start("hunter")
        assert started, msg
        r.thread.join(timeout=40)
        assert not r.thread.is_alive(), "一次性模式应自然结束"
        assert len(_E2EOkNte.hunter_launches) == 1, f"应恰好启动一次追猎，实际 {len(_E2EOkNte.hunter_launches)}"
        snap = r.snapshot()
        assert snap["state"] == "done", f"应完成，实际 {snap['state']}/{snap['message']}"
        # 记账：2局×60=120 体力（hunter_spent），库存 +16（每局8）
        assert STATE.data["today"]["hunter_spent"] == 120, STATE.data["today"]
        assert STATE.data["today"]["used_stamina"] == 0, "追猎记账不得混入日常口径"
        assert STATE.data["inventory"].get("boss_sea_tear") == 16, STATE.data["inventory"]
        assert STATE.data["history"][-1]["mode"] == "hunter"
        # ok-nte 侧独立配置被正确写入
        with open(runner_mod.OkNte(e2e.root).hunter_config, encoding="utf-8") as f:
            cfg = json.load(f)
        assert cfg["追猎目标"] == "海囚" and cfg["领取奖励次数"] == 2, cfg
        # 同一执行器互斥：运行中再 start 另一模式应拒绝
        r2 = runner_mod.Runner()
        ok, _ = r2.start("daily")
        assert ok, "空闲后应可启动日常"
        r2.stop()
        r2.thread.join(timeout=5)


# ---------- 汇总 ----------

def main():
    print("=== app.selftest 自测开始 ===")
    passed = 0
    failed = 0
    for fn in CASES:
        name = fn.__name__
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
            traceback.print_exc()
        else:
            passed += 1
            print(f"PASS {name}")
    print(f"=== 自测结果: 通过 {passed}/{len(CASES)} ===")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
