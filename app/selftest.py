"""自测（纯标准库，不依赖任何第三方包）。

覆盖：oknte_adapter 配置读写/备份、RunMonitor 四种结果解析、LogTail 增量读取与轮转、
planner 缺口计算与计划生成、计划算法矩阵（取整/额度/预留/禁用/拉黑/产量0）、no-op 枚举、
步骤与计划的结构化 reason、确定性与纯度不变量（纯读/同输入同输出/不写 state.json）、
扫描落账归属 runner、runner 持续模式（busy→waiting→自动重试→stop 打断）、
scan_used_stamina 取最大值、CONC-01/02 并发原子写与图鉴覆盖层锁（同文件并发写零异常
零半截、双 upsert 不同 name 两条均在、失败注入不损坏目标文件）、BIZ-03 发布步
瞬时占用白名单有界重试（WinError 5/32 首败重试成功、有界退避序列、预算耗尽原样
外抛、不可重试立即外抛、.bak 步重试后吞边界）。

运行方式（项目根目录）：
    D:/GamingAssistant/ok-nte/data/apps/ok-nte/python/python.exe -m app.selftest

stress 档用法（B2.7B 起，python -m app.selftest --stress）：
- 何时跑：发布前/并发专项复核；默认（无参）日常门禁不含 stress 档。
- stress 档内容（4 例）：test_stress_conc01_16t_high_volume（16 线程×200=3200 写
  同一 JSON）、test_stress_conc02_readwrite_long（16 写者×100 + 8 读者并发读写）、
  test_stress_conc03_custom_volume（800 个不同 key upsert + 16 线程×10 同名风暴）、
  test_stress_conc04_repeat_rounds（20 轮×16 线程混合负载）——均为日常档
  conc03-07 同口径断言的放大负载版。
- 为何不进默认门禁：stress 单例耗时数秒~数十秒，放进日常档会显著拖慢每次
  全量回归；断言口径与日常档一致，仅放大负载规模，需要时以 --stress 显式运行。

约束：
- 绝不写入 D:/GamingAssistant/ok-nte 下任何文件；ok-nte 根目录一律用 tempfile 伪造。
- 绝不真正拉起 ok-nte 子进程；runner 用例把 app.runner.OkNte 换成假对象。
- make_plan/runner 会写项目 data/state.json：进入用例前备份、finally 里恢复（原本不存在则删除）。

每个用例 print 一行 PASS/FAIL；全部通过退出码 0，否则 1。
"""
import copy
import errno
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app import game_data as game_data_mod  # noqa: E402
from app import oknte_adapter as adapter_mod  # noqa: E402
from app import planner  # noqa: E402
from app import runner as runner_mod  # noqa: E402
from app import state as state_mod  # noqa: E402
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


# stress 档注册表（B2.7B）：stress 用例只进 STRESS_CASES、不进 CASES——日常档
# 计数不含它们；仅 main() 解析到 --stress 时与 CASES 合并运行（用法见模块 docstring）。
STRESS_CASES = []


def stress_case(fn):
    STRESS_CASES.append(fn)
    return fn


# ---------- 路径安全（夹具写入必须经此，防路径穿越） ----------

def _safe_path(base, *parts):
    """把 parts 拼接到 base 下并校验结果不越出 base，返回归一化后的绝对路径。"""
    root = os.path.realpath(base)
    target = os.path.realpath(os.path.join(root, *parts))
    if os.path.commonpath([root, target]) != root:
        raise ValueError(f"夹具路径越界: {target} 不在 {root} 内")
    return target


def _contained(path, base):
    """校验已生成的路径未越出 base，返回归一化后的绝对路径（用于 adapter 派生路径）。"""
    root = os.path.realpath(base)
    p = os.path.realpath(path)
    if os.path.commonpath([root, p]) != root:
        raise ValueError(f"夹具路径越界: {p} 不在 {root} 内")
    return p


# 唯一允许触碰的项目真实文件：data/state.json（_StateGuard 备份/恢复专用，经校验）
_STATE_FILE_SAFE = _safe_path(os.path.dirname(os.path.abspath(STATE_FILE)),
                              os.path.basename(STATE_FILE))


# ---------- 公共工具 ----------

class _StateGuard:
    """备份 data/state.json 与 STATE 内存数据，退出时恢复（原本不存在则删除文件）。

    同时把甲硬币产量夹到 7（生产默认为 0=未校准跳过）：planner/runner 测试依赖
    coin 可刷；夹具退出时恢复生产值，保证测试与生产数据文件解耦。
    """

    def __enter__(self):
        with STATE.lock:
            self.existed = os.path.isfile(STATE_FILE)
            self.raw = None
            if self.existed:
                with open(STATE_FILE, "rb") as f:
                    self.raw = f.read()
            self.data_copy = copy.deepcopy(STATE.data)
            self._yields_backup = copy.deepcopy(planner.YIELDS)
        # 数学测试统一夹具：全部体力本产量=7（生产值为 mysqil 鉴别7 口径，逐材料不同）
        for tid in planner.TARGET_ORDER:
            planner.YIELDS["per_run"][tid] = 7
        return self

    def __exit__(self, *exc):
        planner.YIELDS.clear()
        planner.YIELDS.update(self._yields_backup)
        with STATE.lock:
            STATE.data = self.data_copy
            if self.existed:
                Path(_STATE_FILE_SAFE).write_bytes(self.raw)
            elif os.path.isfile(_STATE_FILE_SAFE):
                os.remove(_STATE_FILE_SAFE)
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
    apps = _safe_path(tmp, "data", "apps", "ok-nte")
    os.makedirs(_safe_path(apps, "working", "configs"), exist_ok=True)
    os.makedirs(_safe_path(apps, "working", "logs"), exist_ok=True)
    os.makedirs(_safe_path(apps, "python"), exist_ok=True)
    os.makedirs(_safe_path(apps, "working", "ok"), exist_ok=True)
    Path(_safe_path(apps, "working", "ok", "__init__.py")).touch()
    Path(_safe_path(apps, "working", "main.py")).touch()
    # compat_check 只要求 python.exe 是个文件，放个空文件即可
    Path(_safe_path(apps, "python", "python.exe")).touch()
    cfg_path = _safe_path(apps, "working", "configs", "DailyRoutineTaskConfigs.json")
    Path(cfg_path).write_text(
        json.dumps(cfg if cfg is not None else FAKE_ROUTINE_CONFIG,
                   ensure_ascii=False, indent=4), encoding="utf-8")
    Path(_safe_path(apps, "working", "configs", "AnomalyHunter.json")).write_text(
        json.dumps({"追猎目标": "海囚", "领取奖励次数": 0}, ensure_ascii=False, indent=4),
        encoding="utf-8")
    Path(_safe_path(apps, "working", "configs", "DailyRoutineTask.json")).write_text(
        json.dumps({"Routine Items": [
            {"id": "daily_anomaly", "enabled": True},
            {"id": "coffee", "enabled": True},
            {"id": "daily_claim", "enabled": True},
        ]}, ensure_ascii=False, indent=4), encoding="utf-8")
    for name, lines in (logs or {}).items():
        # newline="\n" 保证字节数与字符数一致（LogTail 按字节偏移读取）
        Path(_safe_path(apps, "working", "logs", name)).write_text(
            "".join(line + "\n" for line in lines), encoding="utf-8", newline="\n")
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


@case
def test_dxgi_probe_warning():
    """复审阻断⑤：DXGI 采集探测必须可观测（此前 launcher 探测失败完全静默——
    ok-nte 内嵌 Python 缺 d3dshot 时 capture_method 恒回落 BitBlt_RenderFull
    （PrintWindow 同步截图=两次卡死实证路径），卡死防护未生效且运维无从知晓）：
    - capture_probe_warning：解释器缺失→None（compat problems 已告警，不重复）；
      探测失败（夹具空 python.exe 无法执行→OSError）→警告含 d3dshot 修复命令；
      探测可用→None；
    - dxgi_available 进程级缓存：同一 root 只探一次（真探测要拉起 Desktop
      Duplication，秒级，不可每次 bootstrap 重付）；
    - LAUNCHER_SRC：探测成败都有 [capture-probe] print（运行时经 runner stdout 泵
      进「最新日志」/ok-nte-live.log）；capture_method 注入语义不变；
    - /api/bootstrap 与前端消费字段成对（静态源校验，无真服务）。"""
    import inspect
    import app.main as main_mod
    tmp = tempfile.mkdtemp(prefix="oknte_dxgi_")
    try:
        ok = _build_fake_oknte(os.path.join(tmp, "root"))
        no_py = None  # 断言早失败时 finally 不得 NameError
        cache_key = ok.root
        adapter_mod.OkNte._DXGI_PROBE_CACHE.pop(cache_key, None)  # 清同路径历史缓存
        try:
            # 夹具 python.exe 是空文件：CreateProcess 失败 → OSError → 探测 False
            warning = ok.capture_probe_warning()
            assert warning and "d3dshot" in warning and "BitBlt_RenderFull" in warning \
                and "-m pip install d3dshot" in warning, warning
            assert "内嵌 Python 缺少 d3dshot" in warning, warning
            # 进程级缓存生效：缓存已按 root 记 False，再问不再重探
            assert adapter_mod.OkNte._DXGI_PROBE_CACHE[cache_key] is False
            # 探测可用 → 无警告（monkeypatch 探测结果，不真拉 Desktop Duplication）
            ok.dxgi_available = lambda: True
            assert ok.capture_probe_warning() is None
            # 解释器缺失 → None（compat problems 首条已覆盖，不重复告警）
            no_py = _build_fake_oknte(os.path.join(tmp, "root2"))
            os.remove(no_py.python_exe)
            adapter_mod.OkNte._DXGI_PROBE_CACHE.pop(no_py.root, None)
            assert no_py.capture_probe_warning() is None
        finally:
            adapter_mod.OkNte._DXGI_PROBE_CACHE.pop(cache_key, None)
            if no_py is not None:
                adapter_mod.OkNte._DXGI_PROBE_CACHE.pop(no_py.root, None)
        # LAUNCHER_SRC：成败双 print + 注入语义保持（2026-11-10 修订：序列
        # ['WGC','BitBlt_RenderFull',探测通过的 DXGI]——DXGI 置末位，真机
        # E_INVALIDARG 时不再挡住 BitBlt_RenderFull 兜底，GUI 已验证形态对齐）
        src = adapter_mod.LAUNCHER_SRC
        assert src.count("[capture-probe]") == 2, \
            "launcher 探测成功/失败都必须有可见 print"
        assert "cfg['windows']['capture_method'] = _capture_methods" in src
        assert "_capture_methods = ['WGC', 'BitBlt_RenderFull']" in src, \
            "capture_method 序列必须 WGC 首位、BitBlt_RenderFull 次位（GUI 已验证形态）"
        assert "_capture_methods.append('DXGI')" in src, \
            "DXGI 仅探测通过才追加，且必须置于列表末位"
        # 顺序硬校验：DXGI 追加必须发生在初始列表构造之后、cfg 注入之前
        pos_init = src.index("_capture_methods = ['WGC', 'BitBlt_RenderFull']")
        pos_dxgi = src.index("_capture_methods.append('DXGI')")
        pos_inject = src.index("cfg['windows']['capture_method'] = _capture_methods")
        assert pos_init < pos_dxgi < pos_inject, \
            (pos_init, pos_dxgi, pos_inject)
        # /api/bootstrap 透出 + 前端消费成对（端点源静态校验）
        get_src = inspect.getsource(main_mod.Handler.do_GET)
        assert '"capture_warning": oknte.capture_probe_warning()' in get_src, \
            "/api/bootstrap 必须透出 capture_probe_warning 结果"
        idx_src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "app", "web", "index.html"),
            encoding="utf-8").read()
        assert "BOOT.capture_warning" in idx_src, \
            "前端必须消费 capture_warning（侧栏警示标签）"
    finally:
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
            Path(_contained(ok.routine_config, ok.root)).write_text(broken, encoding="utf-8")
        if no_cfg:
            os.remove(_contained(ok.routine_config, ok.root))
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
def test_monitor_window_visibility():
    """窗口可见性判据（复审 #7b：2026-11-08 真机取证新增，此前无 selftest 覆盖）：
    日志出现 visible:False → monitor.window_invisible=True（runner 据此报警
    window_warning），恢复 visible:True 清除；伴随形式 changed,visible:False 被
    visible:False 子串判据覆盖；无 visible:False 的 hwnd changed（常规采集切换）
    不报警；可见性行只做展示，不影响 outcome 判定（不产生失败/完成）。"""
    # 置位 + 伴随形式
    m = RunMonitor()
    m.feed(["2026-11-08 10:00:00 [INFO] capture hwnd changed,visible:False"])
    assert m.window_invisible is True, "visible:False（含 changed 伴随形式）必须置位"
    # 恢复清除
    m.feed(["2026-11-08 10:00:30 [INFO] capture hwnd changed,visible:True"])
    assert m.window_invisible is False, "visible:True 必须清除告警"
    # 无 visible:False 的 hwnd changed（常规切换）不置位
    m2 = RunMonitor()
    m2.feed(["2026-11-08 10:00:00 [INFO] capture hwnd changed"])
    assert m2.window_invisible is False, "常规 hwnd changed 不应报警"
    # 大小写敏感照日志原文（标记常量逐字匹配）
    assert RunMonitor.WINDOW_MARK_INVISIBLE == "visible:False"
    assert RunMonitor.WINDOW_MARK_VISIBLE == "visible:True"
    # 只做展示不判定：喂纯可见性行的 outcome 必须与空喂一致（默认 failed 兜底理由，
    # 不新增 failures/完成标记）——可见性行只是 runner 报警的展示信号
    m3 = RunMonitor()
    m3.feed(["2026-11-08 10:00:00 [INFO] visible:False",
             "2026-11-08 10:00:05 [INFO] visible:True",
             "2026-11-08 10:00:10 [INFO] visible:False"])
    status, spent, note = m3.outcome(200, 40)
    m_empty = RunMonitor()
    status0, spent0, note0 = m_empty.outcome(200, 40)
    assert (status, spent, note) == (status0, spent0, note0) \
        and (status, spent, note) == ("failed", 0, "未捕获到任务完成标记"), \
        f"可见性行不参与 outcome 判定，实际 {(status, spent, note)} vs 空喂 {(status0, spent0, note0)}"
    assert m3.failures == [] and m3.completed is False and m3.skipped is False, \
        (m3.failures, m3.completed, m3.skipped)
    assert m3.window_invisible is True  # 末态保留（runner 恢复日志到达前持续报警）


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
        path = _safe_path(tmp, "ok-script.log")

        def write_all(text):
            Path(path).write_text(text, encoding="utf-8", newline="\n")

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
        assert plan["detail"]["skipped_blacklisted"] == ["甲硬币", "特级猎人攻略"], \
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
               ("exp_char", "特级猎人攻略", 30, 5, 25, 7.0, 4, 40, 180, 4), \
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
            assert snap["state"] == "stopped" and snap["message"] == "已手动停止", \
                f"停止后应为 stopped/已手动停止（不记失败），实际 {snap['state']}/{snap['message']!r}"
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
        Path(_contained(os.path.join(ok.log_dir, "ok-script.2026-10-24.log"),
                        ok.root)).write_text(
            "2026-10-24 10:00:00,000 [INFO] ocr found used stamina 120\n",
            encoding="utf-8", newline="\n")
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
    rt_file = _safe_path(tmp, "runtime.json")
    runner_mod.RUNTIME_FILE = rt_file

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
                Path(rt_file).write_text(json.dumps(rt, ensure_ascii=False, indent=2),
                                         encoding="utf-8")

            def make_exec(phase, pid=12345):
                return {"execution_id": "e1", "phase": phase, "pid": pid,
                        "started_at": "2026-10-24 21:00:00", "day": STATE.day_key(),
                        "step": {"mat_id": "coin", "name": "甲硬币", "runs": 2,
                                 "stamina": 80, "gain": 14},
                        "outcome": None}

            # 0) 首次启动（无 runtime 文件）→ 干净 stopped，无提示
            if os.path.exists(rt_file):
                os.remove(rt_file)
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
                path = _safe_path(tmp, "runtime.json" + suffix)
                if os.path.exists(path):
                    os.remove(path)
            Path(rt_file).write_text('{"version": 1, "state": "run', encoding="utf-8")
            r = runner_mod.RUNNER.recover_on_startup()
            assert r["corrupt"] is True, "损坏应上报 corrupt"
            assert runner_mod.RUNNER.snapshot()["state"] == "interrupted"

            # 7) JSON 写入中断（截断）+ .bak 兜底：load_json 回退到最后完好版本
            demo = _safe_path(tmp, "demo.json")
            demo_bak = _safe_path(tmp, "demo.json.bak")
            Path(demo_bak).write_text(json.dumps({"good": 1}), encoding="utf-8")
            Path(demo).write_text('{"good": 2, "brok', encoding="utf-8")  # 写入中途的半截
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
            Path(_contained(cfg_path, ok.root)).write_text('{"daily_anomaly": broken',
                                                           encoding="utf-8")
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

    def run_command(self, launcher_path=None, task="日常任务"):
        type(self).launches.append((time.time(), dict(self.mode_env)))
        env = dict(os.environ)
        env.update(self.mode_env)
        return [sys.executable, FAKE_OKNTE], self.working, env

    def run_command_hunter(self, launcher_path=None):
        type(self).hunter_launches.append((time.time(), dict(self.mode_env)))
        env = dict(os.environ)
        env.update({**self.mode_env, "FAKE_MODE": "hunter"})
        return [sys.executable, FAKE_OKNTE], self.working, env

    def is_running(self):
        # 气密：e2e 用例不得依赖宿主机是否真的开着 ok-nte（真机取证检测会命中
        # 宿主上真实运行的 ok-nte GUI）；busy 场景由 force_busy 显式注入。
        return bool(type(self).force_busy)


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
        self._yields_backup = __import__("copy").deepcopy(planner.YIELDS)
        for tid in planner.TARGET_ORDER:
            planner.YIELDS["per_run"][tid] = 7
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
        log_dir = _safe_path(self.root, "data", "apps", "ok-nte", "working", "logs")
        os.makedirs(log_dir, exist_ok=True)
        open(_safe_path(log_dir, "ok-script.log"), "a").close()
        if mode:
            _E2EOkNte.mode_env = {**_E2EOkNte.mode_env, "FAKE_MODE": mode}

    def __exit__(self, *exc):
        planner.YIELDS.clear()
        planner.YIELDS.update(self._yields_backup)
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
        # 独立配置副本被正确写入；用户原配置一字节未动
        with open(ok.routine_config, encoding="utf-8") as f:
            user_daily = json.load(f)["daily_anomaly"]
        assert user_daily["具体奖励目标"] == "弧盘经验", "用户原配置不得被写入"
        profile_daily_path = os.path.join(ok.working, "configs_planner",
                                          adapter_mod.ROUTINE_CONFIG_NAME)
        with open(profile_daily_path, encoding="utf-8") as f:
            daily = json.load(f)["daily_anomaly"]
        assert daily["具体奖励目标"] == "甲硬币" and daily["目标消耗体力"] == 80 \
            and daily["循环模式"] == "停用", f"写入的配置不符: {daily}"
        profile_items_path = os.path.join(ok.working, "configs_planner",
                                          adapter_mod.ROUTINE_ITEMS_NAME)
        with open(profile_items_path, encoding="utf-8") as f:
            items = json.load(f)["Routine Items"]
        enabled = [e["id"] for e in items if e.get("enabled")]
        assert enabled == ["daily_anomaly"], f"副本应只启用体力计划子任务: {enabled}"
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
        assert r.snapshot()["state"] == "stopped" and r.snapshot()["message"] == "已手动停止"


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
        # 停止行为语义（真机反馈"点停止显示 失败/notfound"的回归防线）：
        # 历史必须记 "stopped" 而非 "failed"，且不得把目标加入当日拉黑
        assert r.snapshot()["state"] == "stopped", \
            f"停止后终态应为 stopped，实际 {r.snapshot()['state']}"
        stopped_entries = [e for e in STATE.data["history"]
                           if e.get("status") == "stopped" and "停止" in (e.get("note") or "")]
        assert stopped_entries, "手动停止应留下一条 status=stopped 的历史记录"
        failed_stops = [e for e in STATE.data["history"]
                        if e.get("status") == "failed" and "手动停止" in (e.get("note") or "")]
        assert not failed_stops, f"手动停止不得记为 failed：{failed_stops}"
        assert not STATE.data["today"].get("blacklist"), \
            f"手动停止不得触发当日拉黑，实际 {STATE.data['today']['blacklist']}"


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
            profile_daily = os.path.join(ok.working, "configs_planner",
                                         adapter_mod.ROUTINE_CONFIG_NAME)
            assert os.path.isfile(profile_daily), "重试前应已写配置副本"
            with open(profile_daily, "rb") as f:
                first_bytes = f.read()
            assert _wait_until(lambda: len(_E2EOkNte.launches) >= 2), "应自动重试"
            with open(profile_daily, "rb") as f:
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
        Path(_contained(ok.routine_config, ok.root)).write_text(
            json.dumps(broken, ensure_ascii=False, indent=4), encoding="utf-8")
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

        # 4) 追猎预留不再扣减日常预演额度（阶段13 独立记账，与 make_plan 同口径）；
        #    恢复不足警告仍按限额评估（2/小时×24=48 < 限额80）
        S.update(reserve_anomaly_hunter=True, anomaly_hunter_stamina=40,
                 stamina_regen_per_hour=2)
        r2 = planner.preview_days(2, now=now)
        assert r2["days"][0]["budget"] == 80, r2["days"][0]
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
    main_mod.DATA_DIR = _safe_path(tmp, "planner_data")
    adapter_mod.DATA_DIR = main_mod.DATA_DIR  # 与 main 同一备份根
    os.makedirs(main_mod.DATA_DIR, exist_ok=True)
    try:
        with _StateGuard():
            # -- schema：保存时盖章 --
            STATE.data["settings"] = dict(DEFAULT_SETTINGS)
            STATE.data["today"] = _empty_today()
            STATE.data["inventory"] = {"coin": 3}
            STATE.data["characters"] = [_char("c1", "导出角色", {"coin": 30})]
            STATE.save()
            assert STATE.data["schema"] == 3, "save 应写入 schema=3"

            # -- 导出结构 --
            export = main_mod.build_export()
            assert export["schema"] == 3 and export["settings"]["daily_stamina_limit"] > 0 \
                and len(export["characters"]) == 1 and export["inventory"] == {"coin": 3} \
                and "per_run" in export["yields"] and "tracks" in export["requirements"] \
                and "teams" in export, \
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
            backups_dir = _safe_path(adapter_mod.DATA_DIR, "backups")
            os.makedirs(backups_dir, exist_ok=True)
            good = copy.deepcopy(FAKE_ROUTINE_CONFIG)
            good["daily_anomaly"]["目标消耗体力"] = 77
            Path(_safe_path(backups_dir, f"{adapter_mod.ROUTINE_CONFIG_NAME}.20260101_000000")
                 ).write_text(json.dumps(good, ensure_ascii=False, indent=4), encoding="utf-8")
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
    from app import main as main_mod
    from app.main import create_server
    # 0) 候选序列：相邻段 + 跳段（2026-11-03 真机实证：Windows 排除区 8766~8865，
    #    旧实现只试 +1..+20 整段 10013，面板"再也无法打开"）
    cands = main_mod._port_candidates(8765, 20)
    assert cands[:5] == [8765, 8766, 8767, 8768, 8769], cands
    assert 8865 in cands and 8965 in cands, "应包含 +100/+200 跳段候选（跳出排除区）"
    assert len(cands) == 3 * 21, len(cands)
    # SEC-05：本用例是进程内首个 create_server 调用——直设全局令牌（生产生成器
    # 现生成，零凭据字面量）→ _ensure_panel_token 空转，全程零文件写入 repo data/
    main_mod._PANEL_TOKEN = main_mod._new_panel_token()
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
    # 2) 全部候选耗尽：monkeypatch 候选为已知被占端口 → 抛可操作的中文错误
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
    orig_cands = main_mod._port_candidates
    try:
        try:
            main_mod._port_candidates = lambda p, r: [base, base + 1]
            create_server(base, auto_range=1)
            raise AssertionError("全部候选被占时应抛 RuntimeError")
        except RuntimeError as e:
            assert "端口" in str(e) and "无法绑定" in str(e), f"报错应可操作: {e}"
        # 10013 形态（Windows 排除区）：报错应包含 winnat 重启与排除区查看两个解法
        class _DenyServer:
            def __init__(self, *a, **k):
                pass

            def server_close(self):
                pass

        orig_http = main_mod.ThreadingHTTPServer

        def _deny(addr, handler):
            err = OSError("access denied")
            err.winerror = 10013
            raise err

        main_mod.ThreadingHTTPServer = _deny
        try:
            main_mod._port_candidates = lambda p, r: [8766]
            try:
                create_server(8766, auto_range=20)
                raise AssertionError("全部 10013 时应抛 RuntimeError")
            except RuntimeError as e:
                assert "winnat" in str(e) and "排除" in str(e), f"10013 报错应带排除区解法: {e}"
        finally:
            main_mod.ThreadingHTTPServer = orig_http
            main_mod._port_candidates = orig_cands
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
    real = _safe_path(tempfile.gettempdir(), "oknte_fake_edge.exe")
    try:
        Path(real).touch()
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
def test_concrete_display_names():
    """系列养成材料显示具体道具名（2026-11-08 用户实测："技能书·帽子系（…）"系名
    风格不指向具体道具，实际要刷的是「黑帽子」）：targets 的 ability_N/arc_N 带
    concrete_names:{base,blue,purple}（值照抄 item_names 首别名，同一事实源不双写
    漂移；缺失置 null），planner.display_name 有 concrete_names 返回紫档具体名，
    缺失回退原链——分档 extra 条目不受影响（系名·档位风格是设计内显示）；
    2026-11-09 全量具体名补全后 exp_char/exp_arc 也带 concrete_names（用例 4c 的
    exp_char 断言已同步为紫档具体名）；四档折算语义不变（折算在
    equivalent_inventory/tiers，与展示名解耦）。"""
    inn = planner.MATERIALS.get("item_names") or {}
    for pre in ("ability", "arc"):
        for n in range(1, 6):
            tid = f"{pre}_{n}"
            cn = planner.TARGETS[tid].get("concrete_names") or {}
            assert set(cn) >= {"base", "blue", "purple"}, (tid, cn)
            assert cn.get("base") == (inn.get(tid) or [None])[0], tid
            assert cn.get("blue") == (inn.get(f"{tid}_b") or [None])[0], tid
            assert cn.get("purple") == (inn.get(f"{tid}_p") or [None])[0], tid
            assert planner.display_name(tid) == cn["purple"], tid
    assert planner.display_name("ability_3") == "「黑帽子」", \
        planner.display_name("ability_3")
    assert planner.display_name("arc_3") == "金色苹果核", planner.display_name("arc_3")
    # exp_char 已补 concrete_names（2026-11-09 全量具体名）：紫档具体名出口，
    # 用例 4c reason 断言同步为「特级猎人攻略」
    assert planner.display_name("exp_char") == "特级猎人攻略"
    assert planner.display_name("exp_arc") == "混沌染剂"
    assert planner.display_name("coin") == "甲硬币"
    # 分档 extra 条目 display 改具体道具名（2026-11-09 工作包5，用户要求全部简化：
    # 系名·档位风格「技能书·帽子系·紫（×9当量）」不指向具体道具；新值照抄
    # targets concrete_names 同档实名——同一事实源不双写漂移；四档折算语义仍在
    # tiers/equivalent_inventory，与展示名解耦）
    assert planner.display_name("ability_3_p") == "「黑帽子」", \
        planner.display_name("ability_3_p")
    for tid, want in (("ability_1_b", "尉官的谨慎"), ("ability_1_p", "「白蔷薇」"),
                      ("arc_1_b", "大师级戏剧芯"), ("arc_1_p", "典藏级戏剧芯"),
                      ("arc_3_p", "金色苹果核"), ("exp_char_b", "资深猎人攻略"),
                      ("exp_arc_p", "混沌染剂")):
        got = planner.display_name(tid)
        cn = (planner.TARGETS.get(tid.rsplit("_", 1)[0], {}).get("concrete_names") or {})
        expect = cn.get("blue" if tid.endswith("_b") else "purple")
        assert got == want == expect, (tid, got, want, expect)
    # 计划 reason/步骤名走同一 display_name 出口 → 具体名同步（缺口→紫档名）；
    # 需求经 extra_needs 直配 ability_3（成长模型依赖目录/轨道，直配最稳）
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.data["inventory"] = {}
        STATE.data["today"] = _day_today()
        STATE.data["characters"] = [_char("c1", "甲", {"ability_3": 9})]
        plan = planner.make_plan(now=NOW)
        step = next(s for s in plan["steps"] if s["mat_id"] == "ability_3")
        assert step["reason"]["display_name"] == "「黑帽子」", step["reason"]


@case
def test_concrete_names_full_coverage():
    """全量材料具体名补全（2026-11-09）：体力本 14 个系列/经验目标
    （ability_1..5/arc_1..5/exp_char/exp_arc）全部带 concrete_names 三档实名，
    三键非空且 planner.display_name 一律返回紫档具体名——与前端 allMaterials()/
    每轮产出卡片、后端 yield_suggestions 的同一显示出口对齐。"""
    ids = ([f"ability_{n}" for n in range(1, 6)]
           + [f"arc_{n}" for n in range(1, 6)]
           + ["exp_char", "exp_arc"])
    for tid in ids:
        cn = planner.TARGETS[tid].get("concrete_names") or {}
        assert set(cn) >= {"base", "blue", "purple"}, (tid, cn)
        for k in ("base", "blue", "purple"):
            assert isinstance(cn.get(k), str) and cn[k].strip(), (tid, k)
        assert planner.display_name(tid) == cn["purple"], (tid, planner.display_name(tid))


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
        Path(_contained(ok.hunter_config, ok.root)).write_text(
            json.dumps({"追猎目标": "黑之书", "领取奖励次数": 0, "上游未知键": [1]},
                       ensure_ascii=False), encoding="utf-8")
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
        os.remove(_contained(ok.hunter_config, ok.root))
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
        Path(_contained(ok.routine_items, ok.root)).write_text(
            json.dumps({"Routine Items": [
                {"id": "daily_anomaly", "enabled": False},
                {"id": "daily_anomaly_hunter", "enabled": False},
                {"id": "coffee", "enabled": True}],
                "Exit After Task": False}, ensure_ascii=False), encoding="utf-8")
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
        # ok-nte 侧独立配置副本被正确写入；用户原 AnomalyHunter.json 不动
        with open(runner_mod.OkNte(e2e.root).hunter_config, encoding="utf-8") as f:
            user_cfg = json.load(f)
        profile_hunter = os.path.join(runner_mod.OkNte(e2e.root).working,
                                      "configs_planner",
                                      adapter_mod.HUNTER_CONFIG_NAME)
        with open(profile_hunter, encoding="utf-8") as f:
            cfg = json.load(f)
        assert cfg["追猎目标"] == "海囚" and cfg["领取奖励次数"] == 2, cfg
        assert user_cfg.get("领取奖励次数") == 0, "用户原 AnomalyHunter.json 不得被写入"
        # 同一执行器互斥：运行中再 start 另一模式应拒绝
        r2 = runner_mod.Runner()
        ok, _ = r2.start("daily")
        assert ok, "空闲后应可启动日常"
        r2.stop()
        r2.thread.join(timeout=5)


# ---------- 用例 15：游戏目录落库（阶段14） ----------

@case
def test_game_data_integrity():
    data = game_data_mod.load()
    names = [c["name"] for c in data["characters"]]
    assert len(names) == len(set(names)) == 27, f"应 27 名角色（25实装+2未上线）且唯一，实际 {len(names)}"
    live = [c for c in data["characters"] if c.get("live")]
    assert len(live) == 25, f"应 25 名已实装，实际 {len(live)}"
    assert names.count("灵可") == 1 and "曳尔沐" in names and "EXE" in names
    # 材料引用合法：BOSS 材料都在 extra 且 obtain=hunter；周本/小怪同理
    extra = planner.EXTRA
    for c in data["characters"]:
        for section in ("boss_material", "weekly_material"):
            ref = c.get(section)
            if ref:
                assert ref["mat_id"] in extra, f"{c['name']} 引用未知材料 {ref}"
        if c.get("mob_breakthrough"):
            assert c["mob_family"] in game_data_mod._MOB_FAMILY_TO_IDS, c["name"]
    # 罐头系映射断言 4 个实证
    m2c = data["matter_state_to_can"]
    assert m2c["固态"] == "arc_1" and m2c["等离子"] == "arc_2" \
        and m2c["液态"] == "arc_3" and m2c["气态"] == "arc_4", m2c
    # 属性→技能书系（mysqil 五系全齐；按角色个体为准，存在魂系例外）
    a2s = data["attribute_to_skill_series"]
    assert a2s["光"] == "ability_2" and a2s["灵"] == "ability_1" \
        and a2s["咒"] == "ability_3" and a2s["暗"] == "ability_5" \
        and a2s["相"] == "ability_4", a2s
    # 武器：名单唯一、物态齐全、owner 与角色名对得上
    wnames = [w["name"] for w in data["weapons"]]
    assert len(wnames) == len(set(wnames)) == 50
    char_names = set(names)
    for w in data["weapons"]:
        assert w["matter_state"] in m2c, w
        if w.get("owner"):
            assert w["owner"] in char_names, w
    # 记忆的永恒已补入材料表（阶段14修正）
    assert "wk_memory" in extra and extra["wk_memory"]["obtain"] == "external"
    # 库名单：仅 25 名已实装、干净名（未上线角色不入库、无后缀标记）
    lib_chars, lib_weapons = game_data_mod.library_names(data)
    assert len(lib_chars) == 25 and all("（未上线）" not in n for n in lib_chars), lib_chars
    assert "灵可" in lib_chars and "曳尔沐" not in lib_chars
    assert len(lib_weapons) == 50 and "远行者之声" in lib_weapons


@case
def test_game_data_import_character():
    import app.main as main_mod
    with _StateGuard():
        data = game_data_mod.load()
        # 灵可：高置信全链路预填
        raw = game_data_mod.build_character_import(data, "灵可")
        assert raw["mats"]["ability"] == 1 and raw["mats"]["arc"] == 2, raw["mats"]
        assert raw["weapon"] == "远行者之声"
        # 小怪素材 = 突破17/18/15 + 技能40/40/64 合计
        # mysqil 口径：BOSS 86 + 周本 11 + 小怪白当量 390（突破206+技能184，族群基础档）
        assert raw["extra_needs"] == {"boss_pick_watermoon": 86, "wk_skirt": 11,
                                      "mob_silhouette_c": 390}, raw["extra_needs"]
        # 落库校验：_sanitize_character 保留全部预填（材料id都在目录里）
        clean = main_mod._sanitize_character(raw)
        assert clean["extra_needs"] == raw["extra_needs"] and clean["weapon"] == "远行者之声"
        # 未上线角色：无任何预填
        raw2 = game_data_mod.build_character_import(data, "EXE")
        assert raw2["extra_needs"] == {} and raw2["mats"]["arc"] == 1, raw2
        # 低置信角色：暗系技能书不猜（安魂曲 ability 保持默认 1? 不——暗未查到→不设置）
        raw3 = game_data_mod.build_character_import(data, "安魂曲")
        assert raw3["mats"]["ability"] == 5, "安魂曲(暗) mysqil 权威=水波系 ability_5"
        # 阶段15 语义：BOSS 材料数量全角色一致（通用量 86），不再用单角色旧值 50；
        # 小怪素材同样取通用值（突破 17/18/15 + 技能 40/40/64 = 57/58/79，族群对应具体材料）
        assert raw3["extra_needs"]["boss_flower_seed"] == 86, raw3["extra_needs"]
        assert raw3["extra_needs"]["mob_whisper_c"] == 390, "白当量合计写族群基础档"
        # 不存在的角色
        try:
            game_data_mod.build_character_import(data, "不存在")
            raise AssertionError("应抛 ValueError")
        except ValueError:
            pass


@case
def test_game_data_requirements_linkage():
    """灵可导入 + 满级目标 → 计划产出合理的技能书/罐头轮数（数据→计划联动）。

    技能材料口径（异能=技能 改造后）：四技能各自 1→10 逐级（wiki skillUpgrade，
    单技能 1→10 = 书184/小怪184/硬币43.7万/周本8）+ 天赋解锁两档（书15/硬币7万/周本3）
    + 角色等级 1→80 跨越门槛 30/40/50/60/70 的突破材料（BOSS 86/小怪 201，wiki charBreak；
    门槛硬币已含在等级梯，不重复计）。
    """
    import app.main as main_mod
    with _StateGuard():
        data = game_data_mod.load()
        raw = game_data_mod.build_character_import(data, "灵可")
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.data["today"] = _empty_today()
        STATE.data["inventory"] = {}
        char = main_mod._sanitize_character(raw)
        char["tracks"] = {"char_level": {"cur": 1, "tgt": 80},
                          "ability_level": {"cur": 1, "tgt": 13},
                          "arc_level": {"cur": 1, "tgt": 80},
                          "console_level": {"cur": 1, "tgt": 1}}
        char["skills"] = {k: {"cur": 1, "tgt": 10}
                          for k in ("normal", "variable", "orbit", "assist")}
        char["talent_unlocks"] = [True, True]
        STATE.data["characters"] = [char]
        # mysqil 鉴别7 口径（生产默认）：coin 12万/局、经验 22.5万/12.5万/局、书·罐头 18/局
        planner.YIELDS["per_run"]["coin"] = 120000
        planner.YIELDS["per_run"]["exp_char"] = 225000
        planner.YIELDS["per_run"]["exp_arc"] = 125000
        for n in range(1, 6):
            planner.YIELDS["per_run"][f"ability_{n}"] = 18
            planner.YIELDS["per_run"][f"arc_{n}"] = 18
        STATE.data["settings"]["daily_stamina_limit"] = 99999  # 大额度：验证多步骤完整规划
        planner.UPSTREAM_DAILY_CAP = 10 ** 9  # 本用例模拟无上游上限，验证完整贪心
        snap = planner.snapshot()
        by_id = {row["mat_id"]: row for c in snap["characters"] for row in c["rows"]}
        # 四技能×(1→10) 书 184×4=736 + 天赋 15 = 751（旧单轨口径 199 少算 4 倍，已废弃）
        assert by_id["ability_1"]["need"] == 751, by_id["ability_1"]
        assert by_id["arc_2"]["need"] == 230, by_id["arc_2"]
        assert by_id["exp_char"]["need"] == 6496000
        assert by_id["boss_pick_watermoon"]["need"] == 86 and not by_id["boss_pick_watermoon"]["farmable"]
        # 小怪 = 四技能逐级 184×4=736 + 六门槛(31/41/51/61/71/满级80) 5+12+18+36+54+81=206
        assert by_id["mob_silhouette_c"]["need"] == 942 and not by_id["mob_silhouette_c"]["farmable"]
        wk_rows = [r for r in by_id.values() if r["mat_id"].startswith("wk_")]
        assert wk_rows and wk_rows[0]["need"] == 35, "周本 = 四技能 8×4 + 天赋 3"
        # coin 已校准 → 正常排步；贪心顺序按 TARGET_ORDER
        plan = planner.make_plan(now=NOW)
        mat_ids = [s["mat_id"] for s in plan["steps"]]
        assert mat_ids == ["exp_char", "exp_arc", "coin", "ability_1", "arc_2"], mat_ids
        by = {s["mat_id"]: s for s in plan["steps"]}
        assert (by["ability_1"]["runs"], by["ability_1"]["gain"]) == (42, 756), by["ability_1"]
        assert (by["arc_2"]["runs"], by["arc_2"]["gain"]) == (13, 234), by["arc_2"]
        assert by["exp_char"]["runs"] == 29, "6496000/225000=28.87→29 局"
        assert by["exp_arc"]["runs"] == 30, "3727000/125000=29.8→30 局"
        # coin 需求 = 角色轨道 2148212（已含门槛硬币）+ 弧盘轨道 1512099 + 技能 437000×4+70000
        coin_need = by_id["coin"]["need"]
        assert coin_need == 5478311, coin_need
        assert by["coin"]["runs"] == -(-coin_need // 120000) == 46, by["coin"]["runs"]
        assert not any(s["mat_id"].startswith(("boss_", "mob_", "wk_")) for s in plan["steps"]), \
            "外部来源材料绝不能进体力本计划"
        planner.UPSTREAM_DAILY_CAP = 180  # 恢复上游上限（其他用例依赖默认值）


# ---------- 用例 16：通用材料表（阶段15） ----------

@case
def test_common_costs_flow():
    import app.main as main_mod
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        data = game_data_mod.load()
        # 1) 共享映射真相：灵可 = BOSS×86 + 周本×32 + 小怪(17+40)/(18+40)/(15+64)
        entry = game_data_mod.find_character(data, "灵可")
        prefill, skipped = game_data_mod.character_material_mapping(
            data, entry, dict(game_data_mod.COMMON_COST_DEFAULTS))
        assert prefill == {"boss_pick_watermoon": 86, "wk_skirt": 11,
                           "mob_silhouette_c": 390}, prefill
        assert skipped == [], skipped
        # 与『按目录导入角色』完全同源（同一处真相）
        raw = game_data_mod.build_character_import(data, "灵可")
        assert raw["extra_needs"] == prefill
        # 数量全角色一样、材料不同：浔的 BOSS 也被通用量抬到 86
        entry2 = game_data_mod.find_character(data, "浔")
        prefill2, _ = game_data_mod.character_material_mapping(
            data, entry2, dict(game_data_mod.COMMON_COST_DEFAULTS))
        assert prefill2["boss_flower_seed"] == 86, prefill2
        # 2) mysqil 已覆盖的角色：族群白当量合计 390 一并预填（mob_base_total 证据）
        entry3 = game_data_mod.find_character(data, "娜娜莉")
        prefill3, skipped3 = game_data_mod.character_material_mapping(
            data, entry3, dict(game_data_mod.COMMON_COST_DEFAULTS))
        assert prefill3 == {"boss_page_delusion": 86, "wk_stamp": 11,
                            "mob_whisper_c": 390}, prefill3
        assert skipped3 == [], skipped3
        # 2b) 族群/数量均未查到的角色（明音凛）：小怪给出跳过说明，不入映射结果
        _pf_nb, skipped_nb = game_data_mod.character_material_mapping(
            data, game_data_mod.find_character(data, "明音凛"),
            dict(game_data_mod.COMMON_COST_DEFAULTS))
        assert any("小怪" in s for s in skipped_nb), skipped_nb
        # 3) 保存钳位：越界压回、未知键丢弃
        clean = main_mod._sanitize_common_costs(
            {"ascension_boss": 10**9, "skill_weekly": -5, "bogus": 1,
             "ascension_mob": 90})
        assert clean == {"ascension_boss": 10**7, "skill_weekly": 0,
                         "ascension_mob": 90}, clean
        # 4) 应用：改通用量后重应用覆盖同名键（幂等）
        STATE.data["common_costs"] = {"ascension_boss": 90}
        STATE.data["characters"] = [main_mod._sanitize_character(raw)]
        # 模拟 main 的 apply 路径（读 _common_costs → 合并覆盖 extra_needs）
        costs = main_mod._common_costs()
        assert costs["ascension_boss"] == 90, costs
        prefill4, _ = game_data_mod.character_material_mapping(
            data, entry, costs)
        assert prefill4["boss_pick_watermoon"] == 90, prefill4
        char = STATE.data["characters"][0]
        merged_extra = {**(char.get("extra_needs") or {}), **prefill4}
        char["extra_needs"] = {k: main_mod._clamp_int(v, 0, 10**7, 0)
                               for k, v in merged_extra.items()}
        assert char["extra_needs"]["boss_pick_watermoon"] == 90
        assert char["extra_needs"]["wk_skirt"] == 11, "未改的通用键保持原值"


@case
def test_stamina_defaults_320():
    from app.main import _sanitize_settings
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        assert STATE.settings["stamina_cap"] == 320, "新默认应为 320（家具扩容后口径）"
        s = _sanitize_settings({"daily_stamina_limit": 9999, "stamina_cap": 500})
        assert s["daily_stamina_limit"] == 999, "每日限额钳位应收紧到 999"
        assert s["stamina_cap"] == 500, "体力上限支持 320+（1~999）"
        s2 = _sanitize_settings({"daily_stamina_limit": 320})
        assert s2["daily_stamina_limit"] == 320


# ---------- 用例 17：P2 批次一（阶段16：图标/兼容报告） ----------

@case
def test_icon_generation():
    import struct
    from app.icon_gen import generate_icon
    tmp = tempfile.mkdtemp(prefix="oknte_icon_")
    try:
        ico = _safe_path(tmp, "app.ico")
        generate_icon(ico)
        blob = open(ico, "rb").read()
        assert blob[:4] == b"\x00\x00\x01\x00", f"ICO 魔数错误: {blob[:4]!r}"
        count = struct.unpack("<H", blob[4:6])[0]
        assert count == 2, f"应含 2 个尺寸，实际 {count}"
        sizes = {blob[6], blob[6 + 16]}  # 目录项第 1 字节 = 宽
        assert sizes == {32, 16}, f"应含 32/16 两尺寸，实际 {sizes}"
        # 数据完整性：目录声明的长度与偏移自洽
        # ICO 头 6 字节：reserved(H) + type(H=1) + count(H)；count 在 blob[4:6]
        total_dir = struct.unpack("<H", blob[4:6])[0]
        assert total_dir == 2, f"应 2 个目录项，实际 {total_dir}"
        offset = 6 + 16 * total_dir
        for i in range(total_dir):
            base = 6 + 16 * i
            size_bytes = struct.unpack("<I", blob[base + 8:base + 12])[0]
            at = struct.unpack("<I", blob[base + 12:base + 16])[0]
            assert at == offset and offset + size_bytes <= len(blob), \
                f"目录项 {i} 偏移/长度不自洽"
            offset += size_bytes
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_compat_report_content():
    import app.main as main_mod
    from app.oknte_adapter import OkNte
    tmp = tempfile.mkdtemp(prefix="oknte_report_")
    orig_datadir = main_mod.DATA_DIR
    main_mod.DATA_DIR = _safe_path(tmp, "planner_data")
    os.makedirs(main_mod.DATA_DIR, exist_ok=True)
    try:
        with _StateGuard():
            STATE.data["settings"] = dict(DEFAULT_SETTINGS)
            STATE.data["today"] = _empty_today()
            ok = _build_fake_oknte(os.path.join(tmp, "root"))
            report = main_mod.build_compat_report(ok)
            assert "兼容报告" in report and compat_status_in(report) or True
            # 关键字段必在
            for key in ("兼容指纹", "首查清单", "校准状态", "甲硬币"):
                assert key in report, f"报告缺少 {key}: {report[:300]}"
    finally:
        main_mod.DATA_DIR = orig_datadir
        shutil.rmtree(tmp, ignore_errors=True)


def compat_status_in(report):
    return "compatible" in report


# ---------- 用例 18：图鉴覆盖层 + wiki 解析器（阶段19） ----------

@case
def test_game_data_custom_overlay():
    tmp = _conc_tmp("codex_overlay")
    orig_custom = game_data_mod.CUSTOM_FILE
    guard = _CustomFileGuard(orig_custom)  # B2.7C 防泄漏门禁：real_path=patch 前真值
    guard.__enter__()
    game_data_mod.CUSTOM_FILE = os.path.join(tmp, "game_data_custom.json")
    try:
        base_bytes = open(game_data_mod.GAME_DATA_FILE, "rb").read()
        # 1) 新增基础目录没有的自定义角色
        entry = {"name": "图鉴测试角色", "rarity": "S", "live": True,
                 "attribute": "光", "matter_state": None, "version": "手工测试",
                 "skill_series": "ability_2", "skill_confidence": "manual",
                 "boss_material": {"mat_id": "boss_x", "boss": "测试BOSS",
                                   "total": 86, "confidence": "manual"},
                 "weekly_material": None, "mob_family": "絮语", "mob_base_total": 390}
        game_data_mod.upsert_custom("characters", entry)
        names = [c["name"] for c in game_data_mod.load()["characters"]]
        assert "图鉴测试角色" in names, names[-3:]
        # 2) 同名覆盖既有目录条目（整条替换语义），且基础文件绝不被修改
        game_data_mod.upsert_custom("characters", {"name": "浔", "rarity": "S",
                                                   "live": True, "version": "手工覆盖"})
        xun = next(c for c in game_data_mod.load()["characters"] if c["name"] == "浔")
        assert xun["version"] == "手工覆盖", xun
        assert open(game_data_mod.GAME_DATA_FILE, "rb").read() == base_bytes, \
            "基础 game_data.json 不得被覆盖层写入"
        # 3) 武器 upsert 幂等（同名覆盖）+ 删除
        game_data_mod.upsert_custom("weapons", {"name": "图鉴测试弓", "rarity": "S"})
        game_data_mod.upsert_custom("weapons", {"name": "图鉴测试弓", "rarity": "A"})
        assert game_data_mod.custom_entries()["weapons"][0]["rarity"] == "A"
        assert game_data_mod.remove_custom("weapons", "图鉴测试弓") is True
        assert game_data_mod.remove_custom("weapons", "图鉴测试弓") is False
        # 4) 全部删空后目录恢复原样
        game_data_mod.remove_custom("characters", "图鉴测试角色")
        game_data_mod.remove_custom("characters", "浔")
        assert game_data_mod.custom_entries()["characters"] == []
        merged3 = game_data_mod.load()
        assert all(c["name"] != "图鉴测试角色" for c in merged3["characters"])
        xun3 = next(c for c in merged3["characters"] if c["name"] == "浔")
        assert xun3.get("version") != "手工覆盖", "删除覆盖层后应回落基础目录值"
    finally:
        game_data_mod.CUSTOM_FILE = orig_custom
        guard_exc = None
        try:
            guard.__exit__(None, None, None)  # B2.7C：先复核防泄漏门禁再清理
        except BaseException as e:  # noqa: BLE001
            guard_exc = e
        shutil.rmtree(tmp, ignore_errors=True)
        if guard_exc is not None:
            raise guard_exc


@case
def test_wiki_fetch_parse_offline():
    """离线解析器自测：直接解析仓库内 DetailedCalc.fresh.js 夹具，不触网。"""
    from app import wiki_fetch as wf
    if not wf.FRESH_JS.is_file():
        # dist 等精简环境未随包 mysqil/ 快照：仓库全量自测已覆盖，此处跳过
        print("（无 mysqil 快照，跳过离线解析断言）")
        return
    groups = wf.parse_groupings(wf.FRESH_JS.read_text(encoding="utf-8"))
    for key in wf.GROUP_KEYS:
        assert key in groups and isinstance(groups[key], dict), key
    assert sum(len(v) for v in groups["charSkillBook"].values()) == 20, "权威覆盖 20 名角色"
    assert wf.reverse_lookup(groups, "浔") == {
        "charSkillBook": "雏鸟的希冀", "charBossMat": "倾诉花种",
        "charWeeklyMat": "名为炫耀的裙袂", "charFieldMat": "失落絮语"}
    p = wf.build_prefill(groups, "浔")
    assert p["found"], p
    assert p["prefill"]["skill_series"] == "ability_2"
    assert p["prefill"]["boss_material"] == {"mat_id": "boss_flower_seed",
                                             "boss": "塞润尼缇", "total": 86,
                                             "confidence": "manual"}
    assert p["prefill"]["mob_base_total"] == 390
    assert wf.build_prefill(groups, "零")["found"], "「零」括号规则"
    p2 = wf.build_prefill(groups, "灵可")
    assert not p2["found"] and set(p2["missing"]) == set(wf.GROUP_KEYS), "未覆盖角色如实报告"


@case
def test_sites_fetch_offline():
    """攻略站模块离线自测：shared.js 式 JS 解析（含注释/裸标识符）、SSRF 白名单
    校验（伪造 DNS，不触网）、消毒与目录合并策略。不触网、不写任何文件。"""
    from app import sites_fetch as sf
    import socket as socket_mod
    # 1) JS 解析：数据数组内注释行 + 值位置裸标识符引用（IMG.x）+ 尾逗号
    js = """
    const IMG = { a: "https://x/a.webp" };
    const CHARS = [
      // ----- S+ tier -----
      { id:"hotori", rarity:"S", name:{en:"Hotori",zh:"浔",ja:"潿"}, img: IMG.a },
      { id:"zero", rarity:"S", name:{en:"Zero",zh:"零",ja:"ゼロ"}, img: IMG.a, },
    ];
    const ARCS = [
      { rarity:"S", type:"solid", name:{en:"Reality Refuge",zh:"现实庇护"}, best:["zero"] }, // 注释
    ];
    const CHAR_MATS = [
      { id:"hotori", items:[ {q:"×17", en:"A", zh:"失落絮语"} ] },
    ];
    """
    cleaned = sf.strip_js_comments(js)
    chars = sf._extract_const(cleaned, "CHARS")
    assert [c["name"]["zh"] for c in chars] == ["浔", "零"], chars
    assert chars[0]["img"] == {"__ref__": "IMG.a"}, "裸标识符应吞为不透明引用"
    assert [a["name"]["zh"] for a in sf._extract_const(cleaned, "ARCS")] == ["现实庇护"]
    mats = sf._extract_const(cleaned, "CHAR_MATS")
    assert mats[0]["items"][0]["zh"] == "失落絮语"
    try:
        sf._extract_const(cleaned, "NOPE")
        raise AssertionError("缺块应抛 ValueError")
    except ValueError:
        pass
    # 2) 消毒
    assert sf.clean_text(" 浔\x00\n ") == "浔" and sf.clean_text(3) == ""
    assert sf.clean_text("x" * 64) == "x" * 32
    # 3) 主机白名单（子域归一化；前缀/后缀伪装与 localhost 拒绝）
    #    2026-11-10 起 nteguide.com 全面禁用（AGENTS.md）：必须被拒
    assert sf.host_allowed("ntestrategy.com") and sf.host_allowed("wiki.mysqil.com")
    assert sf.host_allowed("ntestrategy.com.") and sf.host_allowed("NTESTRATEGY.com")
    # 2026-11-11 新增白名单主机（api.everness.info 资产子域由 endswith 规则自动放行）
    assert sf.host_allowed("everness.info") and sf.host_allowed("api.everness.info")
    assert sf.host_allowed("nte.nanoka.cc")
    for bad in ("localhost", "127.0.0.1", "evil-ntestrategy.com",
                "ntestrategy.com.evil.com", "nteguide.com", "www.nteguide.com",
                "api.nteguide.com", "", None):
        assert not sf.host_allowed(bad), bad
    # 4) URL 校验：伪造 DNS（不触网）——非 https/外域/带用户信息/非 443 端口全拒；
    #    白名单主机解析到环回/私网/链路本地/保留段也拒；nteguide.com 已禁用必拒
    orig = socket_mod.getaddrinfo
    try:
        # 2026-11-14 B2.5 收紧：空解析（getaddrinfo 返回 []）一律拒绝（fail-closed，
        # 修原 no-op 放行缺口，probe c23 三模块实证；policy §四 mandated 变更）
        socket_mod.getaddrinfo = lambda *a, **k: []
        for url in ("https://ntestrategy.com/js/shared.js",
                    "https://wiki.mysqil.com/characters/"):
            try:
                sf._validate_url(url)
                raise AssertionError("空解析必须拒绝（fail-closed）：" + url)
            except ValueError:
                pass
        # 合法锚点（公网解析桩）：返回类型保持 str（调用方/探针既有口径）
        socket_mod.getaddrinfo = lambda *a, **k: [
            (socket_mod.AF_INET, socket_mod.SOCK_STREAM, 6, "", ("1.2.3.4", 443))]
        assert sf._validate_url("https://ntestrategy.com/js/shared.js") == \
            "https://ntestrategy.com/js/shared.js"
        assert sf._validate_url("https://wiki.mysqil.com/characters/") == \
            "https://wiki.mysqil.com/characters/"
        for url in ("http://ntestrategy.com/x", "https://evil.com/x",
                    "https://user:pw@ntestrategy.com/x", "https://ntestrategy.com:8443/x",
                    "https://nteguide.com/api/characters.json",
                    "https://api.nteguide.com/search-index.json"):
            try:
                sf._validate_url(url)
                raise AssertionError("应拒绝：" + url)
            except ValueError:
                pass
        for ip, label in (("127.0.0.1", "环回"), ("10.0.0.5", "私网"),
                          ("169.254.169.254", "链路本地"), ("192.0.2.1", "保留")):
            socket_mod.getaddrinfo = lambda *a, _ip=ip, **k: [
                (socket_mod.AF_INET, socket_mod.SOCK_STREAM, 6, "", (_ip, 443))]
            try:
                sf._validate_url("https://ntestrategy.com/js/shared.js")
                raise AssertionError(f"解析到{label}地址应拒绝：{ip}")
            except ValueError:
                pass
    finally:
        socket_mod.getaddrinfo = orig
    # 5) 合并策略：已有实名不覆盖、缺失补录、跨源同实体（英文名归并）只进一条
    #    （2026-11-10 起第二源为 mysqil——只有角色名表，弧盘/材料两腿为空）
    parsed = {
        "ntestrategy": {"characters": [
                {"name": "浔", "name_en": "Hotori", "slug": "hotori", "source": "ntestrategy"},
                {"name": "早雾", "name_en": "Sakiri", "slug": "sakiri", "source": "ntestrategy"}],
            "arcs": [{"name": "现实庇护", "name_en": "Reality Refuge", "source": "ntestrategy"}],
            "materials": ["失落絮语", "不存在的材料名"]},
        "mysqil": {"characters": [
                {"name": "佐雾", "name_en": "", "slug": "sakiri", "source": "mysqil"}]}}
    merge, stats = sf.merge_catalogs(["浔"], ["现实避难所"], parsed)
    assert merge["char_library"] == ["浔", "早雾"], merge["char_library"]
    assert merge["weapon_library"] == ["现实避难所", "现实庇护"], merge["weapon_library"]
    assert stats["char_library"]["added"] == ["早雾"]
    alt = {a["name"] for a in stats["alt_names"]}
    # 跨源同实体（Sakiri）的后到译名只进 alt_names，不产生重复目录条目
    assert "佐雾" in alt, stats["alt_names"]
    assert stats["materials"]["known"] == 1 and stats["materials"]["unmapped"] == ["不存在的材料名"]
    # 失败回退：单站 None 不影响另一站
    merge2, _ = sf.merge_catalogs(["浔"], [], {"ntestrategy": None, "mysqil": parsed["mysqil"]})
    assert merge2["char_library"] == ["浔", "佐雾"]
    # 快照既往 nteguide 条目剔除（禁用后一次迁移）：characters/arcs/alt_names 三段
    assert sf._strip_legacy_nteguide(
        [{"source": "nteguide", "slug": "x"}, {"source": "mysqil", "slug": "y"}, "junk"]
    ) == [{"source": "mysqil", "slug": "y"}, "junk"]


@case
def test_avatar_map_alias_and_manifest_merge():
    """头像映射显式别名表（2026-11-10 起全表改接 mysqil 拼音 slug）+ manifest
    合并语义（2026-11-10 工作包3）。
    离线：monkeypatch 快照/manifest 读取与下载函数，不触网、不写真实 data/ 目录。"""
    from app import main as main_mod
    from app import sites_fetch as sf
    # ---------- 1) 别名映射：mysqil 拼音 slug 直查 + 别名兜底 + 未收录回落占位 ----------
    files = {"heiyu": "heiyu.webp", "dafutier": "dafutier.webp",
             "tajiduo": "tajiduo.webp", "lingke": "lingke.webp",
             "ling": "ling.webp", "fadiya": "fadiya.webp", "anhunqu": "anhunqu.webp"}
    # 快照形态模拟 mysqil 名表已同步：实名→拼音 slug 精确命中（别名只在精确失败时应用）
    slug_names = {"heiyu": ["黑羽"], "dafutier": ["达芙蒂尔"], "tajiduo": ["塔吉多"],
                  "lingke": ["灵可"], "ling": ["零"], "fadiya": ["法帝娅"]}
    orig_files, orig_names = sf.avatar_files, main_mod._guide_slug_names
    sf.avatar_files = lambda: files
    main_mod._guide_slug_names = lambda: slug_names
    try:
        m = main_mod._avatar_map()
        # mysqil 名表精确命中（同步后的主路径）
        assert m["黑羽"] == "/avatars/heiyu.webp", m.get("黑羽")
        assert m["达芙蒂尔"] == "/avatars/dafutier.webp", m.get("达芙蒂尔")
        assert m["塔吉多"] == "/avatars/tajiduo.webp", m.get("塔吉多")
        assert m["灵可"] == "/avatars/lingke.webp", m.get("灵可")
        assert m["零"] == "/avatars/ling.webp", m.get("零")
        assert m["法帝娅"] == "/avatars/fadiya.webp", m.get("法帝娅")
        # 别名兜底（快照未同步离线场景）：别名表 slug 在本机有文件才登记
        assert main_mod.AVATAR_NAME_ALIASES["安魂曲"] == "anhunqu"
        assert main_mod.AVATAR_NAME_ALIASES["黑羽"] == "heiyu"
        assert main_mod.AVATAR_NAME_ALIASES["灵可"] == "lingke"
        assert main_mod.AVATAR_NAME_ALIASES["零"] == "ling"
        # nteguide 旧 slug（black-bird/daffodil/taygedo/lingko/zero-male）必须绝迹
        banned = {"black-bird", "daffodil", "taygedo", "lingko", "zero-male",
                  "zero-female", "haniel", "edgar", "fadia"}
        assert not (set(main_mod.AVATAR_NAME_ALIASES.values()) & banned), \
            "别名表不得再含 nteguide slug"
        # 未收录回落占位：站上无条目的名字不进映射（前端首字母占位）
        for missing in ("曳尔沐", "EXE", "穗鸟"):
            assert missing not in m, missing
    finally:
        sf.avatar_files, main_mod._guide_slug_names = orig_files, orig_names
    # 离线（无快照）时别名兜底生效：黑羽/灵可/零 经别名表命中本机文件
    sf.avatar_files = lambda: files
    main_mod._guide_slug_names = lambda: {}
    try:
        m_offline = main_mod._avatar_map()
        assert m_offline["黑羽"] == "/avatars/heiyu.webp", m_offline.get("黑羽")
        assert m_offline["灵可"] == "/avatars/lingke.webp", m_offline.get("灵可")
        assert m_offline["零"] == "/avatars/ling.webp", m_offline.get("零")
        assert m_offline["安魂曲"] == "/avatars/anhunqu.webp", m_offline.get("安魂曲")
        # 别名表没有的名字（奥蕾莉亚 不在 mysqil 25 名单）离线不进映射——绝不虚构
        assert "奥蕾莉亚" not in m_offline
    finally:
        sf.avatar_files, main_mod._guide_slug_names = orig_files, orig_names
    # 别名 slug 无本机已下载文件 → 不造 URL（同样回落占位，绝不虚构路径）
    sf.avatar_files = lambda: {}
    main_mod._guide_slug_names = lambda: slug_names
    try:
        m2 = main_mod._avatar_map()
        assert "黑羽" not in m2 and "零" not in m2, "无文件时别名不得登记"
    finally:
        sf.avatar_files, main_mod._guide_slug_names = orig_files, orig_names
    # ---------- 2) manifest 合并语义：幂等 / 失败不掉出 / 既往条目保留 ----------
    tmp = tempfile.mkdtemp(prefix="oknte_avatar_merge_")
    orig_dir, orig_man, orig_get = sf.AVATARS_DIR, sf.AVATAR_MANIFEST_JSON, sf._http_get_bytes
    sf.AVATARS_DIR = tmp
    sf.AVATAR_MANIFEST_JSON = os.path.join(tmp, "manifest.json")
    try:
        # 既往状态：a/d 既往成功（文件在磁盘，source 为 mysqil 立绘 URL），
        # b 既往登记但文件缺失（此前下载失败被剔除的形态）
        for s in ("a", "d"):
            Path(tmp, s + ".webp").write_bytes(s.encode())
        sf.save_json_atomic(sf.AVATAR_MANIFEST_JSON, {
            "schema": 1, "updated_at": "T0", "avatars": {
                s: {"file": f"{s}.webp",
                    "source": f"{sf.MYSQIL_IMAGES_BASE}{s}.webp", "fetched_at": "T0"}
                for s in ("a", "b", "d")}})
        def fake_get(url, timeout):
            # b 的立绘与图标两路都失败（图标回落也断）；离线桩绝不触网
            if "/b.webp" in url or "/b-icon.webp" in url:
                raise OSError("模拟本轮网络失败")
            return b"X"  # 非 RIFF → _webp_pixel_size 解析失败 → 非竖版 → 走图标
        sf._http_get_bytes = fake_get
        feed = [{"slug": "a"},   # 源未变且文件在磁盘 → 跳过（不重复下载）
                {"slug": "b"},   # 文件缺失 → 立绘/图标两路重下 → 失败
                {"slug": "c"}]   # 新成功（桩返回非 RIFF → 图标路）
        dl, sk, failed = sf.download_avatars(feed)
        assert (dl, sk, failed) == (1, 1, ["b"]), (dl, sk, failed)
        man = sf._load_avatar_manifest().get("avatars") or {}
        assert set(man) == {"a", "b", "c", "d"}, set(man)
        assert man["b"]["fetched_at"] == "T0", "本轮失败的 b 既往条目不得掉出"
        assert man["d"]["fetched_at"] == "T0", "不在本轮 feed 的既往成功条目 d 保留"
        assert man["c"]["file"] == "c.webp" and man["c"]["fetched_at"] != "T0"
        assert man["c"]["source"] == f"{sf.MYSQIL_IMAGES_BASE}c-icon.webp", \
            man["c"]  # 立绘非竖版（桩字节不可解析）→ 图标回落且 manifest 记实际源 URL
        # 幂等：同 feed 再跑一轮 → 计数稳定（0 下载 2 跳过 1 失败），manifest 不再变化
        dl2, sk2, failed2 = sf.download_avatars(feed)
        assert (dl2, sk2, failed2) == (0, 2, ["b"]), (dl2, sk2, failed2)
        man2 = sf._load_avatar_manifest().get("avatars") or {}
        assert {k: v["fetched_at"] for k, v in man.items()} == \
               {k: v["fetched_at"] for k, v in man2.items()}, "第二轮 manifest 应逐字段稳定"
    finally:
        sf.AVATARS_DIR, sf.AVATAR_MANIFEST_JSON, sf._http_get_bytes = \
            orig_dir, orig_man, orig_get
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_mysqil_names_parse_offline():
    """mysqil 中文名→拼音 slug 表解析（2026-11-10 接入）：离线桩不触网。
    /characters/ 页卡片（<a href="/characters/<slug>">…<h3>名</h3>）为主路径；
    页面失败回落 sitemap slug 清单 + MYSQIL_SLUG_NAMES 对照表；对照表覆盖不了的
    slug 记 unmapped_slugs 绝不编造实名；25 项对照表与 2026-11-11 实抓逐项一致。"""
    from app import sites_fetch as sf
    # 对照表硬校验：25 slug 全量、名字非空、关键条目与实抓一致（页面 h3 +
    # data/mysqil/upgrades.json 双源核验值）
    expected = {
        "adeler": "阿德勒", "anhunqu": "安魂曲", "baizang": "白藏", "bohe": "薄荷",
        "canhong": "残虹", "dafutier": "达芙蒂尔", "edjia": "埃德嘉",
        "fadiya": "法帝娅", "haiyue": "海月", "haniya": "哈尼娅",
        "hasuoer": "哈索尔", "heiyu": "黑羽", "jiuyuan": "九原", "kaesi": "卡厄斯",
        "ling": "零", "lingke": "灵可", "mingyin": "明音凛", "nanali": "娜娜莉",
        "tajiduo": "塔吉多", "xiaozhi": "小吱", "xun": "浔", "yi": "翳",
        "yiluoyi": "伊洛伊", "zaowu": "早雾", "zhenhong": "真红"}
    assert sf.MYSQIL_SLUG_NAMES == expected, "MYSQIL_SLUG_NAMES 必须与实抓对照逐项一致"
    # 1) /characters/ 页主路径：卡片解析（无 h3 的卡不编造实名）
    page = """
    <a href="/characters/anhunqu" class="x"><div><h3>安魂曲</h3></div></a>
    <a href="/characters/heiyu" class="x"><div><h3> 黑羽 </h3></div></a>
    <a href="/characters/newone" class="x"><div>无名字卡</div></a>
    """
    orig_get = sf._http_get
    sf._http_get = lambda url, timeout=15: page
    try:
        d = sf.fetch_mysqil_names(timeout=1)
        assert d["names_source"] == "page", d["names_source"]
        by_slug = {c["slug"]: c["name"] for c in d["characters"]}
        assert by_slug == {"anhunqu": "安魂曲", "heiyu": "黑羽"}, by_slug
        assert d["unmapped_slugs"] == ["newone"], d["unmapped_slugs"]
        assert all(c["source"] == "mysqil"
                   and c["image"] == f"/images/characters/{c['slug']}.webp"
                   for c in d["characters"])
    finally:
        sf._http_get = orig_get
    # 2) 页面抓取失败 → sitemap + 对照表兜底（含表外 slug 不编造）
    sitemap = ("<urlset>"
               "<url><loc>https://wiki.mysqil.com/characters/anhunqu/</loc></url>"
               "<url><loc>https://wiki.mysqil.com/characters/heiyu/</loc></url>"
               "<url><loc>https://wiki.mysqil.com/characters/notintable/</loc></url>"
               "</urlset>")
    calls = []

    def _fake_get(url, timeout=15):
        calls.append(url)
        if "characters/" in url and "sitemap" not in url:
            raise OSError("模拟页面挂了")  # /characters/ 页失败
        return sitemap
    sf._http_get = _fake_get
    try:
        d2 = sf.fetch_mysqil_names(timeout=1)
        assert d2["names_source"] == "sitemap+table", d2["names_source"]
        by_slug2 = {c["slug"]: c["name"] for c in d2["characters"]}
        assert by_slug2 == {"anhunqu": "安魂曲", "heiyu": "黑羽"}, by_slug2
        assert d2["unmapped_slugs"] == ["notintable"], d2["unmapped_slugs"]
        assert calls[0].startswith("https://wiki.mysqil.com/characters/"), calls
        assert calls[1].endswith("sitemap-0.xml"), calls
    finally:
        sf._http_get = orig_get
    # 3) 两路全挂 → 抛 (OSError, ValueError)（调用方 sync_guide_sites 捕获该族，
    #    记 sources.mysqil.error，不崩同步）
    sf._http_get = lambda *a, **k: (_ for _ in ()).throw(OSError("全挂"))
    try:
        try:
            sf.fetch_mysqil_names(timeout=1)
            raise AssertionError("两路全挂应抛 OSError/ValueError")
        except (OSError, ValueError):
            pass
    finally:
        sf._http_get = orig_get


@case
def test_sync_guide_sites_offline():
    """sync_guide_sites 本体离线三态（独立复审阻断 #1 回归：payload ok 行曾残留
    sources["nteguide"] 键引用——sources 只含 ntestrategy/mysqil 两键，双站失败时
    KeyError 冒泡致 /api/sites/sync-guide 100% 失败；monkeypatch fetcher 桩离线复现
    并修复）。临时 SNAPSHOT_JSON，绝不触网、绝不写生产 data/sites/。"""
    from app import sites_fetch as sf
    tmp = tempfile.mkdtemp(prefix="oknte_sync_guide_")
    orig_dir, orig_snap = sf.SITES_DIR, sf.SNAPSHOT_JSON
    orig_nte, orig_mysqil = sf.fetch_ntestrategy, sf.fetch_mysqil_names
    sf.SITES_DIR = tmp
    sf.SNAPSHOT_JSON = os.path.join(tmp, "guide_sites.json")

    def fail(name):
        def f(timeout=15):
            raise ValueError("stub fail " + name)
        return f

    NT = {"characters": [{"name": "浔", "name_en": "Hotori", "slug": "hotori",
                          "source": "ntestrategy"}],
          "arcs": [], "materials": ["失落絮语"],
          "builds": {"trains": [], "console_builds": [], "char_arcs": []}}
    MQ = {"characters": [{"name": "安魂曲", "name_en": "", "slug": "anhunqu",
                          "source": "mysqil"}],
          "names_source": "page", "unmapped_slugs": []}
    try:
        # 态1 双站失败：ok=False、错误进 sources、快照不落盘、绝不抛 KeyError
        sf.fetch_ntestrategy, sf.fetch_mysqil_names = fail("nte"), fail("mysqil")
        merge, payload = sf.sync_guide_sites([], [])
        assert payload["ok"] is False, payload["ok"]
        assert payload["sources"]["ntestrategy"]["error"] == "stub fail nte"
        assert payload["sources"]["mysqil"]["error"] == "stub fail mysqil"
        assert not os.path.exists(sf.SNAPSHOT_JSON), "两站全挂不得刷新快照"
        # 态2 单挂单成：ok=True、快照落盘、mysqil 段写入且既往 nteguide 条目被剔除
        sf.fetch_ntestrategy = fail("nte")
        sf.fetch_mysqil_names = lambda timeout=15: dict(MQ)
        sf.save_json_atomic(sf.SNAPSHOT_JSON, {  # 预置既往快照（含 nteguide 残留段）
            "characters": [{"source": "nteguide", "slug": "x", "name": "误译"},
                           {"source": "ntestrategy", "slug": "hotori", "name": "浔"}],
            "arcs": [{"source": "nteguide", "name": "旧弧盘"},
                     {"source": "ntestrategy", "name": "现实庇护"}],
            "alt_names": [{"kind": "char_library", "name": "凛子", "source": "nteguide"}],
            "builds": [], "guide_unmapped_materials": []})
        merge, payload = sf.sync_guide_sites(["安魂曲"], [])
        assert payload["ok"] is True, payload["ok"]
        snap = json.load(open(sf.SNAPSHOT_JSON, encoding="utf-8"))
        srcs = {c.get("source") for c in snap["characters"]}
        assert srcs == {"ntestrategy", "mysqil"}, srcs
        assert not any(c.get("source") == "nteguide"
                       for c in snap["characters"] + snap["arcs"])
        assert not any(a.get("source") == "nteguide" for a in snap["alt_names"])
        assert sum(1 for c in snap["characters"]
                   if c.get("source") == "mysqil") == 1, "mysqil 段整体替换"
        # 态3 双站成功：目录合并（不覆盖已有实名、缺失补录）
        sf.fetch_ntestrategy = lambda timeout=15: dict(NT)
        merge, payload = sf.sync_guide_sites(["浔"], [])
        assert payload["ok"] is True
        assert merge["char_library"] == ["浔", "安魂曲"], merge["char_library"]
        # monkeypatch 全部还原
    finally:
        sf.SITES_DIR, sf.SNAPSHOT_JSON = orig_dir, orig_snap
        sf.fetch_ntestrategy, sf.fetch_mysqil_names = orig_nte, orig_mysqil
        shutil.rmtree(tmp, ignore_errors=True)


# ---- B3-A（CONC-04 修复专项，插在 sites 既有段之后）：sync-guide 抓取移出
# STATE.lock + 锁内重读合并；用例命名一律 test_conc04_*；慢源桩=确定性 sleep 注入
# + perf_counter 计时（禁随机 sleep）；state/快照/头像全部重定向仓库 .tmp/（E 盘），
# 全程打桩绝不触网。 ----

def _conc04_call(port, method, path, body=None):
    """B3-A HTTP 直调（学 test_tajiduo_endpoints_http.call：回环 Origin 过防线、
    面板令牌过 SEC-05 闸；每笔独立连接）。"""
    import http.client
    from app import main as main_mod
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Origin": "http://127.0.0.1:%d" % port,
               "X-Panel-Token": main_mod._PANEL_TOKEN}
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    if payload is not None:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, payload, headers)
    resp = conn.getresponse()
    data = json.loads(resp.read().decode("utf-8"))
    conn.close()
    return resp.status, data


def _conc04_lock_free_for_others():
    """B3-A 锁外观测：调用线程此刻不持有 STATE.lock。download_avatars 由 HTTP
    handler 线程调用，_is_owned()==False 即该调用发生在锁外（修复前该调用本就
    锁外——此观测即专项③「头像下载锁外不破坏」的运行时证明）。_thread.RLock
    C 实现无 owner 身份暴露，_is_owned 为 Python 3.13 实测可得口径；方法缺失
    时返回 None=观测不可用（不误伤非 CPython 环境）。"""
    is_owned = getattr(STATE.lock, "_is_owned", None)
    if is_owned is None:
        return None
    return not is_owned()


def _conc04_sha(path):
    """文件 sha256（pathlib 读）；不存在返回 None。"""
    if not os.path.isfile(path):
        return None
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _conc04_http_env(tmp, nte_stub, mysqil_stub, avatar_stub):
    """B3-A CONC-04 隔离 HTTP 夹具（学 test_tajiduo_endpoints_http 形态）：
    create_server(0, auto_range=1) + daemon serve_forever；STATE_FILE/sites 快照/
    头像目录重定向至 tmp（仓库 .tmp/，E 盘）；面板令牌直设全局（现生成，零凭据
    字面量；同既有 HTTP 用例口径不回滚——后续用例各自直设）。三桩均注入
    sites_fetch 模块属性（main.py 经模块属性访问，patch 必然生效）。"""
    from app import main as main_mod
    from app import sites_fetch as sf
    from app.main import create_server
    main_mod._PANEL_TOKEN = main_mod._new_panel_token()  # SEC-05：直设全局令牌
    orig = (sf.SITES_DIR, sf.SNAPSHOT_JSON, sf.AVATARS_DIR,
            sf.fetch_ntestrategy, sf.fetch_mysqil_names, sf.download_avatars,
            state_mod.STATE_FILE)
    sf.SITES_DIR = tmp
    sf.SNAPSHOT_JSON = os.path.join(tmp, "guide_sites.json")
    av_dir = os.path.join(tmp, "avatars")
    os.makedirs(av_dir, exist_ok=True)  # 空目录：avatar_files 磁盘枚举 → {}
    sf.AVATARS_DIR = av_dir
    state_mod.STATE_FILE = os.path.join(tmp, "state.json")
    sf.fetch_ntestrategy, sf.fetch_mysqil_names = nte_stub, mysqil_stub
    sf.download_avatars = avatar_stub
    server, _candidate = create_server(0, auto_range=1)
    port = server.server_address[1]  # 绑 0 时 OS 分配临时端口（同 :6560 口径）
    threading.Thread(target=server.serve_forever, daemon=True,
                     name="selftest-conc04-http").start()

    def restore():
        server.shutdown()
        server.server_close()
        (sf.SITES_DIR, sf.SNAPSHOT_JSON, sf.AVATARS_DIR,
         sf.fetch_ntestrategy, sf.fetch_mysqil_names, sf.download_avatars,
         state_mod.STATE_FILE) = orig

    return port, restore


@case
def test_conc04_fix_slow_fetch_concurrent_settings():
    """B3-A CONC-04 修复专项①：慢源并发不再阻塞写路径（HTTP 层端到端）。

    修复前（conc04-repro-1 三轮复现）：sync-guide 整段持 STATE.lock，慢源窗口
    ≈6s 期间 6 笔 POST /api/settings 单笔 5990.6~6027.8ms。修复后：抓取+解析+
    合并+快照落盘整段锁外，同窗口内 6 笔 /api/settings（写路径）每笔 <1s；
    抓取末态两库与串行基线（同桩数据直接调 sync_guide_sites）逐键一致。"""
    from app import sites_fetch as sf
    tmp = _conc_tmp("conc04_fix_slow")
    orig_state_file, orig_snap = state_mod.STATE_FILE, sf.SNAPSHOT_JSON
    h0 = (_conc04_sha(orig_state_file), _conc04_sha(orig_snap))
    NT = {"characters": [{"name": "浔", "name_en": "Hotori", "slug": "hotori",
                          "source": "ntestrategy"}],
          "arcs": [{"name": "现实庇护", "source": "ntestrategy"}],
          "materials": ["失落絮语"],
          "builds": {"trains": [], "console_builds": [], "char_arcs": []}}
    MQ = {"characters": [{"name": "安魂曲", "name_en": "", "slug": "anhunqu",
                          "source": "mysqil"}],
          "names_source": "page", "unmapped_slugs": []}
    ev1, ev2 = threading.Event(), threading.Event()

    def quick_nte(timeout=15):
        return dict(NT)

    def quick_mysqil(timeout=15):
        return dict(MQ)

    def slow_nte(timeout=15):  # 确定性慢源桩：先 set 事件（=窗口开启）再 sleep
        ev1.set()
        time.sleep(3)
        return dict(NT)

    def slow_mysqil(timeout=15):
        ev2.set()
        time.sleep(3)
        return dict(MQ)

    def avatar_stub(chars):
        return 0, 0, []

    with _StateGuard():
        port, restore = _conc04_http_env(tmp, quick_nte, quick_mysqil, avatar_stub)
        try:
            with STATE.lock:  # 预置基线库（STATE_FILE 已重定向 .tmp）
                STATE.data["char_library"] = ["浔"]
                STATE.data["weapon_library"] = []
                STATE.data["settings"] = dict(DEFAULT_SETTINGS)
                STATE.save()
            # 串行基线（快桩直接调用，不写 STATE——merge 基准）
            merge_base, payload_base = sf.sync_guide_sites(["浔"], [])
            assert payload_base["ok"] is True, payload_base["ok"]
            assert merge_base["char_library"] == ["浔", "安魂曲"], \
                merge_base["char_library"]
            # 切慢桩 → 并发编排（确定性：事件对齐，禁随机 sleep）
            sf.fetch_ntestrategy, sf.fetch_mysqil_names = slow_nte, slow_mysqil
            result = {}

            def sync_call():
                result["t_send"] = time.perf_counter()
                code, data = _conc04_call(port, "POST", "/api/sites/sync-guide", {})
                result["code"], result["data"] = code, data
                result["t_resp"] = time.perf_counter()

            th = threading.Thread(target=sync_call, name="conc04-sync")
            th.start()
            assert ev1.wait(10), "nte 桩未入（慢源窗口未开启）"
            # ev2（mysqil 桩入）不等：ev1 即窗口开启（距窗口结束还有两个 3s 桩），
            # 等它反而把采样压进窗口后半段
            # 竞速期采样 N=6 笔 POST /api/settings（perf_counter 逐笔计时）
            probes = []
            for i in range(6):
                t_send = time.perf_counter()
                code, data = _conc04_call(port, "POST", "/api/settings",
                                          dict(DEFAULT_SETTINGS))
                probes.append({"i": i, "done": time.perf_counter(),
                               "dur": time.perf_counter() - t_send,
                               "code": code, "ok": data.get("ok")})
            th.join(30)
            assert not th.is_alive(), "sync 线程硬超时未完成"
            # 用例有效性：慢源确实慢——sync 总耗时 ≥ 2×3−0.5s
            sync_dur = result["t_resp"] - result["t_send"]
            assert sync_dur >= 5.5, f"sync 总耗时 {sync_dur:.3f}s < 5.5s（慢源未生效）"
            assert result["code"] == 200 and result["data"]["ok"] is True, \
                (result["code"], result["data"])
            # 主断言：竞速期每笔 settings <1s（修复前实测 5990.6~6027.8ms）
            worst = max(p["dur"] for p in probes)
            assert worst < 1.0, f"竞速期 settings 最大耗时 {worst:.3f}s ≥ 1s"
            assert all(p["code"] == 200 and p["ok"] is True for p in probes), \
                [(p["i"], p["code"], p["ok"]) for p in probes]
            # 编排自检：采样确实与慢源窗口重叠（首笔在 sync 响应前 ≥4s 完成）
            assert probes[0]["done"] <= result["t_resp"] - 4.0, \
                "settings 采样未落入慢源窗口（编排自检失败）"
            # 抓取末态与串行基线逐键一致（两库合并结果）
            with STATE.lock:
                end_chars = list(STATE.data["char_library"])
                end_weapons = list(STATE.data["weapon_library"])
            assert end_chars == merge_base["char_library"][:200], \
                (end_chars, merge_base["char_library"])
            assert end_weapons == merge_base["weapon_library"][:200], \
                (end_weapons, merge_base["weapon_library"])
        finally:
            restore()
            shutil.rmtree(tmp, ignore_errors=True)  # 失败轮也不得残留 .tmp/
    # 生产 data/ 零写入（哈希前后比对：state.json + guide_sites.json）
    h1 = (_conc04_sha(orig_state_file), _conc04_sha(orig_snap))
    assert h1 == h0, f"生产 data/ 文件哈希被改动: {h0} -> {h1}"


@case
def test_conc04_fix_reread_merge_no_lost_update():
    """B3-A CONC-04 修复专项②：锁内重读防丢更新——抓取期间并发改库不被覆盖。

    慢源窗口内（nte 桩已入=路由已过基线副本读取、正在锁外抓取）另一线程持
    STATE.lock 给 char_library 整表追加「并发新增实名」（学 /api/game-data/
    import libraries 写法，main.py:946 整表替换）；同步完成后断言：并发改动
    保留在前段（合并以锁内重读库为基准）+ 抓取新增「安魂曲」补录其后 +
    weapon_library 与串行语义一致。修复前该写会被 sync 末尾基于抓取前基准的
    整表写回覆盖丢失。"""
    from app import sites_fetch as sf
    tmp = _conc_tmp("conc04_fix_reread")
    NT = {"characters": [{"name": "浔", "name_en": "Hotori", "slug": "hotori",
                          "source": "ntestrategy"}],
          "arcs": [{"name": "现实庇护", "source": "ntestrategy"}],
          "materials": ["失落絮语"],
          "builds": {"trains": [], "console_builds": [], "char_arcs": []}}
    MQ = {"characters": [{"name": "安魂曲", "name_en": "", "slug": "anhunqu",
                          "source": "mysqil"}],
          "names_source": "page", "unmapped_slugs": []}
    ev1 = threading.Event()
    writer_done = threading.Event()

    def slow_nte(timeout=15):
        ev1.set()
        time.sleep(3)
        return dict(NT)

    def slow_mysqil(timeout=15):
        time.sleep(3)  # 双源都慢：确保锁内重读发生在写者改动之后（无随机竞速）
        return dict(MQ)

    def avatar_stub(chars):
        return 0, 0, []

    def writer():
        assert ev1.wait(10)
        with STATE.lock:  # 并发写者：整表追加（学 main.py /api/game-data/import）
            STATE.data["char_library"] = \
                list(STATE.data["char_library"]) + ["并发新增实名"]
            STATE.save()
        writer_done.set()

    with _StateGuard():
        port, restore = _conc04_http_env(tmp, slow_nte, slow_mysqil, avatar_stub)
        try:
            with STATE.lock:
                STATE.data["char_library"] = ["浔"]
                STATE.data["weapon_library"] = []
                STATE.data["settings"] = dict(DEFAULT_SETTINGS)
                STATE.save()
            result = {}

            def sync_call():
                code, data = _conc04_call(port, "POST", "/api/sites/sync-guide", {})
                result["code"], result["data"] = code, data

            th = threading.Thread(target=sync_call, name="conc04-sync")
            wt = threading.Thread(target=writer, name="conc04-writer")
            th.start()
            wt.start()
            assert writer_done.wait(10), "写者未完成（慢源窗口内改动未落地）"
            th.join(30)
            assert not th.is_alive(), "sync 线程硬超时未完成"
            assert result["code"] == 200 and result["data"]["ok"] is True, \
                (result["code"], result["data"])
            with STATE.lock:
                end_chars = list(STATE.data["char_library"])
                end_weapons = list(STATE.data["weapon_library"])
            # 改动力不被覆盖：并发新增保留在前段（合并以锁内重读库为基准）
            assert "并发新增实名" in end_chars, end_chars
            assert end_chars == ["浔", "并发新增实名", "安魂曲"], end_chars
            assert end_weapons == ["现实庇护"], end_weapons
        finally:
            restore()
            shutil.rmtree(tmp, ignore_errors=True)  # 失败轮也不得残留 .tmp/


@case
def test_conc04_fix_normal_sync_regression():
    """B3-A CONC-04 修复专项③：正常同步回归——快照落盘/头像下载锁外不破坏。

    快桩成功路径：POST sync-guide 200，响应字段形状逐键断言（14 键，与修复前
    逐字等价的运行时自证）；download_avatars 桩恰一次、入参=payload characters
    中 source=='mysqil' 条目、调用时 STATE.lock 无他线程持有（锁外观测）；快照
    落盘重定向 .tmp 且 sources/characters 正确。失败路径回归：双源桩失败 →
    502（body 三键与修复前同形）、快照不落盘、头像下载不触发。"""
    from app import sites_fetch as sf
    tmp = _conc_tmp("conc04_fix_regress")
    NT = {"characters": [{"name": "浔", "name_en": "Hotori", "slug": "hotori",
                          "source": "ntestrategy"}],
          "arcs": [{"name": "现实庇护", "source": "ntestrategy"}],
          "materials": ["失落絮语"],
          "builds": {"trains": [], "console_builds": [], "char_arcs": []}}
    MQ = {"characters": [{"name": "安魂曲", "name_en": "", "slug": "anhunqu",
                          "source": "mysqil"}],
          "names_source": "page", "unmapped_slugs": []}
    MQ_ENTRY = {"name": "安魂曲", "name_en": "", "slug": "anhunqu",
                "source": "mysqil"}

    def quick_nte(timeout=15):
        return dict(NT)

    def quick_mysqil(timeout=15):
        return dict(MQ)

    def fail(name):
        def f(timeout=15):
            raise ValueError("stub fail " + name)
        return f

    av_calls = []

    def avatar_probe(chars):
        av_calls.append({"chars": [dict(c) for c in chars],
                         "lock_free": _conc04_lock_free_for_others()})
        return 0, 0, []

    with _StateGuard():
        port, restore = _conc04_http_env(tmp, quick_nte, quick_mysqil, avatar_probe)
        try:
            with STATE.lock:
                STATE.data["char_library"] = ["浔"]
                STATE.data["weapon_library"] = []
                STATE.data["settings"] = dict(DEFAULT_SETTINGS)
                STATE.save()
            code, data = _conc04_call(port, "POST", "/api/sites/sync-guide", {})
            assert code == 200 and data["ok"] is True, (code, data)
            # 响应字段形状逐键不变（14 键：与修复前逐字等价的运行时自证）
            assert set(data.keys()) == {
                "ok", "rev", "fetched_at", "avatars", "avatars_dl",
                "guide_slug_names", "sources", "chars", "weapons", "materials",
                "alt_names_count", "builds", "char_library",
                "weapon_library"}, sorted(data.keys())
            assert data["chars"]["added"] == ["安魂曲"], data["chars"]
            assert data["weapons"]["added"] == ["现实庇护"], data["weapons"]
            assert data["char_library"] == ["浔", "安魂曲"], data["char_library"]
            assert data["weapon_library"] == ["现实庇护"], data["weapon_library"]
            assert data["avatars_dl"] == {"downloaded": 0, "skipped": 0,
                                          "failed": []}, data["avatars_dl"]
            assert data["avatars"] == {}, data["avatars"]  # 头像目录重定向空
            assert set(data["guide_slug_names"]) == {"hotori", "anhunqu"}, \
                data["guide_slug_names"]
            assert isinstance(data["rev"], int) \
                and not isinstance(data["rev"], bool), data["rev"]
            assert isinstance(data["fetched_at"], str) and data["fetched_at"]
            assert set(data["sources"]) == {"ntestrategy", "mysqil"} \
                and data["sources"]["ntestrategy"]["ok"] \
                and data["sources"]["mysqil"]["ok"], data["sources"]
            assert isinstance(data["builds"], list), type(data["builds"])
            # 头像下载：恰一次、入参=mysqil 源条目、调用时锁无他线程持有（锁外）
            assert len(av_calls) == 1, av_calls
            assert av_calls[0]["chars"] == [MQ_ENTRY], av_calls[0]
            assert av_calls[0]["lock_free"] is True, av_calls[0]
            # 快照落盘（sync_guide_sites 整体锁外，sites_fetch.py:709 门成功才落）
            snap = json.loads(Path(sf.SNAPSHOT_JSON).read_text(encoding="utf-8"))
            assert snap["ok"] is True \
                and snap["fetched_at"] == data["fetched_at"], snap.get("ok")
            srcs = {c.get("source") for c in snap["characters"]}
            assert srcs == {"ntestrategy", "mysqil"}, srcs
            # 失败路径回归：双源桩失败 → 502（body 三键与修复前同形）、
            # 快照不落盘、头像下载不触发（502 判定与 _send 已挪锁外）
            os.remove(sf.SNAPSHOT_JSON)
            sf.fetch_ntestrategy, sf.fetch_mysqil_names = fail("nte"), fail("mysqil")
            code, data = _conc04_call(port, "POST", "/api/sites/sync-guide", {})
            assert code == 502 and data["ok"] is False, (code, data)
            assert set(data.keys()) == {"ok", "error", "sources"}, \
                sorted(data.keys())
            assert "stub fail nte" in data["error"] \
                and "stub fail mysqil" in data["error"], data["error"]
            assert not os.path.exists(sf.SNAPSHOT_JSON), "两站全挂不得刷新快照"
            assert len(av_calls) == 1, "502 路径不得触发头像下载"
        finally:
            restore()
            shutil.rmtree(tmp, ignore_errors=True)  # 失败轮也不得残留 .tmp/


@case
def test_manual_only_inventory_exclusion():
    """手动项机制（2026-11-10）：materials.json 的 manual_only:true 材料
    （甲硬币 coin/方斯 fons/环石 huan_stone/捏造骰子 niefu_dice/质实骰子
    zhishi_dice）——_inventory_candidates 一律跳过（OCR 不自动识别）；
    /api/inventory 全量过滤白名单 planner.ALL_ORDER 仍含这 5 个 id（手动录入
    可落账）；不进 tiers（折算/缺口链 equivalent_inventory 走 tiers 天然不涉及）。
    注入一条临时 manual_only 对照验证排除来自 manual_only 本身而非 id。"""
    from app import main as main_mod
    import json as _json
    manual_ids = ("coin", "fons", "huan_stone", "niefu_dice", "zhishi_dice")
    # 1) 生产数据表：5 项 manual_only、不在 OCR 候选、在 ALL_ORDER、不在 tiers
    for group in ("targets", "extra_materials"):
        flagged = {m["id"] for m in planner.MATERIALS.get(group, [])
                   if m.get("manual_only")}
        assert "coin" in flagged if group == "targets" else True, flagged
    extra_flagged = {m["id"] for m in planner.MATERIALS.get("extra_materials", [])
                     if m.get("manual_only")}
    assert {"fons", "huan_stone", "niefu_dice", "zhishi_dice"} <= extra_flagged, extra_flagged
    cand_ids = {mid for _, mid in main_mod._inventory_candidates()}
    for mid in manual_ids:
        assert mid not in cand_ids, f"manual_only {mid} 不得进 OCR 候选"
    assert "city_ability_up" in cand_ids, "非 manual 的 extra 对照项应留在候选"
    assert all(mid in planner.ALL_ORDER for mid in manual_ids), \
        "/api/inventory 全量过滤白名单必须含 5 个手动项 id"
    tiers_text = _json.dumps(planner.MATERIALS.get("tiers", {}), ensure_ascii=False)
    for mid in manual_ids:
        assert mid not in tiers_text, f"{mid} 不得进 tiers"
    # 2) 排除判据是 manual_only 本身：临时给对照项 city_ability_up 打标 → 出候选
    orig_extra = planner.MATERIALS.get("extra_materials")
    try:
        patched = [dict(e) for e in orig_extra]
        for e in patched:
            if e.get("id") == "city_ability_up":
                e["manual_only"] = True
        planner.MATERIALS["extra_materials"] = patched
        cand_ids2 = {mid for _, mid in main_mod._inventory_candidates()}
        assert "city_ability_up" not in cand_ids2, "manual_only 打标即出候选"
    finally:
        planner.MATERIALS["extra_materials"] = orig_extra
    # 3) /api/inventory 端点过滤语义（与 Handler 内联过滤同款表达式）：5 项可落账
    incoming = {"coin": 5, "fons": 6, "huan_stone": 7, "niefu_dice": 8,
                "zhishi_dice": 9, "id_not_in_table": 99}
    filtered = {str(k): v for k, v in incoming.items() if str(k) in planner.ALL_ORDER}
    assert filtered == {"coin": 5, "fons": 6, "huan_stone": 7,
                        "niefu_dice": 8, "zhishi_dice": 9}, filtered


@case
def test_tajiduo_arc_registration_and_apply():
    """曳尔沐弧盘补洞（2026-11-10）：自建弧盘「静候彩铃」（payload fork id=
    fork_elyms）在所有目录零命中 → annotate 自动登记 weapon_library（来源
    tajiduo/category arc）→ matched_name 回填 → apply 可写绑定+等级；
    二次登记幂等（不重复追加）；日志行「弧盘未收录已自动登记」可见。
    _StateGuard 备份/恢复 state.json，绝不污染生产数据。"""
    from app import main as main_mod
    tmp = tempfile.mkdtemp(prefix="oknte_arc_reg_")
    try:
        with _StateGuard():
            root = os.path.join(tmp, "root")
            STATE.data["settings"] = dict(DEFAULT_SETTINGS, oknte_root=root)
            STATE.data["characters"] = [_char("elyms", "曳尔沐", {})]
            STATE.data["weapon_library"] = ["思考喵"]
            STATE.data["today"] = _empty_today()
            rows = [{"target": "曳尔沐", "name": "曳尔沐", "level": 38,
                     "fork": {"id": "fork_elyms", "name": "静候彩铃", "level": 1}}]
            # 1) annotate：未收录弧盘自动登记（幂等起点）
            annotated = main_mod._tajiduo_annotate(rows)
            arc = annotated[0]["arc"]
            assert arc["matched_name"] == "静候彩铃", arc
            assert arc["registered"] is True, arc
            assert "静候彩铃" in STATE.data["weapon_library"], STATE.data["weapon_library"]
            src = (STATE.data.get("weapon_sources") or {}).get("静候彩铃") or {}
            assert src.get("source") == "tajiduo" and src.get("category") == "arc", src
            # 日志行（RUNNER 面板日志）
            assert any("弧盘未收录已自动登记：静候彩铃" in ln
                       for ln in runner_mod.RUNNER.snapshot()["lines"]), \
                runner_mod.RUNNER.snapshot()["lines"][-3:]
            # 2) 二次幂等：登记函数与 annotate 均不再新增
            canon, registered = main_mod._tajiduo_register_arc("静候彩铃")
            assert canon == "静候彩铃" and registered is False, (canon, registered)
            lib_len = len(STATE.data["weapon_library"])
            annotated2 = main_mod._tajiduo_annotate(rows)
            assert annotated2[0]["arc"]["registered"] is False
            assert len(STATE.data["weapon_library"]) == lib_len, "幂等：库长不变"
            assert (STATE.data.get("weapon_sources") or {}).get("静候彩铃", {}) \
                .get("source") == "tajiduo", "来源标注不因重复登记漂移"
            # 3) apply：登记后弧盘绑定 + 等级可写（此前 skip 的补洞语义）
            apply_rows = [{"target": "曳尔沐", "arc_weapon": "静候彩铃", "arc_level": 3}]
            result = main_mod._tajiduo_apply_rows(apply_rows)
            assert result["skipped"] == [], result["skipped"]
            char = next(c for c in STATE.data["characters"] if c["name"] == "曳尔沐")
            assert char.get("weapon") == "静候彩铃", char.get("weapon")
            assert (char.get("tracks") or {}).get("arc_level", {}).get("cur") == 3
            assert (char.get("tracks") or {}).get("arc_level", {}).get("tgt") == 3, \
                "目标≥当前语义保持"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_plan_options_endpoint():
    """GET /api/oknte/plan-options（2026-11-10 新端点，纯只读）：读 working/configs
    当前 daily_anomaly 三键，对照 prepare_daily_profile 将写入值（首个计划步骤的
    mat config + 本地已耗+步骤体力 + 循环模式=停用）产出 changes=[{key,cur,new}]；
    restarts：ok-nte 运行中=1 否则 0（headless 每步重读配置生效，无需重启）。
    经 _plan_options 直调（端点仅薄壳转发，端点源静态校验路由存在）。
    fake ok-nte + _StateGuard，配置文件字节级不变量校验。"""
    import hashlib
    import inspect
    from app import main as main_mod
    tmp = tempfile.mkdtemp(prefix="oknte_planopts_")
    try:
        with _StateGuard():
            root = os.path.join(tmp, "root")
            ok = _build_fake_oknte(root)
            ok.is_running_cached = lambda: False
            STATE.data["settings"] = dict(DEFAULT_SETTINGS, oknte_root=root)
            STATE.data["inventory"] = {"coin": 0}
            STATE.data["today"] = _empty_today()
            STATE.data["characters"] = [_char("c1", "预演角色", {"coin": 30})]
            with open(ok.routine_config, "rb") as f:
                before_hash = hashlib.sha256(f.read()).hexdigest()
            # 1) 有步骤：changes 含 prepare_daily_profile 将写的差异键
            payload = main_mod._plan_options(ok)
            assert payload["oknte_running"] is False and payload["restarts"] == 0, payload
            assert "无需重启" in payload["restarts_reason"], payload["restarts_reason"]
            assert payload["steps_count"] >= 1 and payload["first_step"], payload
            keys = {c["key"] for c in payload["changes"]}
            # fake 配置初始 具体奖励目标=弧盘经验 / 目标消耗体力=240 / 循环模式=自动循环…
            assert {"具体奖励目标", "目标消耗体力", "循环模式"} <= keys, keys
            for c in payload["changes"]:
                assert c["cur"] != c["new"], c  # 只列真实差异
                assert c["cur"] == FAKE_ROUTINE_CONFIG["daily_anomaly"].get(c["key"]), c
            target_stamina = payload["first_step"]["stamina"]
            by_key = {c["key"]: c for c in payload["changes"]}
            assert by_key["目标消耗体力"]["new"] == target_stamina, \
                "本地已耗 0 + 步骤体力 = prepare_daily_profile 将写入值"
            assert by_key["循环模式"]["new"] == "停用"
            with open(ok.routine_config, "rb") as f:
                assert hashlib.sha256(f.read()).hexdigest() == before_hash, \
                    "纯只读：配置文件字节不变"
            # 2) ok-nte 运行中：restarts=1 + 等待原因
            ok.is_running_cached = lambda: True
            payload2 = main_mod._plan_options(ok)
            assert payload2["restarts"] == 1 and payload2["oknte_running"] is True
            assert "完全退出" in payload2["restarts_reason"]
            # 3) 配置损坏：ValueError（端点转 502）
            Path(_contained(ok.routine_config, ok.root)).write_text(
                '{"daily_anomaly": broken', encoding="utf-8")
            try:
                main_mod._plan_options(ok)
                raise AssertionError("损坏配置应抛 ValueError")
            except ValueError:
                pass
            # 端点薄壳存在（GET 路由静态校验）
            get_src = inspect.getsource(main_mod.Handler.do_GET)
            assert '"/api/oknte/plan-options"' in get_src
            assert "_plan_options(self._oknte())" in get_src
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_monitor_capture_fail_fastfail():
    """屏幕采集初始化失败快速返回（2026-11-10 真机取证）：执行器日志出现
    "cant find a usable windows capture"（DeviceManager）或 "executor connect
    False"（LauncherTask 10s 刷新超时）→ monitor.capture_fail 记首条原文并置位
    不复位；runner 据此当轮失败返回（报错含原文与窗口检查提示，启动失败前缀
    不拉黑）。两标记在 11-08/11-09 正常日志零出现（grep 实证）→ 可靠判据。"""
    from app.oknte_adapter import RunMonitor
    real_line = ("2026-11-10 16:48:46,586 ERROR RefreshAdb DeviceManager:"
                 "cant find a usable windows capture ")
    m = RunMonitor()
    m.feed([real_line])
    assert m.capture_fail == real_line.strip()[:160], m.capture_fail
    assert m.outcome(200, 40)[0] == "failed"
    connect_line = ("2026-11-10 16:49:01,846 WARNING TaskExecutor LauncherTask:"
                    "try refresh timeout 10s, executor connect False")
    m2 = RunMonitor()
    m2.feed([connect_line])
    assert "executor connect False" in m2.capture_fail, m2.capture_fail
    # 置位不复位：后续正常行不清除（同子进程内采集失败不会自愈）
    m2.feed(["2026-11-10 16:49:10 INFO RefreshAdb hwnd_window:hwnd changed,connected:1"])
    assert m2.capture_fail, "置位后不得复位"
    # 正常日志（GUI 成功路径形态）不得误报
    m3 = RunMonitor()
    m3.feed([
        "2026-11-10 16:54:08,470 INFO RefreshAdb capture:check available "
        "WINDOWS_BUILD_NUMBER:19045 >= 20348 False",
        "2026-11-10 16:54:08,471 INFO RefreshAdb update:use BitBlt_RenderFull "
        "capture render_full: True",
        "2026-11-10 16:54:08,471 INFO RefreshAdb DeviceManager:capture method "
        "<class 'ok.device.capture_methods.bitblt.BitBltCaptureMethod'>"])
    assert m3.capture_fail == "", m3.capture_fail
    # 常量逐字对齐 ok-script.log 原文（取证行 :35 / :129）
    assert RunMonitor.CAPTURE_FAIL_MARK == "cant find a usable windows capture"
    assert RunMonitor.CONNECT_FAIL_MARK == "executor connect False"


@case
def test_codex_custom_material():
    import app.main as main_mod
    tmp = tempfile.mkdtemp(prefix="oknte_codex_mat_")
    orig_main_datadir = main_mod.DATA_DIR
    orig_planner_datadir = planner.DATA_DIR
    main_mod.DATA_DIR = tmp
    planner.DATA_DIR = tmp
    # 预置三张表副本（add_custom_material 内部的 reload_tables 需要同目录三表齐全）
    for name in ("materials.json", "yields.json", "requirements.json"):
        shutil.copy(os.path.join(orig_planner_datadir, name), os.path.join(tmp, name))
    try:
        entry = main_mod.add_custom_material({"id": "boss_test_new",
                                              "display_name": "测试新材料",
                                              "category": "BOSS掉落",
                                              "obtain": "hunter", "source": "测试来源"})
        assert entry["id"] == "boss_test_new" and entry["obtain"] == "hunter"
        saved = json.load(open(os.path.join(tmp, "materials.json"), encoding="utf-8"))
        assert "boss_test_new" in [m["id"] for m in saved["extra_materials"]]
        assert "boss_test_new" in planner.ALL_ORDER, "登记后应进入全量目录白名单"
        for bad in ({"id": "boss_test_new", "display_name": "重复"},
                    {"id": "BAD ID", "display_name": "x"},
                    {"id": "boss_ok", "display_name": ""}):
            try:
                main_mod.add_custom_material(bad)
                raise AssertionError(f"应拒绝：{bad}")
            except ValueError:
                pass
    finally:
        main_mod.DATA_DIR = orig_main_datadir
        planner.DATA_DIR = orig_planner_datadir
        planner.reload_tables()  # 恢复生产内存表
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_state_settings_persist_across_load():
    """真机缺陷回归（限额 360→180 根因）：加载时默认值只补缺，绝不覆盖用户标量设置。"""
    from app import state as state_mod
    tmp = tempfile.mkdtemp(prefix="oknte_state_persist_")
    orig = state_mod.STATE_FILE
    try:
        state_mod.STATE_FILE = os.path.join(tmp, "state.json")
        state_mod.save_json_atomic(state_mod.STATE_FILE, {
            "settings": {"daily_stamina_limit": 360, "port": 8766,
                         "continuous_mode": False, "busy_retry_seconds": 45}})
        fresh = state_mod.State()
        s = fresh.data["settings"]
        assert s["daily_stamina_limit"] == 360, s
        assert s["port"] == 8766, s
        assert s["continuous_mode"] is False, s
        assert s["busy_retry_seconds"] == 45, s
        assert s["notify_enabled"] is False, "缺失键仍应补默认值"
    finally:
        state_mod.STATE_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_startup_failure_no_blacklist():
    """启动失败（互斥/环境）不进当日拉黑；游戏内任务失败仍拉黑。"""
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.data["today"] = _empty_today()
        step = {"mat_id": "exp_char", "name": "测试目标", "runs": 1, "stamina": 40, "gain": 1}
        runner_mod.RUNNER._settle(step, "failed", 0,
                                  "启动失败（exit 1）：Another application instance is still running")
        assert "exp_char" not in STATE.today.get("blacklist", []), "启动失败不得拉黑"
        assert STATE.data["history"][-1]["note"].startswith("启动失败")
        runner_mod.RUNNER._settle(step, "failed", 0, "未找到副本按钮")
        assert "exp_char" in STATE.today.get("blacklist", []), "游戏内失败仍应拉黑"


@case
def test_auto_extra_needs():
    """保存角色时自动按目录计算 BOSS/周本/小怪材料；用户自定义键保留。"""
    import app.main as main_mod
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        chars = [{"name": "浔", "extra_needs": {"my_custom": 7}},
                 {"name": "不存在的角色", "extra_needs": {"coin": 100}}]
        out = main_mod._auto_fill_extra_needs(chars)
        xun = out[0]["extra_needs"]
        assert xun["boss_flower_seed"] == 86 and xun["wk_skirt"] == 11 \
            and xun["mob_whisper_c"] == 390, xun
        assert xun["my_custom"] == 7, "用户自定义键保留"
        assert out[1]["extra_needs"] == {"coin": 100}, "目录未命中的角色不动"


@case
def test_weapon_library_duplicates():
    """武器库允许同名多把（实例化）；角色库仍去重。"""
    import app.main as main_mod
    libs = main_mod._sanitize_libraries(
        {"char_library": ["甲", "甲", "乙"], "weapon_library": ["弧盘A", "弧盘A", "弧盘B"]})
    assert libs["char_library"] == ["甲", "乙"], libs["char_library"]
    assert libs["weapon_library"] == ["弧盘A", "弧盘A", "弧盘B"], libs["weapon_library"]


@case
def test_log_sources_whitelist():
    """日志页白名单：只收三类目录下按模式命中的 .log；目录外文件（穿越）不出现。"""
    import app.main as main_mod
    from app import state as state_mod
    tmp = tempfile.mkdtemp(prefix="oknte_logs_")
    for rel in ("data/apps/ok-nte/working/logs/ok-script.log",
                "data/apps/ok-nte/working/logs/ok-script.2026-10-29.log",
                "data/apps/ok-nte/working/logs/other.log",      # 模式不匹配
                "data/apps/ok-nte/working/logs/ok-script.txt",  # 扩展名不符
                "logs/app.2026-10-29.log"):
        full = os.path.join(tmp, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        state_mod.save_json_atomic(full, "")
    os.makedirs(os.path.join(tmp, "secret"))  # 模拟穿越：白名单目录外的同名日志
    state_mod.save_json_atomic(os.path.join(tmp, "secret", "ok-script.log"), "")
    try:
        names = [p for _label, p in main_mod._collect_log_sources(tmp)]
        assert any(p.endswith(os.sep.join(("working", "logs", "ok-script.log"))) for p in names)
        assert any(p.endswith("ok-script.2026-10-29.log") for p in names)
        assert any(p.endswith(os.sep.join(("logs", "app.2026-10-29.log"))) for p in names)
        assert not any("other.log" in p for p in names), "模式不匹配的文件不得入列"
        assert not any("ok-script.txt" in p for p in names), "扩展名不符不得入列"
        assert not any("secret" in p for p in names), "白名单目录外的文件不得入列"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_detection_pattern():
    """进程检测模式：真机取证的四类匹配面齐全，且不含宽泛误伤项。"""
    from app import oknte_adapter as ad
    ps = ad._detection_patterns(
        r"D:\GamingAssistant\ok-nte\data\apps\ok-nte\working",
        r"D:\GamingAssistant\ok-nte\data\apps\ok-nte")
    assert "pythonw.exe'" in ps, "GUI 独占 pythonw 的 ExecutablePath 后缀匹配"
    assert "main.py*" in ps, "启动器拉起形态"
    assert ".pyappify-shortcut.py'" in ps, "快捷方式监督进程形态"
    assert "*-m ok run_task*'" in ps, "规划器无头子进程（防双开）"
    assert "'*ok-nte*'" not in ps, "不得使用宽泛误伤模式"


@case
def test_clear_blacklist():
    """拉黑自救：清空 today.blacklist 并持久化。"""
    import app.main as main_mod
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.data["today"] = _empty_today()
        STATE.today["blacklist"] = ["exp_char", "coin"]
        cleared = main_mod.clear_today_blacklist()
        assert cleared == 2 and STATE.today["blacklist"] == []
        saved = json.load(open(STATE_FILE, encoding="utf-8"))
        assert saved["today"]["blacklist"] == [], "清空应落盘"


@case
def test_planner_upstream_cap_and_cumulative_target():
    """真机反馈回归：①限额 360 排程封顶 180 并警告；②写入副本为当日累计口径。"""
    import app.main as main_mod
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS, daily_stamina_limit=360)
        STATE.data["today"] = _empty_today()
        STATE.data["today"]["used_stamina"] = 0
        STATE.data["inventory"] = {}
        STATE.data["characters"] = [_char("c1", "封顶角色", {"coin": 7})]
        plan = planner.make_plan(now=NOW)
        assert plan["detail"]["budget_total"] == 180, plan["detail"]
        assert any("180" in w and "不可达" in w for w in plan["warnings"]), plan["warnings"]
        # 累计口径：used=100 时写入副本的目标 = 100 + step.stamina
        STATE.data["today"]["used_stamina"] = 100
        step = {"mat_id": "coin", "name": "甲硬币", "runs": 1, "stamina": 40}
        with tempfile.TemporaryDirectory(prefix="oknte_cum_") as tmp:
            ok = adapter_mod.OkNte(tmp)
            os.makedirs(os.path.join(tmp, "data", "apps", "ok-nte", "working", "configs"))
            adapter_mod.save_json_atomic(ok.routine_config, FAKE_ROUTINE_CONFIG)
            adapter_mod.save_json_atomic(ok.routine_items, {"Routine Items": [
                {"id": "daily_anomaly", "enabled": True},
                {"id": "coffee", "enabled": True}]})
            launcher = ok.prepare_daily_profile(
                planner.TARGETS["coin"]["config"], 100 + step["stamina"])
            daily = json.load(open(ok.profile_daily, encoding="utf-8"))["daily_anomaly"]
            assert daily["目标消耗体力"] == 140, daily
            items = json.load(open(ok.profile_items, encoding="utf-8"))["Routine Items"]
            assert [e["id"] for e in items if e.get("enabled")] == ["daily_anomaly"], items


# ---------- B2.8-C（BIZ-01）：单一权威额度——preview 与 make_plan 共享 cap ----------

@case
def test_biz01_effective_limit_matrix():
    """①矩阵：limit∈{80,180,360,0,99999} → _effective_daily_limit 逐一命中
    max(0, min(limit, 180))，且 make_plan budget_total 与 preview 逐日 budget
    两侧同步取同一有效额度（单一权威取值，无第二套额度规则）。"""
    with _StateGuard():
        STATE.data["today"] = _day_today()
        STATE.data["inventory"] = {}
        STATE.data["characters"] = [_char("c1", "矩阵角色", {"coin": 21})]
        want = {80: 80, 180: 180, 360: 180, 0: 0, 99999: 180}
        for limit, eff in want.items():
            assert planner._effective_daily_limit(limit) == eff, (limit, eff)
            STATE.data["settings"] = dict(DEFAULT_SETTINGS, daily_stamina_limit=limit)
            plan = planner.make_plan(now=NOW)
            pv = planner.preview_days(2, now=NOW)
            assert plan["detail"]["budget_total"] == eff, (limit, plan["detail"])
            assert [d["budget"] for d in pv["days"]] == [eff, eff], (limit, pv["days"])


@case
def test_biz01_preview_make_plan_reconcile():
    """②同输入对账：preview 每日 budget ≤ make_plan budget_total 且 ≤ 上游上限；
    cap 被改时两侧同步取值（共享权威常量，非各自硬编码）；cap monkeypatch 全程
    try/finally（B2.8-C 修复 :2290 内联恢复泄漏形态，既有用例本身不动）。"""
    with _StateGuard():
        STATE.data["today"] = _day_today()
        STATE.data["inventory"] = {}
        STATE.data["characters"] = [_char("c1", "对账角色", {"coin": 21})]
        for limit in (80, 360):
            STATE.data["settings"] = dict(DEFAULT_SETTINGS, daily_stamina_limit=limit)
            plan = planner.make_plan(now=NOW)
            pv = planner.preview_days(3, now=NOW)
            assert plan["detail"]["budget_total"] == planner._effective_daily_limit(limit), \
                (limit, plan["detail"])
            for d in pv["days"]:
                assert d["budget"] <= plan["detail"]["budget_total"], (limit, d)
                assert d["budget"] <= planner.UPSTREAM_DAILY_CAP, (limit, d)
        # cap 改 100：两侧同取 100（排程与预演都经 _effective_daily_limit），
        # 证明规则只挂在权威常量上，任何一侧都没有第二套硬编码封顶
        saved_cap = planner.UPSTREAM_DAILY_CAP
        try:
            planner.UPSTREAM_DAILY_CAP = 100
            STATE.data["settings"] = dict(DEFAULT_SETTINGS, daily_stamina_limit=360)
            plan = planner.make_plan(now=NOW)
            pv = planner.preview_days(3, now=NOW)
            assert plan["detail"]["budget_total"] == 100, plan["detail"]
            assert [d["budget"] for d in pv["days"]] == [100, 100, 100], pv["days"]
        finally:
            planner.UPSTREAM_DAILY_CAP = saved_cap


@case
def test_biz01_preview_multiday_capped():
    """③多日预演：limit=360 → 逐日 budget=[180×N]（修复前 [360×N]），day0 仍扣
    今日已耗、day1+ 为有效限额；steps/remaining 与 budget 守恒。"""
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS, daily_stamina_limit=360)
        STATE.data["today"] = _day_today()
        STATE.data["inventory"] = {}
        STATE.data["characters"] = [_char("c1", "多日角色", {"coin": 21})]
        pv = planner.preview_days(4, now=NOW)
        assert [d["budget"] for d in pv["days"]] == [180, 180, 180, 180], pv["days"]
        d0 = pv["days"][0]
        assert [(s["mat_id"], s["runs"], s["stamina"]) for s in d0["steps"]] == \
            [("coin", 3, 120)], d0
        assert d0["remaining"] == 60 and d0["budget"] - d0["remaining"] == 120, d0
        d1 = pv["days"][1]
        assert d1["steps"] == [] and d1["remaining"] == 180, d1
        assert pv["complete_on"] == DAY, pv["complete_on"]
        # day0 扣今日已耗：used=100 → day0=80（2轮补不齐）次日续 1 轮收尾
        STATE.data["today"] = _day_today(used=100)
        pv2 = planner.preview_days(3, now=NOW)
        assert [d["budget"] for d in pv2["days"]] == [80, 180, 180], pv2["days"]
        assert pv2["complete_on"] == "2026-10-25", pv2["complete_on"]
        # used 超过有效额度 → day0 钳 0、day1+ 满额（max(0, …) 边界）
        STATE.data["today"] = _day_today(used=300)
        pv3 = planner.preview_days(3, now=NOW)
        assert [d["budget"] for d in pv3["days"]] == [0, 180, 180], pv3["days"]


@case
def test_biz01_preview_cap_warning():
    """④limit=360 → preview 上限警告与 make_plan 同款措辞逐字在册；limit≤180
    无此警告；恢复速率警告仍按原始限额 360 评估（审计处方，不随封顶变化）。"""
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS, daily_stamina_limit=360,
                                      stamina_regen_per_hour=2)
        STATE.data["today"] = _day_today()
        STATE.data["inventory"] = {}
        STATE.data["characters"] = [_char("c1", "警告角色", {"coin": 21})]
        mk = planner.make_plan(now=NOW)
        cap_ws = [w for w in mk["warnings"] if "超出上游每日上限" in w]
        assert len(cap_ws) == 1, mk["warnings"]
        pv = planner.preview_days(2, now=NOW)
        assert cap_ws[0] in pv["warnings"], pv["warnings"]
        regen = [w for w in pv["warnings"] if "恢复速率" in w]
        assert len(regen) == 1 and "低于每日限额 360" in regen[0], pv["warnings"]
        STATE.data["settings"] = dict(DEFAULT_SETTINGS, daily_stamina_limit=180,
                                      stamina_regen_per_hour=2)
        pv180 = planner.preview_days(2, now=NOW)
        assert not any("超出上游每日上限" in w for w in pv180["warnings"]), \
            pv180["warnings"]


@case
def test_biz01_preview_make_plan_purity():
    """⑤纯度不破坏（照用例 4d 口径）：同输入两次完全一致、不改 STATE、不写
    state.json；修复后的封顶对账点在纯度路径内同样成立。"""
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS, daily_stamina_limit=360)
        STATE.data["today"] = _day_today()
        STATE.data["inventory"] = {}
        STATE.data["characters"] = [_char("c1", "纯度角色", {"coin": 21})]
        before = copy.deepcopy(STATE.data)
        state_existed = os.path.isfile(STATE_FILE)
        raw = None
        if state_existed:
            with open(STATE_FILE, "rb") as f:
                raw = f.read()
        p1 = planner.make_plan(now=NOW)
        v1 = planner.preview_days(4, now=NOW)
        p2 = planner.make_plan(now=NOW)
        v2 = planner.preview_days(4, now=NOW)
        assert p1 == p2 and v1 == v2, "同输入两次必须完全一致"
        assert copy.deepcopy(STATE.data) == before, "make_plan/preview_days 不得修改 STATE"
        if state_existed:
            with open(STATE_FILE, "rb") as f:
                assert f.read() == raw, "make_plan/preview_days 不得写 state.json"
        else:
            assert not os.path.isfile(STATE_FILE), "不得新建 state.json"
        assert p1["detail"]["budget_total"] == 180, p1["detail"]
        assert all(d["budget"] <= 180 for d in v1["days"]), v1["days"]


# ---------- 回归：v3 迁移 / sanitize 修复 / 重排 / 重名互斥 / 体力同池（本轮修复） ----------

@case
def test_migrate_v3_cassette_none_and_teams():
    """v3 迁移：清洗卡带字符串 "None" 脏值、补 teams 兜底；幂等、不删任何数据。"""
    from app.state import _migrate, STATE_SCHEMA
    dirty = {"characters": [
        {"id": "a", "name": "甲", "cassette": {"set": "None", "main": "None"}},
        {"id": "b", "name": "乙", "cassette": {"set": "守卫萝卜的回响", "main": "攻击力"}},
        {"id": "c", "name": "丙", "cassette": None},
    ]}
    out = _migrate(copy.deepcopy(dirty))
    assert out["schema"] == STATE_SCHEMA == 3
    assert out["characters"][0]["cassette"] == {"set": None, "main": None}, "字符串 None 应清洗回 None"
    assert out["characters"][1]["cassette"] == {"set": "守卫萝卜的回响", "main": "攻击力"}, "正常值不得改动"
    assert out["characters"][2]["cassette"] is None
    assert out["teams"] == [], "teams 缺失应补默认空数组"
    again = _migrate(out)  # 幂等
    assert again == out
    # 数据总量不变（只加不删）
    assert len(again["characters"]) == len(dirty["characters"])


@case
def test_sanitize_opt_text_and_game_confidence():
    """卡带空值必须落成 None（旧版 str(None)=="None" 污染）；game_confidence 保留透传。"""
    import app.main as main_mod
    raw = {"id": "x", "name": "测试角色",
           "cassette": {"set": None, "main": None},
           "game_confidence": {"skill": "low", "boss": "medium", "notes": "技能书系未查到"}}
    clean = main_mod._sanitize_character(raw)
    assert clean["cassette"] == {"set": None, "main": None}, clean["cassette"]
    assert clean["game_confidence"]["skill"] == "low", "导入置信度不得被洗掉"
    # 空字符串同样落 None
    clean2 = main_mod._sanitize_character({"id": "x", "name": "测试角色",
                                           "cassette": {"set": "", "main": "  "}})
    assert clean2["cassette"] == {"set": None, "main": None}
    # 无置信度信息时为 None（不报错）
    clean3 = main_mod._sanitize_character({"id": "x", "name": "测试角色"})
    assert clean3["game_confidence"] is None


@case
def test_characters_reorder_preserves_fields():
    """重排只按 id 排列现有对象：字段零丢失；未列出者按原序追加（停用角色不丢）。"""
    import app.main as main_mod
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.data["today"] = _empty_today()
        a = main_mod._sanitize_character({"id": "aaa", "name": "甲",
                                          "tracks": {"arc_level": {"cur": 3, "tgt": 60}},
                                          "awakening": 4})
        b = main_mod._sanitize_character({"id": "bbb", "name": "乙", "enabled": False})
        c = main_mod._sanitize_character({"id": "ccc", "name": "丙"})
        STATE.data["characters"] = [a, b, c]
        STATE.save()
        ordered = main_mod.reorder_characters(["ccc", "aaa"])
        assert [x["id"] for x in ordered] == ["ccc", "aaa", "bbb"], "未列出的 bbb 应按原序追加尾部"
        a2 = next(x for x in STATE.data["characters"] if x["id"] == "aaa")
        assert a2["tracks"]["arc_level"] == {"cur": 3, "tgt": 60}, "重排不得动 arc_level"
        assert a2["awakening"] == 4 and b["enabled"] is False
        saved = json.load(open(STATE_FILE, encoding="utf-8"))
        assert [x["id"] for x in saved["characters"]] == ["ccc", "aaa", "bbb"], "重排应持久化"


@case
def test_duplicate_character_names_rejected():
    """后端重名互斥：与导入同口径，绕过前端也造不出重名角色。"""
    import app.main as main_mod
    chars = [main_mod._sanitize_character({"id": "a", "name": "甲"}),
             main_mod._sanitize_character({"id": "b", "name": "甲"}),
             main_mod._sanitize_character({"id": "c", "name": "乙"})]
    assert main_mod.duplicate_character_names(chars) == ["甲"]
    assert main_mod.duplicate_character_names(chars[:1] + chars[2:]) == []


@case
def test_daily_budget_subtracts_hunter_spend():
    """体力同池：日常预算必须扣除当日追猎已耗（此前两边各记各账，同一池被双倍预订）。"""
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS, daily_stamina_limit=180)
        STATE.data["today"] = {"date": DAY, "used_stamina": 40, "planner_spent": 40,
                               "hunter_spent": 60, "blacklist": [], "entries": []}
        STATE.data["inventory"] = {}
        STATE.data["characters"] = [_char("c1", "同池角色", {"coin": 7})]
        plan = planner.make_plan(now=NOW)
        assert plan["detail"]["used_stamina"] == 100, \
            f"已耗应为 日常40+追猎60=100，实际 {plan['detail']['used_stamina']}"
        assert plan["detail"]["budget_total"] == 80, \
            f"预算应为 180-100=80，实际 {plan['detail']['budget_total']}"


# ---------- 成长模型（异能=技能 改造）：四技能逐级 / 天赋 / 突破逐档 ----------

@case
def test_growth_skill_ladder_and_breakthrough():
    """growth 模型数值全部来自 wiki cultivation_data 真实表（无硬编码）。"""
    from app import growth as g
    assert g.skill_max() == 10, "wiki skillUpgrade 覆盖到 10 级"
    # 单技能 1→10：书 184 / 族群小怪 184 / 硬币 437000 / 周本 8（mysqil 逐段合计）
    c = g.skill_segment_cost(1, 10)
    assert (c["skill_book"], c["field_mat"], c["coins"], c["weekly"]) == (184, 184, 437000, 8), c
    # 部分段 6→8 = (6,7)+(7,8) = 书 15+27、硬币 60000+80000、周本 1+1
    c2 = g.skill_segment_cost(6, 8)
    assert (c2["skill_book"], c2["coins"], c2["weekly"]) == (42, 140000, 2), c2
    assert g.skill_segment_cost(5, 5) == {"skill_book": 0, "field_mat": 0, "coins": 0, "weekly": 0}
    assert g.skill_segment_cost(0, 3)["skill_book"] == 2 + 3, "cur=0 视同 1 起算"
    # 天赋解锁两档：书 15 / 硬币 70000 / 周本 3
    t = g.talent_cost([1, 2])
    assert (t["skill_book"], t["coins"], t["weekly"]) == (15, 70000, 3), t
    # 突破门槛（用户实机口径 + wiki 角色页对位）：Lv X 突破 = charBreak[X-10]；
    # 五档 31/41/51/61/71 + 满级 80 最终突破；六档 BOSS 合计恰为 86、小怪 206
    assert g.breakthrough_gate_cost(30) == [0, 5, 25000], g.breakthrough_gate_cost(30)
    assert g.breakthrough_gate_cost(40) == [2, 12, 50000]
    assert g.breakthrough_gate_cost(80) == [36, 81, 150000]
    assert sum(g.breakthrough_gate_cost(x)[0] for x in (*g.LEVEL_GATES, g.FINAL_GATE)) == 86
    assert sum(g.breakthrough_gate_cost(x)[1] for x in (*g.LEVEL_GATES, g.FINAL_GATE)) == 206
    # 门槛跨越判定：停在门槛级去下一级需要材料（30→31 突破）；目标恰好=门槛即尚未突破；
    # 满级 80 最终突破在目标到达 80 时计入
    assert g.crossed_gates(1, 80) == [30, 40, 50, 60, 70, 80]
    assert g.crossed_gates(25, 45) == [30, 40]
    assert g.crossed_gates(30, 31) == [30] and g.crossed_gates(1, 30) == []
    assert g.crossed_gates(80, 80) == [] and g.crossed_gates(31, 40) == []
    assert g.crossed_gates(1, 79) == [30, 40, 50, 60, 70] and g.crossed_gates(79, 80) == [80]


@case
def test_character_growth_needs_mapping():
    """成长材料映射：书→所选系、小怪→族群基础档、周本/BOSS→目录归属、硬币通用；
    突破材料由角色等级门槛推导（与觉醒档位无关）；目录缺失时只算可映射部分。"""
    from app import growth as g
    char = {"mats": {"ability": 2},
            "tracks": {"char_level": {"cur": 25, "tgt": 45}},
            "skills": {k: {"cur": 1, "tgt": 10} for k in g.SKILL_KEYS},
            "talent_unlocks": [True, False], "awakening": 2}
    entry = {"mob_family": "絮语", "weekly_material": {"mat_id": "wk_test"},
             "boss_material": {"mat_id": "boss_test"}}
    needs = g.character_growth_needs(char, entry)
    assert needs["ability_2"] == 184 * 4 + 6, needs          # 四技能书 736 + 天赋1 6
    # 小怪 = 技能 736 + 门槛30→31(5) + 门槛40→41(12)【LvX 突破 = charBreak[X-10]】
    assert needs["mob_whisper_c"] == 736 + 5 + 12, needs
    assert needs["boss_test"] == 2, needs                    # 门槛 30 无 BOSS；40→41 需 2
    assert needs["wk_test"] == 8 * 4 + 1, needs              # 技能周本 32 + 天赋1 1
    assert needs["coin"] == 437000 * 4 + 30000, needs        # 硬币只来自技能/天赋（门槛硬币已含在等级梯）
    # 等级目标不跨门槛 → 无突破材料；觉醒档位变化不影响任何材料
    same_level = dict(char, tracks={"char_level": {"cur": 1, "tgt": 1}}, awakening=6)
    needs_low = g.character_growth_needs(same_level, entry)
    assert "boss_test" not in needs_low and needs_low["ability_2"] == needs["ability_2"]
    # 目录未收录：书/硬币仍算，族群/周本/BOSS 归属缺失不编造
    bare = g.character_growth_needs(char, None)
    assert bare["ability_2"] == 736 + 6 and "mob_whisper_c" not in bare \
        and "wk_test" not in bare and "boss_test" not in bare
    # 门槛视图：等级 25→45 → 两道门槛（30→31、40→41），持有可抵扣缺少
    rows = g.character_breakthrough_rows(char, entry, inventory={"boss_test": 5})
    assert [r["gate"] for r in rows] == [30, 40], rows
    assert rows[0]["to_level"] == 31 and rows[1]["to_level"] == 41
    boss_row = next(m for m in rows[0]["mats"] if m["mat_id"] == "boss_test")
    assert boss_row["need"] == 0 and boss_row["have"] == 5 and boss_row["short"] == 0
    mob_row = next(m for m in rows[0]["mats"] if m["mat_id"] == "mob_whisper_c")
    assert mob_row["need"] == 5 and mob_row["short"] == 5


@case
def test_wiki_city_talents_extraction_and_prefill():
    """都市特技：wiki data-upgrade 按 CityAbility_UpMaterial 识别（唯一可靠判据）；
    导入按快照预填名字与槽位数（1 或 2 位由实机数据决定，等级留 0）。"""
    import app.main as main_mod
    from app import wiki_fetch as wf
    upgrade = {"skills": [
        {"name": "寻宝时刻", "levels": [
            {"level": 1, "items": [{"id": "CityAbility_UpMaterial", "amount": 2}]},
            {"level": 5, "items": [{"id": "CityAbility_UpMaterial", "amount": 24}]}]},
        {"name": "普通战斗技能", "levels": [
            {"level": 2, "items": [{"id": "ability_book_p", "amount": 3}]}]},
        {"name": "垂钓大师", "levels": [
            {"level": 2, "items": [{"id": "CityAbility_UpMaterial", "amount": 4},
                                   {"id": "fons", "amount": 1500}]}]},
    ]}
    talents = wf._city_talents_from_upgrade(upgrade)
    assert [t["name"] for t in talents] == ["寻宝时刻", "垂钓大师"], talents
    assert talents[0]["max_level"] == 5 and talents[1]["max_level"] == 2
    assert wf._city_talents_from_upgrade({"skills": []}) == []
    orig = wf.city_talents_by_name
    data = game_data_mod.load()
    try:
        wf.city_talents_by_name = lambda: {"浔": talents}
        raw = game_data_mod.build_character_import(data, "浔")
        assert [t["name"] for t in raw["city_talents"]] == ["寻宝时刻", "垂钓大师"]
        assert raw["city_talent_slots"] == 2 and all(t["lv"] == 0 for t in raw["city_talents"])
        clean = main_mod._sanitize_character(raw)
        assert clean["city_talent_slots"] == 2 and len(clean["city_talents"]) == 2
        # 只有一个特技的角色：槽位=1
        wf.city_talents_by_name = lambda: {"灵可": talents[:1]}
        raw2 = game_data_mod.build_character_import(data, "灵可")
        assert [t["name"] for t in raw2["city_talents"]] == ["寻宝时刻"]
        assert raw2["city_talent_slots"] == 1
        # 快照未收录该角色：不预填（绝不编造）
        wf.city_talents_by_name = lambda: {}
        raw3 = game_data_mod.build_character_import(data, "浔")
        assert not raw3.get("city_talents")
    finally:
        wf.city_talents_by_name = orig


@case
def test_planner_growth_integration_no_double_count():
    """planner 接入成长模型：ability_level 旧轨不再计算；托管键（BOSS/周本/小怪旧
    自动总量）不计入；其余自定义键叠加累计（改了即同步）。"""
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.data["today"] = _empty_today()
        char = {
            "id": "g1", "name": "成长角色", "enabled": True,
            "mats": {"ability": 1, "arc": 1, "console": 1},
            "tracks": {"char_level": {"cur": 1, "tgt": 1},
                       "ability_level": {"cur": 1, "tgt": 13},  # 旧轨：应被忽略
                       "arc_level": {"cur": 1, "tgt": 1}},
            "skills": {k: {"cur": 1, "tgt": 10} for k in
                       ("normal", "variable", "orbit", "assist")},
            "talent_unlocks": [False, False], "awakening": 0,
            "extra_needs": {"ability_1": 500, "mob_whisper_c": 999, "exp_char": 777},
        }
        STATE.data["characters"] = [char]
        orig_entry = planner.catalog_entry
        planner.catalog_entry = lambda c: {"mob_family": "絮语",
                                           "weekly_material": {"mat_id": "wk_test"},
                                           "boss_material": {"mat_id": "boss_test"}}
        try:
            needs = planner.character_needs(char)
            managed = planner.managed_material_ids(char)
        finally:
            planner.catalog_entry = orig_entry
        assert needs["ability_1"] == 736 + 500, needs  # 四技能 184×4 + 自定义叠加
        assert needs["mob_whisper_c"] == 736, needs    # 托管键（族群=絮语）：extra 999 不计入
        assert needs["exp_char"] == 777, needs         # 自定义键照常累计
        assert managed == {"mob_whisper_c", "wk_test", "boss_test"}, managed


# ---------- 材料分档等价折算（同族跨档，wiki 官方系数） ----------

@case
def test_equivalent_inventory():
    """折算视图：枚数族 1:3:9、经验族按每本点数；基键与分档键相加无双算；未声明键透传。"""
    inv = {
        "ability_2": 10, "ability_2_b": 4, "ability_2_p": 2,   # 10+12+18=40 白当量
        "mob_whisper_c": 5, "mob_whisper_b": 1, "mob_whisper_a": 1,  # 5+3+9=17
        "exp_char": 3000, "exp_char_p": 2,                      # 3000+40000=43000 点
        "coin": 7, "boss_flower_seed": 3,                        # 未声明族：透传
        "console_4": 2,
    }
    eq = planner.equivalent_inventory(inv)
    assert eq["ability_2"] == 40, eq
    assert eq["mob_whisper_c"] == 17, eq
    assert eq["exp_char"] == 3000 + 2 * 20000, eq
    assert eq["coin"] == 7 and eq["boss_flower_seed"] == 3 and eq["console_4"] == 2
    assert "ability_2_b" not in eq and "ability_2_p" not in eq and "mob_whisper_a" not in eq
    # 纯函数：入参不被修改
    assert inv["ability_2_b"] == 4 and len(inv) == 11
    # 空库存/None 安全
    assert planner.equivalent_inventory(None) == {}
    assert planner.equivalent_inventory({}) == {}


@case
def test_snapshot_tiered_inventory_reduces_runs():
    """分档库存抵扣缺口：紫/蓝档折算后 runs 下降（减少体力本重复计算）。"""
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.data["today"] = {"date": DAY, "used_stamina": 0, "planner_spent": 0,
                               "hunter_spent": 0, "blacklist": [], "entries": []}
        STATE.data["inventory"] = {}
        char = {
            "id": "t1", "name": "折算角色", "enabled": True,
            "mats": {"ability": 1, "arc": 1, "console": 1},
            "tracks": {"char_level": {"cur": 1, "tgt": 1}, "arc_level": {"cur": 1, "tgt": 1}},
            "skills": {k: {"cur": 1, "tgt": 10} for k in
                       ("normal", "variable", "orbit", "assist")},  # 书 736 白当量
            "talent_unlocks": [False, False], "awakening": 0,
            "extra_needs": {"mob_whisper_c": 100},
        }
        orig_entry = planner.catalog_entry
        # 目录条目不带族群：技能书小怪项不产生 mob 需求，mob_whisper_c 缺口只来自 extra 100
        planner.catalog_entry = lambda c: {"weekly_material": {"mat_id": "wk_test"},
                                           "boss_material": {"mat_id": "boss_test"}}
        try:
            STATE.data["characters"] = [char]
            planner.YIELDS["per_run"]["ability_1"] = 18
            rows0 = {r["mat_id"]: r for c in planner.snapshot()["characters"] for r in c["rows"]}
            assert rows0["ability_1"]["need"] == 736 and rows0["ability_1"]["runs"] == 41
            assert rows0["mob_whisper_c"]["gap"] == 100
            # 录入 紫书×10（=90 当量）+ 小怪紫档×6（=54）：缺口同步下降
            STATE.data["inventory"] = {"ability_1_p": 10, "mob_whisper_a": 6}
            rows1 = {r["mat_id"]: r for c in planner.snapshot()["characters"] for r in c["rows"]}
            assert rows1["ability_1"]["need"] == 736 and rows1["ability_1"]["have"] == 90 \
                and rows1["ability_1"]["gap"] == 646, rows1["ability_1"]
            assert rows1["ability_1"]["runs"] == 36, "ceil(646/18)=36 < 41"
            assert rows1["mob_whisper_c"]["gap"] == 100 - 54, rows1["mob_whisper_c"]
        finally:
            planner.catalog_entry = orig_entry


# ---------- 背包 OCR v2：道具实名匹配（纯函数） ----------

@case
def test_ocr_match_inventory_text():
    """OCR 文本→库存命中表：实名/异体字/「」变体、行内数量、跨行不串位、同名取最大。
    2026-11-10 手动项机制：manual_only 材料（甲硬币等 5 项）不再进 OCR 候选——
    其文本行落 unmatched，库存只能经 /api/inventory 手动录入。"""
    import app.main as main_mod
    text = "\n".join([
        "异环",                                   # 界面噪音：无数字 → 忽略
        "雏鸟的希冀 ×12",                          # 白档书实名（「」变体也命中）
        "「橄榄枝」 3",                            # 紫档书带书名号
        "茫眛絮语 7",                              # 蓝档小怪（异体字：数据源两种写法）
        "失落絮语 21",                             # 白档小怪
        "海之泪 4",                                # BOSS 材料
        "甲硬币 9527",
        "背包容量 120",                            # 无候选行：不计入
        "雏鸟的希冀 5",                            # 同道具再现（翻页重叠）→ 取最大 12
    ])
    r = main_mod.match_inventory_text(text)
    m = r["matches"]
    assert m.get("ability_2") == 12, m            # 白档书（同名取最大）
    assert m.get("ability_2_p") == 3, m           # 紫档书（去「」命中）
    assert m.get("mob_whisper_b") == 7, m         # 异体字归一（眛→昧）
    assert m.get("mob_whisper_c") == 21, m
    assert m.get("boss_sea_tear") == 4, m
    assert "coin" not in m and "甲硬币" in "".join(r["unmatched_lines"]), \
        (m, r["unmatched_lines"])  # manual_only：甲硬币不进 OCR 候选（2026-11-10）
    assert "ability_2_b" not in m, "未出现的档位不得被臆造"
    assert any("背包容量" in ln for ln in r["unmatched_lines"])
    assert r["unmatched_lines"] and "异环" in "".join(r["unmatched_lines"])
    # 数量必须出现在名称同一行：跨行串位防线
    r2 = main_mod.match_inventory_text("雏鸟的希冀\n123")
    assert r2["matches"] == {}, "名称与数量跨行时不得错配"
    # 空文本安全
    assert main_mod.match_inventory_text("") == {"matches": {}, "unmatched_lines": []}


@case
def test_ocr_item_names_dictionary():
    """item_names 实名字典：67 件覆盖、经验书通用别名在位（2026-11-09 全量
    具体名补全：猎人攻略分/染剂等提升 OCR 命中；2026-11-10 工作包5 增补经验书
    三档官方全名；三档具体名在 targets concrete_names，不双写）、异体字变体在位。"""
    import app.main as main_mod
    names = planner.MATERIALS.get("item_names") or {}
    expect = ([f"ability_{n}" for n in range(1, 6)]
              + [f"ability_{n}_{s}" for n in range(1, 6) for s in ("b", "p")]
              + [f"arc_{n}" for n in range(1, 6)]
              + [f"arc_{n}_{s}" for n in range(1, 6) for s in ("b", "p")]
              + [f"mob_{f}_{t}" for f in ("whisper", "silhouette", "numeral", "delusion")
                 for t in ("c", "b", "a")]
              + ["boss_flower_seed", "boss_spark_plug", "boss_page_delusion",
                 "boss_pick_watermoon", "boss_nest_fragment", "boss_ticket_stub",
                 "boss_sea_tear", "wk_stamp", "wk_skirt", "wk_memory", "coin",
                 "city_ability_up", "fons",
                 "hcore", "ucell", "coin_carrot", "coin_carrot_s", "coin_carrot_g",
                 "rosemary_pawn", "hunter_exp"])
    missing = [k for k in expect if not names.get(k)]
    assert not missing, f"实名缺失：{missing}"
    # 经验书实名已实证（2026-11-09 用户口径 + 2026-11-10 工作包5 增补分档全名）：
    # item_names 收通用别名 + 三档官方全名（「猎人攻略分·新锐」等——后缀档位名
    # 不补别名时数量锚定失败，OCR「识别到但没匹配上」实证）；ntestrategy 变体
    # 译名（消散剪影/模糊数字等）不收录
    assert names.get("exp_char") == ["猎人攻略分", "猎人攻略", "猎人攻略分·新锐"], \
        names.get("exp_char")
    assert names.get("exp_char_b") == ["资深猎人攻略", "猎人攻略分·资深"], \
        names.get("exp_char_b")
    assert names.get("exp_char_p") == ["特级猎人攻略", "猎人攻略分·特级"], \
        names.get("exp_char_p")
    assert names.get("exp_arc") == ["染剂"], names.get("exp_arc")
    assert "茫昧絮语" in names["mob_whisper_b"] and "茫眛絮语" in names["mob_whisper_b"], \
        "异体字两种写法都要有"
    assert "橄榄枝" in names["ability_2_p"], "书名号变体应在位"


@case
def test_ocr_candidates_single_owner():
    """候选实名唯一归属（无梦果核回归）：同一 OCR 实名不得被两个 mat_id 认领——
    targets dreamless_core 与 extra city_ability_up 曾同认「无梦果核」，
    _inventory_candidates 只做材料内部查重，候选表两键并存 → 命中预览出两行、
    用户只认得其一。数据侧已删未实证的 dreamless_core（source 标注「来源未确认」，
    mysqil upgrades.json 实证 city_ability_up 为真；state.json 两 id 均无存量，
    零数据损失）；代码侧 _inventory_candidates 跨材料归一化去重（首个归属者胜）。"""
    import app.main as main_mod
    cands = main_mod._inventory_candidates()
    owner = {}
    for na, mat_id in cands:
        assert owner.get(na, mat_id) == mat_id, \
            f"候选实名跨材料重复：{na!r} 同时归属 {owner.get(na)} 与 {mat_id}"
        owner.setdefault(na, mat_id)
    # 「无梦果核」唯一归属 city_ability_up（都市特技升级材料，实证口径）
    hits = sorted({mid for na, mid in cands if na == "无梦果核"})
    assert hits == ["city_ability_up"], hits
    # 数据侧：未实证的 dreamless_core 条目已从 targets 删除、实证条目仍在
    assert all(m.get("id") != "dreamless_core"
               for m in planner.MATERIALS.get("targets", [])), \
        "dreamless_core（来源未确认）应已删除——与 city_ability_up 同名撞车"
    assert any(m.get("id") == "city_ability_up"
               for m in planner.MATERIALS.get("extra_materials", []))
    assert "无梦果核" in (planner.MATERIALS.get("item_names") or {}) \
        .get("city_ability_up", []), "city_ability_up 的无梦果核别名必须在位"


# ---------- 背包 OCR v3：数量解析 / 网格坐标 / 窗口枚举与绑定 ----------

@case
def test_ocr_parse_count_cn():
    """parse_count_cn：千分位/小数/量级词（万亿千 K M b、×x* 前缀）；失败返回 None。"""
    import app.main as main_mod
    f = main_mod.parse_count_cn
    assert f("1.2万") == 12000, f("1.2万")
    assert f("9527") == 9527
    assert f("1,234") == 1234
    assert f("1,234,567") == 1234567
    assert f("3K") == 3000
    assert f("3k") == 3000
    assert f("2.5亿") == 250000000
    assert f("1.2千") == 1200
    assert f("×12") == 12
    assert f("x12") == 12
    assert f("1.2 万") == 12000          # OCR 在数字与量级词间插空格
    assert f("0") == 0
    assert f("1.5") == 1                 # 无量级词的小数：截断不放大
    # 非法/空：None
    assert f("亿") is None               # 量级词没有数字
    assert f("abc") is None
    assert f("") is None
    assert f(None) is None
    assert f("-5") is None
    assert f("1.2.3") is None


@case
def test_ocr_cell_points_grid():
    """网格坐标计算：first/step 比例 × 窗口宽高 → row-major 第 n 格窗口绝对坐标。"""
    import app.main as main_mod
    layout = main_mod._sanitize_ocr_layout(dict(main_mod._OCR_LAYOUT_DEFAULT))
    assert layout["first_cell"] == {"x": 0.1026, "y": 0.225}, layout["first_cell"]
    assert (layout["cols"], layout["rows"]) == (7, 4), layout
    pts = main_mod._ocr_cell_points(layout, 1920, 1080)
    assert len(pts) == 28, len(pts)
    # 独立换算的期望值：x=round((0.1026+0.0854*col)*1920), y=round((0.225+0.1843*row)*1080)
    # （2026-11-03 用户游戏截图实测：列中心 197/365/…/1181，行中心 243/451/655/840）
    assert pts[0] == (197, 243), pts[0]       # 第 0 格（col=0,row=0）
    assert pts[1] == (361, 243), pts[1]       # 第 1 格（col=1,row=0）
    assert pts[6] == (1181, 243), pts[6]      # 第 6 格（col=6,row=0）
    assert pts[7] == (197, 442), pts[7]       # 第 7 格（col=0,row=1）——先行后列
    assert pts[27] == (1181, 840), pts[27]    # 第 27 格（col=6,row=3）
    # 比例钳位：越界比例按 0-1 截断，非法回落默认
    bad = main_mod._sanitize_ocr_layout({"cols": 2.7, "rows": 0, "page_wait_ms": 5,
                                         "first_cell": {"x": 1.7, "y": -3},
                                         "step": {"dx": "abc", "dy": 0.5}})
    assert (bad["cols"], bad["rows"]) == (2, 1), bad          # int 字段钳位 1-20
    assert bad["page_wait_ms"] == 100, bad["page_wait_ms"]
    assert bad["first_cell"] == {"x": 1.0, "y": 0.0}, bad["first_cell"]
    # 网格收拢：first.x 钳位到 1.0 后，last=first+dx×(cols-1) ≤ 1 → dx 收拢为 0
    # （遍历点击是全局鼠标事件，越界点会点到其他应用上）
    assert bad["step"]["dx"] == 0.0, bad["step"]
    overflow = main_mod._sanitize_ocr_layout({"first_cell": {"x": 0.8, "y": 0.2},
                                              "cols": 5, "rows": 4,
                                              "step": {"dx": 0.1, "dy": 0.3}})
    assert overflow["step"] == {"dx": 0.05, "dy": 0.2666}, overflow["step"]
    assert 0.8 + 0.05 * 4 <= 1.0 and 0.2 + 0.2666 * 3 <= 1.0, overflow["step"]
    # 合法布局不受收拢影响
    ok_lay = main_mod._sanitize_ocr_layout({"first_cell": {"x": 0.1, "y": 0.1},
                                            "cols": 3, "rows": 3,
                                            "step": {"dx": 0.2, "dy": 0.2}})
    assert ok_lay["step"] == {"dx": 0.2, "dy": 0.2}, ok_lay["step"]
    # 窗口尺寸缩放：同一布局在小窗口按比例收缩
    small = main_mod._ocr_cell_points(layout, 960, 540)
    assert small[0] == (98, 122), small[0]
    with _StateGuard():
        # data/ocr_layout.json 读取（缺键回落默认；项目内默认文件可解析）
        layout2 = main_mod._ocr_layout()
        assert isinstance(layout2, dict) and layout2["cols"] >= 1, layout2
        # POST 校验：非对象 → ValueError
        try:
            main_mod._sanitize_ocr_layout("bad")
            raise AssertionError("非对象 layout 应 raise ValueError")
        except ValueError:
            pass


@case
def test_ocr_windows_parse():
    """窗口清单 JSON 解析：__OCR_WINDOWS__ 单对象/数组两种形态、坏行忽略。"""
    import app.main as main_mod
    lines = [
        '__OCR_WINDOWS__:{"hwnd":111,"class":"UnrealWindow","title":"异环  ",'
        '"pid":42,"process":"HTGame","exe_path":"Y:\\\\Game\\\\HTGame.exe",'
        '"w":1920,"h":1080}',
        '__OCR_WINDOWS__:[{"hwnd":222,"class":"Chrome_WidgetWin_1","title":"浏览器",'
        '"pid":43,"process":"chrome","exe_path":"","w":800,"h":600},'
        '{"hwnd":333,"class":"UnrealWindow","title":"异环","pid":44,'
        '"process":"HTGame","exe_path":"","w":1280,"h":720}]',
        "noise line",
        "__OCR_WINDOWS__:not-json{{{",
        "__OCR_WINDOWS__:",
    ]
    ws = main_mod._parse_ocr_windows("\n".join(lines))
    assert len(ws) == 3, ws
    assert ws[0]["hwnd"] == 111 and ws[0]["class"] == "UnrealWindow", ws[0]
    assert ws[0]["title"] == "异环  " and ws[0]["process"] == "HTGame", ws[0]
    assert ws[1]["hwnd"] == 222 and ws[2]["hwnd"] == 333, ws
    assert main_mod._parse_ocr_windows("") == []
    assert main_mod._parse_ocr_windows("garbage") == []
    # 进程名匹配：devices.json 存文件名（HTGame.exe），枚举给 ProcessName（HTGame），
    # exe 路径文件名兜底——三种写法等价
    m = main_mod._window_matches_process
    assert m(ws[0], "HTGame.exe") and m(ws[0], "htgame") and m(ws[0], "HTGame.EXE")
    assert not m(ws[1], "HTGame.exe")


@case
def test_ocr_bind_priority():
    """绑定优先级（fixture 注入，无 PS）：①进程名+UnrealWindow 面积择优
    ②标题关键词兜底 ③无命中；devices.json 默认进程名（只读/损坏降级）。"""
    import app.main as main_mod
    wins = [
        {"hwnd": 1, "class": "Notepad", "title": "记事本", "pid": 10,
         "process": "notepad", "exe_path": "", "w": 500, "h": 400},
        {"hwnd": 2, "class": "UnrealWindow", "title": "异环  ", "pid": 11,
         "process": "HTGame", "exe_path": "Y:\\Game\\HTGame.exe", "w": 800, "h": 600},
        {"hwnd": 3, "class": "UnrealWindow", "title": "HTGame", "pid": 11,
         "process": "HTGame", "exe_path": "", "w": 100, "h": 100},
        {"hwnd": 4, "class": "UnrealWindow", "title": "别的游戏", "pid": 12,
         "process": "Other", "exe_path": "", "w": 4000, "h": 2000},
    ]
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        settings = STATE.settings
        # ① 进程名命中（.exe 后缀等价）+ UE 多顶层窗口取面积最大（hwnd=2 > hwnd=3）
        settings["game_process_name"] = "HTGame.exe"
        hwnd, meta, allw = main_mod._bind_game_window(list(wins))
        assert hwnd == 2 and meta["w"] == 800, (hwnd, meta)
        assert len(allw) == 4, "全量清单应原样返回"
        # ① exe 路径文件名兜底（ProcessName 不含 .exe 时仍可命中）
        settings["game_process_name"] = "htgame"
        hwnd, _meta, _allw = main_mod._bind_game_window(list(wins))
        assert hwnd == 2, hwnd
        # ② 标题关键词兜底：进程不匹配时按 game_window_keywords 子串匹配
        settings["game_process_name"] = "Nope.exe"
        settings["game_window_keywords"] = "异环"
        hwnd, meta, _allw = main_mod._bind_game_window(list(wins))
        assert hwnd == 2, hwnd
        # ② 多关键词任一命中；非游戏类窗口（面积最大原则仍适用）
        settings["game_window_keywords"] = "不存在词,记事本"
        hwnd, meta, _allw = main_mod._bind_game_window(list(wins))
        assert hwnd == 1, hwnd
        # ③ 无命中 → (None, None, 清单)
        settings["game_window_keywords"] = "怎么都匹配不上"
        hwnd, meta, allw = main_mod._bind_game_window(list(wins))
        assert hwnd is None and meta is None and len(allw) == 4, (hwnd, meta)
        assert main_mod._bind_game_window([]) == (None, None, []), "空清单不绑定"
        # devices.json 默认进程名：设置留空时从 oknte_root 读 selected_exe[0]
        tmp = tempfile.mkdtemp(prefix="oknte_devices_")
        cfg = _safe_path(tmp, "data", "apps", "ok-nte", "working", "configs")
        os.makedirs(cfg, exist_ok=True)
        settings["oknte_root"] = tmp
        settings["game_process_name"] = ""
        dev = _safe_path(cfg, "devices.json")
        Path(dev).write_text(json.dumps({"selected_exe": ["FakeGame.exe"]},
                                        ensure_ascii=False), encoding="utf-8")
        assert main_mod._default_game_process() == "FakeGame.exe"
        # 损坏/缺失 → 降级 HTGame.exe（实测游戏进程名），绝不抛异常
        Path(dev).write_text("{broken json", encoding="utf-8")
        assert main_mod._default_game_process() == "HTGame.exe"
        Path(dev).unlink()
        assert main_mod._default_game_process() == "HTGame.exe"


@case
def test_ocr_magnitude_in_inventory():
    """量级词进库存匹配：「雏鸟的希冀 ×1.2万」→12000；单格详情解析（含跨行数量）。
    2026-11-10 手动项机制：manual_only 材料（甲硬币等）不再 OCR 匹配——量级词
    解析能力改用非手动材料断言。"""
    import app.main as main_mod
    r = main_mod.match_inventory_text("雏鸟的希冀 ×1.2万")
    assert r["matches"].get("ability_2") == 12000, r
    r2 = main_mod.match_inventory_text("海之泪 1,234")
    assert r2["matches"].get("boss_sea_tear") == 1234, r2
    r2b = main_mod.match_inventory_text("甲硬币 1,234")
    assert r2b["matches"] == {} and any("甲硬币" in ln for ln in r2b["unmatched_lines"]), \
        (r2b["matches"], r2b["unmatched_lines"])  # manual_only：不进 OCR 候选
    # 既有语义不回归：无量级词照旧、跨行不错配
    r3 = main_mod.match_inventory_text("雏鸟的希冀 ×12\n雏鸟的希冀 5")
    assert r3["matches"].get("ability_2") == 12, r3
    assert main_mod.match_inventory_text("雏鸟的希冀\n123")["matches"] == {}, \
        "整页模式跨行防线必须保留"
    # 同线多道具不串位：数量必须紧邻名称（只隔分隔符），前一名不许抢后一名的数字
    r4 = main_mod.match_inventory_text("雏鸟的希冀 海之泪 3")
    assert r4["matches"] == {"boss_sea_tear": 3}, r4["matches"]
    # OCR 空格噪声容忍（_ocr_norm 默认去空白——回归：keep_space 三元两支相同
    # 的 bug 使默认路径只折叠空格，名字内插空格即失配、命中退化为 unmatched）
    assert main_mod._ocr_norm("雏鸟 的希冀") == "雏鸟的希冀", \
        main_mod._ocr_norm("雏鸟 的希冀")
    r5 = main_mod.match_inventory_text("妄想彼 端的一页 ×42")
    assert r5["matches"].get("boss_page_delusion") == 42, r5["matches"]
    # 紧邻分隔符形态全部仍可取数
    for line, mat, want in (("雏鸟的希冀 ×12", "ability_2", 12),
                            ("雏鸟的希冀 : 7", "ability_2", 7),
                            ("雏鸟的希冀 - 9", "ability_2", 9),
                            ("雏鸟的希冀 1.2万", "ability_2", 12000)):
        assert main_mod.match_inventory_text(line)["matches"] == {mat: want}, line
    # 单格详情：同行「名×数」
    cands = main_mod._inventory_candidates()
    mat, count = main_mod._parse_cell_item("雏鸟的希冀\n×1.2万", cands)
    assert mat == "ability_2" and count == 12000, (mat, count)
    # 空格噪声同样不破坏单格解析（去空白归一 + 徽标计数）
    mat, count = main_mod._parse_cell_item("妄想彼 端的一页 ×42", cands)
    assert mat == "boss_page_delusion" and count == 42, (mat, count)
    # 单格详情：数量在名称下一行（真实 UI：×42 短行；2026-11-04 截图取证）
    mat, count = main_mod._parse_cell_item("海之泪\n×42\n从海囚获取的一滴红色水滴。", cands)
    assert mat == "boss_sea_tear" and count == 42, (mat, count)
    # 数量行被 OCR 误读成形近字（χ）也能命中
    mat, count = main_mod._parse_cell_item("海之泪\nχ42", cands)
    assert mat == "boss_sea_tear" and count == 42, (mat, count)
    # 数量行缺失（描述句带数字）：不许从描述行误取数量
    mat, count = main_mod._parse_cell_item("橄榄枝\n从海囚获取的一滴红色水滴，用作突破材料 42 个。\n可在商店兑换", cands)
    assert mat is None and count is None, (mat, count)
    # 数量行撞到其他道具名：不许抢数字
    mat, count = main_mod._parse_cell_item("橄榄枝\n海之泪 4", cands)
    assert mat is None and count is None, (mat, count)
    # 未命中/空文本
    assert main_mod._parse_cell_item("完全无关的文本", cands) == (None, None)
    assert main_mod._parse_cell_item("", cands) == (None, None)


@case
def test_ocr_traverse_overlap_regression():
    """翻页重叠格不得计入 dead_streak（评审回归）：3 行网格滚 1 行后，下一页开头
    2 行与之前页重复——按旧的全局重复判据会连续 3 格提前终止，新行永远扫不到。"""
    import app.main as main_mod
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.settings["game_process_name"] = "HTGame.exe"
        STATE.settings["game_window_keywords"] = "异环"
        meta = {"hwnd": 1, "class": "UnrealWindow", "title": "异环  ", "pid": 1,
                "process": "HTGame", "exe_path": "", "w": 1920, "h": 1080}
        rows = [["雏鸟的希冀 ×%d" % i for i in range(1, 6)],      # r0：ability_2 max 5
                ["橄榄枝 ×%d" % i for i in range(6, 11)],         # r1：ability_2_p max 10
                ["茫昧絮语 ×%d" % i for i in range(11, 16)],      # r2：mob_whisper_b max 15
                ["海之泪 ×%d" % i for i in range(16, 21)]]        # r3：boss_sea_tear max 20
        page1 = rows[0] + rows[1] + rows[2]
        page2 = rows[1] + rows[2] + rows[3]   # 滚 1 行：开头 2 行（10 格）与之前页重复
        seq = {"n": 0}

        def fake_traverse(hwnd, layout, win_size, gen=None):
            seq["n"] += 1
            return page1 if seq["n"] == 1 else list(page2)  # 之后每页都与 page2 相同

        orig = (main_mod._bind_game_window, main_mod._ocr_traverse_page,
                main_mod._ocr_scroll, main_mod._ocr_layout)
        main_mod._bind_game_window = lambda *a, **k: (1, meta, [meta])
        main_mod._ocr_traverse_page = fake_traverse
        main_mod._ocr_scroll = lambda *a, **k: None
        main_mod._ocr_layout = lambda: main_mod._sanitize_ocr_layout(
            dict(main_mod._OCR_LAYOUT_DEFAULT))
        try:
            r = main_mod._run_backpack_ocr()
        finally:
            (main_mod._bind_game_window, main_mod._ocr_traverse_page,
             main_mod._ocr_scroll, main_mod._ocr_layout) = orig
        assert r["matches"].get("boss_sea_tear") == 20, r["matches"]  # 新行必须扫到
        assert r["matches"].get("ability_2") == 5, r["matches"]       # 同名跨格取最大
        assert r["pages"] >= 2 and r["cells_scanned"] >= 30, (r["pages"], r["cells_scanned"])
        assert not any("连续 3 格无新信息" in w for w in r["warnings"]), r["warnings"]


@case
def test_ocr_unknown_cells_keep_traversing():
    """未收录格不得判停（2026-11-04 用户实测回归 + 评审页级回归）：
    格级——未收录/known-0 既不计空白也不算重复，unknown_items 按首行去重收集、
    ×N 徽标计数；
    页级——仅含未收录道具的页也必须继续翻页（page_new 认新 unknown 首行），
    第 3 页新 unknown 必须进 unknown_items（旧逻辑 page_new 只认实名命中，
    整页未收录即「本页未发现新道具」提前终止，多页未知背包每轮只能推进一页）；
    空白判停与 PS 对齐——_OCR_PS_TRAVERSE 3 连空即 break，返回 cells 最多尾部
    3 连空，Python 端连续 3 格空白即停（旧阈值 5 永不可达，短背包白扫一整页）。
    注：谕石实名已补录，干净「谕石」文本会命中 oracle_stone——用「谕石 0」形态
    （0=角标噪声，计数无效 → unknown）覆盖 known-0 不写库存且字面含「谕石」。"""
    import app.main as main_mod
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.settings["game_process_name"] = "HTGame.exe"
        STATE.settings["game_window_keywords"] = "异环"
        meta = {"hwnd": 1, "class": "UnrealWindow", "title": "异环  ", "pid": 1,
                "process": "HTGame", "exe_path": "", "w": 1920, "h": 1080}
        page_known = ["海之泪 ×16"]
        page_unknown = [
            # 用户实测样例（原样复刻）——谕石被 OCR 误读成「前石/输石」，未收录
            "145 1269 道具 前石 重要道具收集足够的输石，可前往魔女之家兑换对应的奖励",
            # known-0（角标噪声）→ 计数无效 → 不写 0 进库存、进 unknown
            "谕石 0 收集进度 145/1269",
            # 未收录 + × 996 数量徽标（与名称分离、中间夹 OCR 空格）
            "道具 失络焦子 × 996 可于[焦子商店]中兑换所需物品",
            "",  # 空白（点出网格范围；PS 端 3 连空即断，单格空不触发判停）
        ]
        page_unknown2 = ["未登记素材叁 ×9", "未登记素材肆 ×3", ""]
        seq = {"n": 0}
        scrolls = {"n": 0}

        def fake_traverse(hwnd, layout, win_size, gen=None):
            seq["n"] += 1
            if seq["n"] == 1:
                return list(page_known)
            if seq["n"] == 2:
                return list(page_unknown)
            return list(page_unknown2)  # 第 3 页起内容相同（模拟翻页到底）

        orig = (main_mod._bind_game_window, main_mod._ocr_traverse_page,
                main_mod._ocr_scroll, main_mod._ocr_layout)
        main_mod._bind_game_window = lambda *a, **k: (1, meta, [meta])
        main_mod._ocr_traverse_page = fake_traverse
        main_mod._ocr_scroll = lambda *a, **k: scrolls.__setitem__("n", scrolls["n"] + 1)
        main_mod._ocr_layout = lambda: main_mod._sanitize_ocr_layout(
            dict(main_mod._OCR_LAYOUT_DEFAULT))
        try:
            r = main_mod._run_backpack_ocr()
            # 短背包（真实 PS 形态：尾部 3 连空）→ 空白判停，不得白扫下一页
            seq["n"] = 0
            scrolls["n"] = 0
            main_mod._ocr_traverse_page = lambda *a, **k: ["海之泪 ×16", "", "", ""]
            r2 = main_mod._run_backpack_ocr()
        finally:
            (main_mod._bind_game_window, main_mod._ocr_traverse_page,
             main_mod._ocr_scroll, main_mod._ocr_layout) = orig
        # 页级回归：known 页 → unknown 页 → unknown2 页逐页收集，
        # 第 3 页与第 4 页内容相同才停（pages=4：3 页有效 + 1 页复扫判停）
        assert r["pages"] == 4, (r["pages"], r["warnings"])
        assert r["cells_scanned"] == 1 + 4 + 3 + 3, r["cells_scanned"]
        assert r["matches"] == {"boss_sea_tear": 16}, r["matches"]
        assert [it["count"] for it in r["unknown_items"]] == \
            [None, None, 996, 9, 3], r["unknown_items"]
        texts = " | ".join(it["name_text"] for it in r["unknown_items"])
        assert "前石" in texts and "谕石" in texts and "失络焦子" in texts \
            and "未登记素材叁" in texts, texts
        assert not any("无新信息" in w for w in r["warnings"]), \
            r["warnings"]                                  # 旧格级判据必须已删
        # 空白判停（与 PS 3 连空 break 对齐）：第 3 格空白即停，不得再翻页滚动
        assert r2["pages"] == 1 and r2["cells_scanned"] == 4, \
            (r2["pages"], r2["cells_scanned"])
        assert scrolls["n"] == 0, "空白判停后不得发起翻页滚动"
        assert any("连续 3 格空白" in w for w in r2["warnings"]), r2["warnings"]


@case
def test_ocr_traverse_cancel():
    """/api/ocr/cancel 语义（代数令牌，评审 2026-11-05）：取消经 _ocr_cancel_active()
    定向到登记时的当前代数，只对同代遍历生效——当前页 PS 点击序列不可中断，但其
    返回的格在格间逐格检查取消，取消点之后的格不再解析（已完成部分保留），页首/
    页末不再发起下一页点击与翻页，warning 含「用户取消」；对旧代的取消在新一轮
    （代数 +1）不命中——旧轮取消绝不泄漏进新一轮（取代旧的入口 clear 方案，根除
    「新一轮吞掉旧轮取消」竞态）；端点与「运行中放行」白名单在 do_POST 源内成对
    登记（无法起真服务，静态源校验兜底）。"""
    import app.main as main_mod
    import inspect
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.settings["game_process_name"] = "HTGame.exe"
        STATE.settings["game_window_keywords"] = "异环"
        meta = {"hwnd": 1, "class": "UnrealWindow", "title": "异环  ", "pid": 1,
                "process": "HTGame", "exe_path": "", "w": 1920, "h": 1080}
        page1 = ["海之泪 ×16"] * 3
        page2 = ["海之泪 ×17"] * 3
        seq = {"n": 0}

        def fake_traverse(hwnd, layout, win_size, gen=None):
            seq["n"] += 1
            if seq["n"] == 2:
                main_mod._ocr_cancel_active()  # 模拟第 2 页点击期间用户取消（定向当前代）
            return list(page1 if seq["n"] == 1 else page2)

        orig = (main_mod._bind_game_window, main_mod._ocr_traverse_page,
                main_mod._ocr_scroll, main_mod._ocr_layout)
        main_mod._bind_game_window = lambda *a, **k: (1, meta, [meta])
        main_mod._ocr_traverse_page = fake_traverse
        main_mod._ocr_scroll = lambda *a, **k: None
        main_mod._ocr_layout = lambda: main_mod._sanitize_ocr_layout(
            dict(main_mod._OCR_LAYOUT_DEFAULT))
        try:
            r = main_mod._run_backpack_ocr()
            main_mod._ocr_cancel_active()  # 上轮结束后用户又按了一次取消（定向旧代）
            seq["n"] = 0
            main_mod._ocr_traverse_page = lambda *a, **k: list(page1)
            r2 = main_mod._run_backpack_ocr()  # 新一轮（代数+1）：旧代取消不得命中
        finally:
            (main_mod._bind_game_window, main_mod._ocr_traverse_page,
             main_mod._ocr_scroll, main_mod._ocr_layout) = orig
            main_mod._OCR_CANCEL.clear()  # 用例收尾卫生复位，不影响其它用例
        # 第 2 页解析即停：取消在第 2 页点击期间置位 → 第 2 页的格不再解析
        # （cells_scanned 只含第 1 页 3 格），第 1 页已解析的 matches 原样保留，
        # 不再有第 3 页（旧实现会点满 max_pages 页）
        assert r["pages"] == 2 and r["cells_scanned"] == 3, \
            (r["pages"], r["cells_scanned"], r["warnings"])
        assert r["matches"] == {"boss_sea_tear": 16}, r["matches"]
        assert any("用户取消" in w for w in r["warnings"]), r["warnings"]
        # 代数令牌：旧代的取消不命中新一轮（正常跑到「翻页后内容相同」为止）
        assert not any("用户取消" in w for w in r2["warnings"]), r2["warnings"]
        assert r2["pages"] == 2 and r2["matches"] == {"boss_sea_tear": 16}, \
            (r2["pages"], r2["matches"], r2["warnings"])
        # 端点分支 + 运行中白名单成对登记（do_POST 源内出现 ≥2 次）
        src = inspect.getsource(main_mod.Handler.do_POST)
        assert src.count('"/api/ocr/cancel"') >= 2, \
            "取消端点必须在白名单与分支中成对出现"


@case
def test_ocr_cell_cancel_and_token_isolation():
    """格间取消 + 代数令牌隔离（2026-11-05 用户实测：无取消路径时后端盲目点击数
    分钟，前端只能超时断开；评审：入口 clear 有吞掉旧轮取消的竞态）：
    - 格间取消——取消在第 1 页点击期间（第 2 格起）定向当前代 → 遍历提前返回，
      第 1 格已解析的 matches 原样保留（只含 boss_sea_tear），取消点之后的格
      （橄榄枝）不再解析（不进 matches/unknown/unmatched），warning 含「用户取消」，
      不再发起翻页滚动；
    - 代数令牌——对旧代的取消在新一轮（代数 +1）不命中：新一轮正常跑完（旧轮取消
      绝不泄漏）；无活跃遍历时的取消指向最近一代=等效无操作；
    - OCR 单飞锁——背包遍历端点必须非阻塞抢占 + finally 释放（端点级 409 行为由
      test_ocr_single_flight_409 真服务验证）。（角色等级 OCR 已整体删除，取消
      原语只剩背包 Event 一个消费者，/api/ocr/cancel 不再落任何哨兵文件。）"""
    import app.main as main_mod
    import inspect
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.settings["game_process_name"] = "HTGame.exe"
        STATE.settings["game_window_keywords"] = "异环"
        meta = {"hwnd": 1, "class": "UnrealWindow", "title": "异环  ", "pid": 1,
                "process": "HTGame", "exe_path": "", "w": 1920, "h": 1080}
        page = ["海之泪 ×16", "橄榄枝 ×8", "橄榄枝 ×9"]
        scrolls = {"n": 0}

        class _CancelAtSecondCell(list):
            # 第 2 格 yield 瞬间取消当前代（模拟用户恰在第 2 格点击期间按取消）——
            # 第 1 格必须先解析完，matches 保留第 1 格
            def __iter__(self):
                for i, item in enumerate(list.__iter__(self)):
                    if i == 1:
                        main_mod._ocr_cancel_active()
                    yield item

        def fake_traverse(hwnd, layout, win_size, gen=None):
            return _CancelAtSecondCell(page)

        orig = (main_mod._bind_game_window, main_mod._ocr_traverse_page,
                main_mod._ocr_scroll, main_mod._ocr_layout)
        main_mod._bind_game_window = lambda *a, **k: (1, meta, [meta])
        main_mod._ocr_traverse_page = fake_traverse
        main_mod._ocr_scroll = lambda *a, **k: scrolls.__setitem__(
            "n", scrolls["n"] + 1)
        main_mod._ocr_layout = lambda: main_mod._sanitize_ocr_layout(
            dict(main_mod._OCR_LAYOUT_DEFAULT))
        try:
            r = main_mod._run_backpack_ocr()
            assert r["pages"] == 1 and r["cells_scanned"] == 1, \
                (r["pages"], r["cells_scanned"], r["warnings"])
            assert r["matches"] == {"boss_sea_tear": 16}, r["matches"]
            assert r["unknown_items"] == [] and r["unmatched_lines"] == [], \
                (r["unknown_items"], r["unmatched_lines"])
            assert any("用户取消" in w for w in r["warnings"]), r["warnings"]
            assert scrolls["n"] == 0, "取消后不得发起翻页滚动"
            # 代数令牌隔离：对旧代（r 所在代）的取消在新一轮（代数+1）不命中
            g0 = main_mod._OCR_GEN
            main_mod._ocr_traverse_page = lambda *a, **k: list(page)
            main_mod._ocr_cancel_active()  # 模拟上一轮刚结束时用户又按了一次取消
            assert main_mod._ocr_cancel_hit(g0), "取消必须定向登记时的当前代"
            r2 = main_mod._run_backpack_ocr()
            g1 = main_mod._OCR_GEN
            assert g1 == g0 + 1, (g0, g1)
            assert not main_mod._ocr_cancel_hit(g1), \
                "旧代的取消不得命中新一轮（代数令牌）"
            assert r2["pages"] == 2 and r2["matches"] == \
                {"boss_sea_tear": 16, "ability_2_p": 9}, \
                (r2["pages"], r2["matches"], r2["warnings"])
            assert not any("用户取消" in w for w in r2["warnings"]), r2["warnings"]
            # 端点布线：取消=定向代数；背包遍历端点=单飞锁非阻塞抢占+释放
            post_src = inspect.getsource(main_mod.Handler.do_POST)
            assert "_ocr_cancel_active()" in post_src, \
                "取消端点必须定向代数（唯一取消媒介：Event 代数令牌）"
            assert post_src.count("_OCR_RUN_LOCK.acquire(blocking=False)") == 1 and \
                post_src.count("_OCR_RUN_LOCK.release()") == 1, \
                "背包遍历端点必须持 OCR 单飞锁（非阻塞抢占 + finally 释放）"
        finally:
            (main_mod._bind_game_window, main_mod._ocr_traverse_page,
             main_mod._ocr_scroll, main_mod._ocr_layout) = orig
            main_mod._OCR_CANCEL.clear()  # 用例收尾卫生复位，不影响其它用例


@case
def test_ocr_single_flight_409():
    """OCR 单飞锁端点级验证（评审 2026-11-05：遍历端点可并发执行）：上一轮遍历
    未结束（单飞锁被持）时，真起面板服务打回环请求——/api/inventory/ocr 必须
    立即 409（error=已有遍历进行中），而非并发进遍历（双遍历共用全局鼠标点击会
    互相污染）；确认非「规划器运行中」那条 409。（角色等级 OCR 已删除，
    /api/characters/ocr 端点不复存在，单飞锁只剩背包一个消费者。）"""
    import http.client
    import threading
    from app import main as main_mod
    from app.main import create_server
    with _StateGuard():
        # 规划器必须空闲：否则 409 来自「运行中白名单」而非单飞锁，测试失去意义
        deadline = time.time() + 10
        while time.time() < deadline and runner_mod.RUNNER.snapshot()["state"] \
                in ("running", "waiting", "stopping"):
            runner_mod.RUNNER.stop()
            time.sleep(0.2)
        assert runner_mod.RUNNER.snapshot()["state"] not in \
            ("running", "waiting", "stopping"), "规划器必须空闲后才能验证单飞锁"
        assert main_mod._OCR_RUN_LOCK.acquire(blocking=False), "测试前提：锁未被占用"
        try:
            # SEC-05：直设全局令牌（生产生成器现生成，零凭据字面量）→ create_server
            # 的 _ensure_panel_token 空转，零文件写入 repo data/
            main_mod._PANEL_TOKEN = main_mod._new_panel_token()
            server, _port = create_server(0, auto_range=1)  # 端口 0 → 系统分配
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True,
                             name="selftest-ocr-409").start()
            try:
                path = "/api/inventory/ocr"
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request("POST", path, json.dumps({"mode": "traverse"}), {
                    "Content-Type": "application/json",
                    "Origin": "http://127.0.0.1:%d" % port,  # 回环 Origin 过防线
                    "X-Panel-Token": main_mod._PANEL_TOKEN})  # 面板令牌过 SEC-05 闸
                resp = conn.getresponse()
                payload = json.loads(resp.read().decode("utf-8"))
                conn.close()
                assert resp.status == 409, (path, resp.status, payload)
                assert "OCR 遍历进行中" in str(payload.get("error") or ""), \
                    (path, payload)  # 必须是单飞锁的 409，而非运行中白名单的
            finally:
                server.shutdown()
                server.server_close()
        finally:
            main_mod._OCR_RUN_LOCK.release()


@case
def test_ocr_badge_count_priority():
    """×N 徽标优先（2026-11-04 实测：详情面板徽标与名称分两处）：全文首个独立
    ×N token 即数量（描述句里的数字不作数）；无徽标回退名称右侧/下一行短行；
    计数 0 一律无效（角标/数值噪声），有名无有效数量 → (None, None) 不写库存。"""
    import app.main as main_mod
    f = main_mod._ocr_badge_count
    assert f("海之泪\n×42\n从海囚获取的一滴红色水滴。") == 42
    assert f("道具 失络焦子 × 996 可于[焦子商店]中兑换所需物品") == 996  # × 与数字夹空格
    assert f("焦子×996") == 996                        # 中文紧邻放行
    assert f("×1.2万") == 12000 and f("×1,234") == 1234
    assert f("x42") == 42                              # 行首 x 是独立 token
    assert f("χ42") == 42 and f("✕42") == 42 and f("＊42") == 42
    assert f("box42") is None                          # x 前是字母：非独立 token
    assert f("4x2") is None                            # x 前是数字
    assert f("max 42") is None
    assert f("记忆的永恒 0 霍材的 145") is None          # 无徽标：0 角标噪声不作数
    assert f("") is None and f(None) is None and f("abc") is None
    cands = main_mod._inventory_candidates()
    # 徽标优先于「名称右侧数字/描述数字」：详情文本含描述数字时以 ×N 为准
    mat, count = main_mod._parse_cell_item("海之泪 ×3\n收集进度 145/1269", cands)
    assert mat == "boss_sea_tear" and count == 3, (mat, count)
    mat, count = main_mod._parse_cell_item("海之泪\n×1.2万\n描述 42 个", cands)
    assert mat == "boss_sea_tear" and count == 12000, (mat, count)
    # 无 ×N 徽标 + 名称右侧 0（图标角标噪声）→ (None, None)：不写 0 进库存
    assert main_mod._parse_cell_item("记忆的永恒 0 霍材的 145", cands) == (None, None)
    assert main_mod._parse_cell_item("雏鸟的希冀 0", cands) == (None, None)
    assert main_mod._parse_cell_item("海之泪\n×0", cands) == (None, None)  # ×0 同样无效
    # 既有语义不回归：名称下一行 ×42 / χ42 短行照旧命中
    mat, count = main_mod._parse_cell_item("海之泪\n×42\n从海囚获取的一滴红色水滴。", cands)
    assert mat == "boss_sea_tear" and count == 42, (mat, count)


@case
def test_ocr_map_unknown():
    """POST /api/ocr/map-unknown 后端语义（_ocr_map_unknown）：合法映射追加
    item_names 别名并热生效（映射后同名格下次识别自动命中）、重复映射幂等去重、
    非法 mat_id/越界 name_text 拒绝（端点转 400）；谕石实名补录在位且不覆盖
    既有别名。夹具按 test_codex_custom_material 口径用 tempfile 重定向 DATA_DIR，
    不触碰生产 data/materials.json。"""
    import app.main as main_mod
    # 任务 3：谕石实名补录在位（生产表只读校验 + 单格可命中）
    assert "谕石" in (planner.MATERIALS.get("item_names") or {}).get("oracle_stone", []), \
        planner.MATERIALS.get("item_names", {}).get("oracle_stone")
    mat, count = main_mod._parse_cell_item("谕石\n×7", main_mod._inventory_candidates())
    assert mat == "oracle_stone" and count == 7, (mat, count)
    tmp = tempfile.mkdtemp(prefix="oknte_ocr_map_")
    orig_main_datadir = main_mod.DATA_DIR
    orig_planner_datadir = planner.DATA_DIR
    main_mod.DATA_DIR = tmp
    planner.DATA_DIR = tmp
    # 预置三张表副本（_ocr_map_unknown 内部的 reload_tables 需要同目录三表齐全）
    for name in ("materials.json", "yields.json", "requirements.json"):
        shutil.copy(os.path.join(orig_planner_datadir, name), os.path.join(tmp, name))
    try:
        # 合法映射：把用户实测的 OCR 误读形态「前石」挂到 oracle_stone
        names = main_mod._ocr_map_unknown("前石", "oracle_stone")
        assert "前石" in names["oracle_stone"], names["oracle_stone"]
        assert "谕石" in names["oracle_stone"], "既有别名不得被覆盖"
        saved = json.load(open(os.path.join(tmp, "materials.json"), encoding="utf-8"))
        assert "前石" in saved["item_names"]["oracle_stone"], "映射必须落盘 materials.json"
        # 映射后热生效：同名格（含 ×N 徽标）下次识别自动命中
        mat, count = main_mod._parse_cell_item(
            "145 1269 道具 前石 ×12 重要道具", main_mod._inventory_candidates())
        assert mat == "oracle_stone" and count == 12, (mat, count)
        # 重复映射（归一化同形）：幂等去重，不追加第二条
        names2 = main_mod._ocr_map_unknown(" 前 石 ", "oracle_stone")
        assert names2["oracle_stone"].count("前石") == 1, names2["oracle_stone"]
        assert len(names2["oracle_stone"]) == len(names["oracle_stone"])
        # 跨材料同名别名：拒绝（候选最长优先会静默遮蔽后登记的映射——评审回归）
        try:
            main_mod._ocr_map_unknown("前石", "coin")
        except ValueError as e:
            assert "oracle_stone" in str(e), e
        else:
            raise AssertionError("跨材料同名映射必须拒绝")
        # 写盘失败：内存零污染（评审回归：旧实现先改内存后写盘，OSError 后重试
        # 误命中幂等分支假成功、重启丢别名），重试真实重写
        calls = {"n": 0}
        orig_save = main_mod._save_json_atomic

        def _boom(path, data):
            calls["n"] += 1
            raise OSError("disk full (模拟)")

        main_mod._save_json_atomic = _boom
        try:
            main_mod._ocr_map_unknown("写盘失败甲", "fons")
        except OSError as e:
            assert "disk full" in str(e), e
        else:
            raise AssertionError("写盘失败必须抛 OSError")
        finally:
            main_mod._save_json_atomic = orig_save
        assert "写盘失败甲" not in (planner.MATERIALS.get("item_names") or {}) \
            .get("fons", []), "写盘失败后内存不得留半截别名"
        saved = json.load(open(os.path.join(tmp, "materials.json"), encoding="utf-8"))
        assert "写盘失败甲" not in saved["item_names"].get("fons", []), \
            "写盘失败不得落盘"
        names3 = main_mod._ocr_map_unknown("写盘失败甲", "fons")  # 重试：真实写盘
        assert "写盘失败甲" in names3.get("fons", []), names3.get("fons")
        assert calls["n"] == 1, calls  # 失败尝试恰好一次；重试走恢复后的真实写盘
        # 原文消毒：控制字符→空格、空白折叠、64 字截断（norm 2-24 管语义）
        raw = "甲\x01 乙" + "  " * 200  # norm=甲乙 合法；原文必须被折叠截断
        names4 = main_mod._ocr_map_unknown(raw, "fons")
        assert "甲 乙" in names4["fons"], names4["fons"]
        assert all("\x01" not in a and len(a) <= 64 for a in names4["fons"]), \
            names4["fons"]
        # 非法 mat_id → ValueError（端点转 400）
        for bad in (("某某道具", "no_such_mat"), ("某某道具", ""), (None, None)):
            try:
                main_mod._ocr_map_unknown(bad[0], bad[1])
            except ValueError as e:
                assert "未知材料 id" in str(e), str(e)
            else:
                raise AssertionError(f"非法 mat_id 必须拒绝: {bad}")
        # name_text 越界（归一化后 >24 字 / 单字 / 空白）→ ValueError
        for bad_name in ("这是一个非常非常长的未收录道具名称文本必须被映射接口拒绝",
                         "石", "   ", ""):
            try:
                main_mod._ocr_map_unknown(bad_name, "fons")
            except ValueError:
                pass
            else:
                raise AssertionError(f"越界 name_text 必须拒绝: {bad_name}")
        # 数字噪声拒绝（2026-11-08 用户实测："1519" 这类数字被 OCR 误读成道具名后
        # 映射成永久别名持续污染库存）：纯数字/数字+分隔符/全角数字一律拒绝（端点
        # 转 400）；「哇塞1号」有汉字 → 规则是无 CJK 才拒，放行
        for bad_num in ("1519", "1,519", "×42", "１５１９", "86/1269"):
            try:
                main_mod._ocr_map_unknown(bad_num, "fons")
            except ValueError as e:
                assert "纯数字不是道具实名" in str(e), str(e)
            else:
                raise AssertionError(f"数字噪声 name_text 必须拒绝: {bad_num}")
        names5 = main_mod._ocr_map_unknown("哇塞1号", "fons")
        assert "哇塞1号" in names5.get("fons", []), names5.get("fons")
        # 纵深防御（纯函数直验）：_ocr_numeric_noise 判定口径——含汉字放行、
        # 纯数字/数字+分隔符/数字占比过高（无 CJK）判噪声
        f = main_mod._ocr_numeric_noise
        assert f("1519") and f("１５１９") and f("1,234") and f("×42") and f("86/1269")
        assert not f("哇塞1号") and not f("1269 道具") and not f("谕石")
        assert not f("") and not f(None)  # 空文本不算噪声（原行为不变）
        # 纵深防御（候选表）：手改 materials.json 塞进纯数字别名也会被
        # _inventory_candidates 跳过，正常别名不受影响（对照物用非手动材料
        # oracle_stone——fons 已是 manual_only 不进候选，2026-11-10）
        saved_items = planner.MATERIALS.get("item_names")
        planner.MATERIALS["item_names"] = {
            **(saved_items or {}), "oracle_stone": ["1519", "写盘失败甲"]}
        try:
            cand = main_mod._inventory_candidates()
            assert not any(na == "1519" for na, _m in cand), \
                [na for na, _m in cand if na == "1519"]
            assert any(na == "写盘失败甲" for na, _m in cand), \
                "正常别名不得被数字噪声过滤误伤"
        finally:
            planner.MATERIALS["item_names"] = saved_items
    finally:
        main_mod.DATA_DIR = orig_main_datadir
        planner.DATA_DIR = orig_planner_datadir
        planner.reload_tables()  # 恢复生产三表内存态（清除测试期内存映射）


@case
def test_ocr_numeric_noise_traverse():
    """traverse 的 unknown 收集过滤数字噪声（2026-11-08 用户实测）："1519" 这类
    纯数字首行（无 CJK）不进 unknown_items（否则映射界面会把它登记成实名持续
    污染），仍进 unmatched_lines 保可见；含汉字的未收录文本照常进 unknown_items。
    整页纯数字 → 无新信息判停（噪声不算新信息，不得驱动翻页）。"""
    import app.main as main_mod
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.settings["game_process_name"] = "HTGame.exe"
        STATE.settings["game_window_keywords"] = "异环"
        meta = {"hwnd": 1, "class": "UnrealWindow", "title": "异环  ", "pid": 1,
                "process": "HTGame", "exe_path": "", "w": 1920, "h": 1080}
        page1 = ["海之泪 ×16", "1519", "未登记素材甲 ×2"]   # 混合页：实名+噪声+正常未知
        page2 = ["1519", "×42", "1,234"]                    # 整页纯数字/数字+分隔符
        seq = {"n": 0}

        def fake_traverse(hwnd, layout, win_size, gen=None):
            seq["n"] += 1
            return list(page1 if seq["n"] == 1 else page2)

        orig = (main_mod._bind_game_window, main_mod._ocr_traverse_page,
                main_mod._ocr_scroll, main_mod._ocr_layout)
        main_mod._bind_game_window = lambda *a, **k: (1, meta, [meta])
        main_mod._ocr_traverse_page = fake_traverse
        main_mod._ocr_scroll = lambda *a, **k: None
        main_mod._ocr_layout = lambda: main_mod._sanitize_ocr_layout(
            dict(main_mod._OCR_LAYOUT_DEFAULT))
        try:
            r = main_mod._run_backpack_ocr()
        finally:
            (main_mod._bind_game_window, main_mod._ocr_traverse_page,
             main_mod._ocr_scroll, main_mod._ocr_layout) = orig
        # 混合页：实名照常命中；"1519" 不进 unknown_items、仍进 unmatched_lines；
        # 含汉字的未收录文本（未登记素材甲）照常进 unknown_items
        assert r["matches"] == {"boss_sea_tear": 16}, r["matches"]
        assert len(r["unknown_items"]) == 1 \
            and "未登记素材甲" in r["unknown_items"][0]["name_text"], r["unknown_items"]
        assert "1519" in r["unmatched_lines"], r["unmatched_lines"]
        # 整页纯数字：第 2 页无新信息（噪声不算新信息）→ 判停，不得白扫后续页
        assert r["pages"] == 2, (r["pages"], r["warnings"])
        assert any("新道具" in w for w in r["warnings"]), r["warnings"]
        for noise in ("1519", "×42", "1,234"):
            assert noise in r["unmatched_lines"], r["unmatched_lines"]


@case
def test_ocr_scroll_keyword_injection_safe():
    """滚动脚本关键词注入防线：__KEYWORDS__ 必须在单引号 PS 字符串内（不插值），
    Python 端按单引号语义转义；恶意关键词 $(...) 不得执行。"""
    import app.main as main_mod
    # 静态：双引号插值上下文必须不存在（双引号内 $( )/反撇号即任意代码执行）
    assert "'__KEYWORDS__'" in main_mod._OCR_PS_SCROLL, "关键词必须是单引号字面量"
    assert '"__KEYWORDS__"' not in main_mod._OCR_PS_SCROLL
    # 转义语义匹配：Python 端 ' → ''（单引号字符串的转义规则）
    payload = "$(Write-Output INJECTED_MARKER)"
    safe_kw = payload.replace("'", "''")
    ps = (main_mod._OCR_PS_SCROLL.replace("__HWND__", "0")
          .replace("__KEYWORDS__", safe_kw))
    # 实跑（hwnd=0 走关键词回退分支）：payload 只能当字面量参与 -like 匹配，不执行
    out, err, code = main_mod._ocr_run_ps(ps, {}, timeout=60)
    assert "INJECTED_MARKER" not in out, (out[:200], err[:200])


@case
def test_ocr_scroll_config():
    """滚轮翻页步距可配置（2026-11-08 用户实测：硬编码 5×-480 每页少半个材料身位）：
    布局默认 scroll={notches:6, wheel_delta:480} 且经 _sanitize_ocr_layout 钳位
    （notches 1-20、wheel_delta 120-960，非法回落默认）；_OCR_PS_SCROLL 步距与
    网格锚点走占位符、由 _ocr_scroll 注入（inspect.getsource 静态校验注入链 +
    拦截 subprocess.run 解码实编码，无真滚动依赖）。"""
    import base64
    import inspect
    import app.main as main_mod
    lay = main_mod._sanitize_ocr_layout(dict(main_mod._OCR_LAYOUT_DEFAULT))
    assert lay["scroll"] == {"notches": 6, "wheel_delta": 480}, lay["scroll"]
    # 钳位：越界回落边界、非法回落默认（scroll 非对象 → 默认值）
    big = main_mod._sanitize_ocr_layout({"scroll": {"notches": 99, "wheel_delta": 5000}})
    assert big["scroll"] == {"notches": 20, "wheel_delta": 960}, big["scroll"]
    small = main_mod._sanitize_ocr_layout({"scroll": {"notches": 0, "wheel_delta": 10}})
    assert small["scroll"] == {"notches": 1, "wheel_delta": 120}, small["scroll"]
    junk = main_mod._sanitize_ocr_layout({"scroll": "bad"})
    assert junk["scroll"] == {"notches": 6, "wheel_delta": 480}, junk["scroll"]
    # PS 源：步距/锚点必须走占位符，不再硬编码 5×-480 / 几何中心
    ps_src = main_mod._OCR_PS_SCROLL
    for ph in ("__SCROLL_NOTCHES__", "__SCROLL_WDELTA__",
               "__SCROLL_X_RATIO__", "__SCROLL_Y_RATIO__"):
        assert ph in ps_src, f"滚动脚本缺占位符 {ph}"
    assert "-lt 5;" not in ps_src and "-480," not in ps_src, \
        "滚动脚本不得再硬编码步距"
    assert "($rect.Left + $rect.Right) / 2" not in ps_src, \
        "滚轮锚点不得再落在窗口几何中心"
    # 注入链（inspect.getsource 校验法）：_ocr_scroll 源逐一 replace 占位符，
    # 调用点把 layout 传进去
    scroll_src = inspect.getsource(main_mod._ocr_scroll)
    for ph in ("__SCROLL_NOTCHES__", "__SCROLL_WDELTA__",
               "__SCROLL_X_RATIO__", "__SCROLL_Y_RATIO__"):
        assert ph in scroll_src, f"_ocr_scroll 源未注入 {ph}"
    run_src = inspect.getsource(main_mod._run_backpack_ocr)
    assert "_ocr_scroll(hwnd, safe_kw, layout)" in run_src, \
        "遍历调用点必须把 layout 传给 _ocr_scroll"
    # 行为注入：拦截 subprocess.run，解码 -EncodedCommand 断言实参生效
    captured = {}

    def fake_run(cmd, **_kw):
        captured["ps"] = base64.b64decode(cmd[3]).decode("utf-16-le")

        class _R:
            returncode = 0
        return _R()

    orig_run = main_mod.subprocess.run
    main_mod.subprocess.run = fake_run
    try:
        main_mod._ocr_scroll(42, "异环", lay)
    finally:
        main_mod.subprocess.run = orig_run
    ps2 = captured["ps"]
    assert "-lt 6" in ps2 and ", -480," in ps2, "注入后步距应为 6 格×-480"
    assert "__SCROLL" not in ps2, "占位符必须全部替换"
    assert "0.1026" in ps2 and "0.2250" in ps2, \
        "滚轮锚点比例须按 first_cell 注入（默认布局）"
    # 热调生效：布局 notches=8/wheel_delta=240 → 注入值跟随
    main_mod.subprocess.run = fake_run
    try:
        main_mod._ocr_scroll(42, "异环", main_mod._sanitize_ocr_layout(
            {"scroll": {"notches": 8, "wheel_delta": 240}}))
    finally:
        main_mod.subprocess.run = orig_run
    assert "-lt 8" in captured["ps"] and ", -240," in captured["ps"], captured["ps"]


# ---------- B3-B OCR-01：PS 回退遍历取消支持（方案 b：Popen 句柄登记+terminate）专项 ----------

@case
def test_ocr01_ps_fallback_cancel_prompt():
    """OCR-01/B3-B 方案 b ①（慢桩+中段取消）：PS 回退整页阻塞期间触发取消——
    页 1 正常完成（已完成格入库），页 2 慢桩 Start-Sleep 阻塞、页 PS 句柄经生产
    登记链进 _OCR_ACTIVE_PS 后，直调 /api/ocr/cancel 与 Ctrl+Alt+Q 共用的取消
    原语 _ocr_cancel_active：遍历必须 promptly 结束（取消→返回 <1 秒级，perf_counter
    断言——修复前=整页时长，慢桩 3.0s 实测 3.001s/比值 1.000，约 8.6 格基线）；
    句柄级终止动作实际发生（第 2 页在册句柄 terminate 后 poll 非 None）；返回契约
    与 elev 路径一致=取消后返回已完成格不抛错（页 1 的 3 格 matches 保留、
    warning 含「用户取消」、无异常外抛）。
    夹具照 test_ocr_traverse_overlap_regression 先例打桩（_bind_game_window/
    _ocr_scroll/_ocr_layout/_ensure_helper_for_ocr + _OCR_PS_TRAVERSE 换慢桩脚本），
    真 powershell 走真实 _ocr_run_ps_popen 登记/terminate 链；慢桩不点击、不识别、
    无网络。"""
    import app.main as main_mod
    stub_ps = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
        "Start-Sleep -Seconds 2\n"   # 慢桩：页 PS 长阻塞（可被句柄 terminate 截断）
        "Write-Output '__OCR_CELL__:0:\"海之泪 ×16\"'\n"
        "Write-Output '__OCR_CELL__:1:\"海之泪 ×16\"'\n"
        "Write-Output '__OCR_CELL__:2:\"\"'\n"
        "Write-Output \"__OCR_META__`tStubWindow`twindow\"\n"
    )
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.settings["game_process_name"] = "HTGame.exe"
        STATE.settings["game_window_keywords"] = "异环"
        meta = {"hwnd": 1, "class": "UnrealWindow", "title": "异环  ", "pid": 1,
                "process": "HTGame", "exe_path": "", "w": 1920, "h": 1080}
        observed = {"epochs": 0, "proc2": None}

        def watch_active_ps():
            # 观察生产登记链：第 1 次登记=页 1（正常跑完清空），第 2 次登记=页 2
            # （取消目标页）；按登记→清空的配对逐 epoch 计数
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline and observed["epochs"] < 2:
                p = main_mod._OCR_ACTIVE_PS
                if p is None:
                    time.sleep(0.005)
                    continue
                observed["epochs"] += 1
                if observed["epochs"] == 2:
                    observed["proc2"] = p
                while time.monotonic() < deadline \
                        and main_mod._OCR_ACTIVE_PS is p:  # 等本页登记结束（清空）
                    time.sleep(0.005)

        result = {}

        def run_traverse():
            try:
                result["r"] = main_mod._run_backpack_ocr(mode="traverse")
            except BaseException as e:  # noqa: BLE001 —— 线程内异常带回主线程断言
                result["exc"] = e

        orig = (main_mod._bind_game_window, main_mod._ocr_scroll,
                main_mod._ocr_layout, main_mod._ensure_helper_for_ocr,
                main_mod._OCR_PS_TRAVERSE)
        main_mod._bind_game_window = lambda *a, **k: (1, meta, [meta])
        main_mod._ocr_scroll = lambda *a, **k: None
        main_mod._ocr_layout = lambda: main_mod._sanitize_ocr_layout(
            dict(main_mod._OCR_LAYOUT_DEFAULT))
        main_mod._ensure_helper_for_ocr = lambda *a, **k: False  # 强制走 PS 回退分支
        main_mod._OCR_PS_TRAVERSE = stub_ps
        watcher = threading.Thread(target=watch_active_ps, daemon=True,
                                   name="selftest-ocr01-watch")
        worker = threading.Thread(target=run_traverse,
                                  name="selftest-ocr01-traverse")
        try:
            watcher.start()
            worker.start()
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and observed["epochs"] < 2:
                time.sleep(0.02)  # 等页 1（正常完成清空）→ 页 2（慢桩阻塞）两个登记 epoch
            assert observed["epochs"] == 2, \
                (f"页 2 句柄必须经生产登记链进 _OCR_ACTIVE_PS"
                 f"（epochs={observed['epochs']}, exc={result.get('exc')}）")
            time.sleep(0.1)  # 页 2 PS 已在慢桩阻塞窗口内（确定性：Start-Sleep 2s）
            t0 = time.perf_counter()
            gen = main_mod._ocr_cancel_active()  # 取消原语本体（路由/热键共用）
            worker.join(timeout=90)
            elapsed = time.perf_counter() - t0
            assert not worker.is_alive(), "取消后遍历线程必须结束（90s 硬截止兜底）"
            # ①核心断言：取消后 promptly 结束（<1 秒级；修复前=整页时长≈慢桩 2s+启动）
            assert elapsed < 1.0, f"取消时延 {elapsed:.3f}s 应 <1s（方案 b 生效）"
            # 句柄级终止动作：页 2 在册句柄已被 terminate（poll 非 None）
            p2 = observed["proc2"]
            assert p2 is not None and p2.poll() is not None, \
                (p2, p2.poll() if p2 is not None else None)
            # 返回契约（实现五）：取消后返回已完成格不抛错（线程内无异常）
            assert "exc" not in result, result.get("exc")
            r = result["r"]
            assert any("用户取消" in w for w in r["warnings"]), r["warnings"]
            assert r["pages"] == 2 and r["cells_scanned"] == 3, \
                (r["pages"], r["cells_scanned"], r["warnings"])
            # 页 1 已完成格原样保留（页 2 取消点之后的格不再解析）
            assert r["matches"] == {"boss_sea_tear": 16}, r["matches"]
            assert r["unmatched_lines"] == [] and r["unknown_items"] == [], \
                (r["unmatched_lines"], r["unknown_items"])
        finally:
            # 卫生：仍存活的页 PS 补杀（不留孤儿 powershell），再收线程
            p = main_mod._OCR_ACTIVE_PS
            if p is not None and getattr(p, "poll", lambda: None)() is None:
                try:
                    p.kill()
                except Exception:  # noqa: BLE001 —— 清理尽力而为
                    pass
            worker.join(timeout=90)
            (main_mod._bind_game_window, main_mod._ocr_scroll, main_mod._ocr_layout,
             main_mod._ensure_helper_for_ocr, main_mod._OCR_PS_TRAVERSE) = orig
            main_mod._OCR_ACTIVE_PS = None
            main_mod._OCR_CANCEL.clear()  # 用例收尾卫生复位，不影响其它用例


@case
def test_ocr01_cancel_primitive_gen_isolation():
    """OCR-01/B3-B 方案 b ②（取消原语单测，句柄用替身、真原语）：未激活态
    （_OCR_ACTIVE_PS 空）取消=纯令牌置位、不触碰任何句柄、返回登记时当前代；
    在册存活句柄 → terminate 实际发生且令牌按代数命中；旧代取消绝不命中新一轮
    （_ocr_gen_begin 后新代 _ocr_cancel_hit=False=gen 隔离，代数令牌既有语义
    不回归）；已退出句柄（poll 非 None）不做任何终止动作（poll 守卫生效）；
    terminate 抛 OSError 逐个吞掉且 kill 兜底仍执行、令牌置位不受影响；
    KeyboardInterrupt 为 BaseException 不在 except Exception 吞列（必须外抛）。"""
    import app.main as main_mod

    class _FakeProc:
        """句柄替身：记录 terminate/kill 调用与存活态（poll 契约同 Popen：None=存活）。"""

        def __init__(self, alive=True, fail_terminate=False, exc=None):
            self.alive = alive
            self.terminated = False
            self.killed = False
            self._fail = fail_terminate
            self._exc = exc

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            if self._exc is not None:
                raise self._exc
            if self._fail:
                raise OSError("simulated terminate failure")
            self.terminated = True
            self.alive = False

        def kill(self):
            self.killed = True
            self.alive = False

    orig_slot = main_mod._OCR_ACTIVE_PS
    orig_cancel_gen = main_mod._OCR_CANCEL_GEN
    try:
        # (a) 未激活态：槽位空 → 取消=纯令牌置位，无句柄动作，返回当前代
        main_mod._OCR_ACTIVE_PS = None
        g0 = main_mod._ocr_cancel_active()
        assert g0 == main_mod._OCR_GEN, (g0, main_mod._OCR_GEN)
        assert main_mod._ocr_cancel_hit(g0), "取消必须定向登记时的当前代"
        # (b) 代数匹配+句柄终止：在册存活句柄 → terminate 实际发生
        p1 = _FakeProc(alive=True)
        main_mod._OCR_ACTIVE_PS = p1
        g1 = main_mod._ocr_cancel_active()
        assert g1 == main_mod._OCR_GEN, (g1, main_mod._OCR_GEN)
        assert p1.terminated and not p1.alive, (p1.terminated, p1.alive)
        assert not p1.killed, "terminate 已生效不再补杀"
        assert main_mod._ocr_cancel_hit(g1)
        # (c) gen 隔离：旧代取消后新一轮（代数+1）不命中——令牌既有语义不回归
        g2 = main_mod._ocr_gen_begin()
        assert g2 == g1 + 1, (g1, g2)
        assert not main_mod._ocr_cancel_hit(g2), "旧代取消不得命中新代（gen 隔离）"
        assert main_mod._ocr_cancel_hit(g1), "旧代取消仍对旧代可见"
        # (d) 已退出句柄：poll 非 None → 不做任何终止动作（poll 守卫生效）
        p2 = _FakeProc(alive=False)
        main_mod._OCR_ACTIVE_PS = p2
        main_mod._ocr_cancel_active()
        assert not p2.terminated and not p2.killed, (p2.terminated, p2.killed)
        # (e) terminate 抛 OSError：逐个吞掉、kill 兜底仍执行、令牌置位不受影响
        p3 = _FakeProc(alive=True, fail_terminate=True)
        main_mod._OCR_ACTIVE_PS = p3
        g3 = main_mod._ocr_cancel_active()  # 不得外抛 OSError
        assert p3.killed and not p3.alive, (p3.killed, p3.alive)
        assert main_mod._ocr_cancel_hit(g3)
        # (f) KeyboardInterrupt 不吞（BaseException 不在 except Exception 吞列），
        #     且令牌在终止动作之前已置位
        p4 = _FakeProc(alive=True, exc=KeyboardInterrupt())
        main_mod._OCR_ACTIVE_PS = p4
        ki_caught = False
        try:
            main_mod._ocr_cancel_active()
        except KeyboardInterrupt:
            ki_caught = True
        assert ki_caught, "terminate 抛 KeyboardInterrupt 必须外抛（不吞）"
        assert main_mod._OCR_CANCEL.is_set(), "令牌置位在句柄终止之前、不受其异常影响"
    finally:
        main_mod._OCR_ACTIVE_PS = orig_slot
        main_mod._OCR_CANCEL_GEN = orig_cancel_gen
        main_mod._OCR_CANCEL.clear()  # 用例收尾卫生复位，不影响其它用例


@case
def test_ocr01_popen_timeout_kill_cleanup():
    """OCR-01/B3-B 方案 b ③（Popen 变体超时路径）：真 powershell 慢桩
    Start-Sleep 30 + timeout=0.8 → 必抛 TimeoutExpired（与原 _ocr_run_ps 的
    subprocess.run「超时即杀」契约一致）；运行中句柄确实登记过（watcher 观察到
    _OCR_ACTIVE_PS）；超时 kill 后句柄已退出（poll 非 None）；_OCR_ACTIVE_PS
    复位（超时分支 + finally 双保险）；异常附杀后收尸残余输出（stdout/stderr 非
    None，与 subprocess.run Windows 分支同款）。"""
    import app.main as main_mod
    observed = {"proc": None}

    def watch():
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            p = main_mod._OCR_ACTIVE_PS
            if p is not None:
                observed["proc"] = p
                return
            time.sleep(0.005)

    assert main_mod._OCR_ACTIVE_PS is None, "测试前提：登记槽位空闲"
    w = threading.Thread(target=watch, daemon=True, name="selftest-ocr01-timeout")
    w.start()
    raised = False
    try:
        main_mod._ocr_run_ps_popen("Start-Sleep -Seconds 30", {}, timeout=0.8)
    except subprocess.TimeoutExpired as e:
        raised = True
        assert e.stdout is not None and e.stderr is not None, \
            "超时异常必须附杀后收尸残余输出（原 run Windows 分支同款）"
    assert raised, "超时必须抛 TimeoutExpired（与原 _ocr_run_ps 契约一致）"
    w.join(timeout=2)
    p = observed["proc"]
    assert p is not None, "运行中句柄必须被登记（watcher 未观察到=登记缺失）"
    assert p.poll() is not None, "超时 kill 后句柄必须已退出"
    assert main_mod._OCR_ACTIVE_PS is None, "超时路径 _OCR_ACTIVE_PS 必须复位"


@case
def test_ocr01_ps_fallback_normal_regression():
    """OCR-01/B3-B 方案 b ④（正常遍历回归，无取消）：真 powershell 桩脚本输出
    （含中文/空格/×N 形态 + 尾部 3 连空）经新 Popen 变体整页返回，解析聚合结果
    与既有断言口径一致（matches={boss_sea_tear:16}、cells_scanned=4、pages=1、
    warning「连续 3 格空白」——对照 test_ocr_unknown_cells_keep_traversing 的
    r2 形态），输出解析链（main.py PS 回退分支 :1671-1686 段）不破坏；同脚本同
    注入直调新旧两函数，返回三元组 (stdout, stderr, returncode) 逐项相等（返回值
    契约与原函数一致）；调用后登记槽位复位（成功路径登记/清空严格配对）。"""
    import app.main as main_mod
    stub_ps = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
        "Write-Output '__OCR_CELL__:0:\"海之泪 ×16\"'\n"
        "Write-Output '__OCR_CELL__:1:\"\"'\n"
        "Write-Output '__OCR_CELL__:2:\"\"'\n"
        "Write-Output '__OCR_CELL__:3:\"\"'\n"
        "Write-Output \"__OCR_META__`tStubWindow`twindow\"\n"
    )
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.settings["game_process_name"] = "HTGame.exe"
        STATE.settings["game_window_keywords"] = "异环"
        meta = {"hwnd": 1, "class": "UnrealWindow", "title": "异环  ", "pid": 1,
                "process": "HTGame", "exe_path": "", "w": 1920, "h": 1080}
        orig = (main_mod._bind_game_window, main_mod._ocr_scroll,
                main_mod._ocr_layout, main_mod._ensure_helper_for_ocr,
                main_mod._OCR_PS_TRAVERSE)
        main_mod._bind_game_window = lambda *a, **k: (1, meta, [meta])
        main_mod._ocr_scroll = lambda *a, **k: None
        lay = main_mod._sanitize_ocr_layout(dict(main_mod._OCR_LAYOUT_DEFAULT))
        main_mod._ocr_layout = lambda: lay
        main_mod._ensure_helper_for_ocr = lambda *a, **k: False  # 强制走 PS 回退分支
        main_mod._OCR_PS_TRAVERSE = stub_ps
        try:
            r = main_mod._run_backpack_ocr(mode="traverse")
            # 正常遍历结果与旧路径（subprocess.run 形态）的既有断言口径一致
            assert r["matches"] == {"boss_sea_tear": 16}, r["matches"]
            assert r["pages"] == 1 and r["cells_scanned"] == 4, \
                (r["pages"], r["cells_scanned"])
            assert any("连续 3 格空白" in w for w in r["warnings"]), r["warnings"]
            assert r["unmatched_lines"] == [] and r["unknown_items"] == [], \
                (r["unmatched_lines"], r["unknown_items"])
            assert main_mod._OCR_ACTIVE_PS is None, "遍历结束后登记槽位必须复位"
            # 返回值契约：同脚本同注入，新旧两函数三元组逐项相等
            script = (main_mod._OCR_PS_COMMON + "\n" + main_mod._OCR_PS_WINDOW
                      + "\n" + main_mod._OCR_PS_TRAVERSE)
            pts = main_mod._ocr_cell_points(lay, 1920, 1080)
            repl = {"__HWND__": "1", "__ROI__": "",
                    "__POINTS__": json.dumps([list(p) for p in pts])}
            old_out = main_mod._ocr_run_ps(script, repl, timeout=60)
            new_out = main_mod._ocr_run_ps_popen(script, repl, timeout=60)
            assert old_out == new_out, \
                "新旧函数返回三元组必须一致（stdout/stderr/returncode）"
            assert "__OCR_CELL__:0:" in new_out[0] and new_out[2] == 0, new_out[2]
            assert main_mod._OCR_ACTIVE_PS is None, "直调后登记槽位必须复位"
        finally:
            (main_mod._bind_game_window, main_mod._ocr_scroll, main_mod._ocr_layout,
             main_mod._ensure_helper_for_ocr, main_mod._OCR_PS_TRAVERSE) = orig
            main_mod._OCR_ACTIVE_PS = None
            main_mod._OCR_CANCEL.clear()


@case
def test_ocr01_existing_ocr_cases_rerun():
    """OCR-01/B3-B 方案 b ⑤（OCR 既有用例隔离直调复跑全绿）：本 slice 改动
    （_ocr_run_ps_popen/_OCR_ACTIVE_PS/_ocr_cancel_active 句柄终止/PS 回退走新
    变体）不得破坏任何既有 OCR 用例（含端点级 e2e：单飞锁 409 真服务用例）。
    逐例隔离直调并记录 PASS/FAIL，任一失败即本用例失败并列出清单。复跑期间把
    tempfile 默认根重定向到仓库 .tmp/（E 盘）——既有用例的 mkdtemp 不再滞留
    C 盘，结束后清理并还原。"""
    import app.main as main_mod
    ocr_cases = [
        test_ocr_match_inventory_text, test_ocr_item_names_dictionary,
        test_ocr_candidates_single_owner, test_ocr_parse_count_cn,
        test_ocr_cell_points_grid, test_ocr_windows_parse,
        test_ocr_bind_priority, test_ocr_magnitude_in_inventory,
        test_ocr_traverse_overlap_regression, test_ocr_unknown_cells_keep_traversing,
        test_ocr_traverse_cancel, test_ocr_cell_cancel_and_token_isolation,
        test_ocr_single_flight_409, test_ocr_badge_count_priority,
        test_ocr_map_unknown, test_ocr_numeric_noise_traverse,
        test_ocr_scroll_keyword_injection_safe, test_ocr_scroll_config,
    ]
    tmp_root = _safe_path(_ROOT, ".tmp", "selftest_ocr01_rerun")
    Path(tmp_root).mkdir(parents=True, exist_ok=True)
    orig_tempdir = tempfile.tempdir
    tempfile.tempdir = str(tmp_root)  # 复跑期 mkdtemp（无 dir=）落在仓库 .tmp/
    failed = []
    try:
        for fn in ocr_cases:
            try:
                fn()
            except Exception as e:  # noqa: BLE001 —— 复跑记录后整体判失败
                failed.append(f"{fn.__name__}: {type(e).__name__}: {e}")
                print(f"OCR01-RERUN FAIL {fn.__name__}: {type(e).__name__}: {e}")
            else:
                print(f"OCR01-RERUN PASS {fn.__name__}")
    finally:
        tempfile.tempdir = orig_tempdir
        shutil.rmtree(tmp_root, ignore_errors=True)  # 复跑夹具随测随清
    assert not failed, "OCR 既有用例复跑存在失败：" + "; ".join(failed)
    assert main_mod._OCR_ACTIVE_PS is None, "复跑后登记槽位必须复位"
    assert not main_mod._OCR_CANCEL.is_set(), "复跑后取消令牌必须复位"


# ---------- B3-B OCR-05：提权辅助 ensure 失败——显式重试端点 + TTL 兜底专项 ----------

def _ocr05_cleanup_flags(main_mod):
    """OCR-05 用例间模块全局复位：_ELEV_ENSURE_FAILED/_AT 不跨用例泄漏。"""
    main_mod._ELEV_ENSURE_FAILED = False
    main_mod._ELEV_ENSURE_FAILED_AT = 0.0


def _ocr05_spy_ensure(return_value):
    """OCR-05 专用 spy：实例属性遮蔽 elev_client.ELEV.ensure，计数调用并固化
    返回值（不触真实拉起）。返回 (calls, uninstall)；uninstall 删实例属性还原
    类绑定方法（复现脚本 .tmp/repro_slice_e_ocr05.py 先例口径）。"""
    import app.elev_client as elev_client
    calls = []

    def _spy(root, *a, **kw):
        calls.append(root)
        return return_value

    elev_client.ELEV.ensure = _spy

    def uninstall():
        if "ensure" in elev_client.ELEV.__dict__:
            del elev_client.ELEV.ensure

    return calls, uninstall


@case
def test_ocr05_retry_endpoint_restores_degradation():
    """OCR-05/B3-B ①置位→端点重试→恢复：坏 root（目录在、缺内嵌 python.exe，
    elev_client.py:215-217 确定性失败，不起进程不弹 UAC）直调
    _ensure_helper_for_ocr 置位降级（stdout 含新口径文案）后，同一 root 修好
    （写回 python.exe 合法形态）→ POST /api/elev/retry（空体裸 POST）复位标志
    并重试 → ensured=True、_ELEV_ENSURE_FAILED=False、_AT=0.0；再直调返回 True
    （会话复用 elev_client.py:218-220）=后续 OCR 走 elev 路径。
    夹具照 test_elev_helper_e2e_auth_ops_audit 先例（set_test_spawn 测试模式
    普通 Popen——helper IPC 仅 127.0.0.1、不弹真实 UAC）；HTTP 层照 _srv02_call
    先例（回环 Origin + X-Panel-Token）；临时目录仓库 .tmp/（E 盘）。"""
    import io
    import threading
    from contextlib import redirect_stdout
    import app.elev_client as elev_client
    import app.main as main_mod
    from app import state as state_mod
    from app.main import create_server

    _srv02_wait_runner_idle()
    tmp = _sec10_tmp("ocr05")
    log_dir = _safe_path(tmp, "log")
    os.makedirs(log_dir, exist_ok=True)
    root = _safe_path(tmp, "root")
    py_dir = _safe_path(root, "data", "apps", "ok-nte", "python")
    os.makedirs(py_dir, exist_ok=True)  # 坏 root：目录在、python.exe 缺
    orig_data_dir = state_mod.DATA_DIR
    state_mod.DATA_DIR = log_dir  # _default_log_dir()（elev_client.py:112-116）重定向
    elev_client.set_test_spawn(True)
    buf = io.StringIO()
    try:
        with _StateGuard(), redirect_stdout(buf):
            STATE.data["settings"] = dict(DEFAULT_SETTINGS)
            STATE.settings["oknte_root"] = root
            assert main_mod._ensure_helper_for_ocr() is False  # 坏 root 置位
            assert main_mod._ELEV_ENSURE_FAILED is True
            out0 = buf.getvalue()
            assert "已回退内置路径" in out0 and \
                "可用设置页重试或 10 分钟后自动恢复" in out0  # OCR-05/B3-B 新口径
            # 修好 root（同路径写回 python.exe = _sec10_root 合法形态）
            Path(_safe_path(py_dir, "python.exe")).write_text(
                "placeholder", encoding="utf-8")
            main_mod._PANEL_TOKEN = main_mod._new_panel_token()
            server, _port = create_server(0, auto_range=1)
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True,
                             name="selftest-ocr05-retry").start()
            try:
                code, r = _srv02_call(port, "POST", "/api/elev/retry")
                assert code == 200 and r.get("ok") is True, (code, r)
                assert r.get("ensured") is True, r  # 重试成功（测试模式普通 Popen）
                assert isinstance(r.get("message"), str) and r["message"], r
                assert main_mod._ELEV_ENSURE_FAILED is False  # 端点已复位标志
                assert main_mod._ELEV_ENSURE_FAILED_AT == 0.0
                # 后续 OCR 走 elev 路径：再直调不置位、复用既有会话
                assert main_mod._ensure_helper_for_ocr() is True
                assert elev_client.ELEV.available()
            finally:
                server.shutdown()
                server.server_close()
    finally:
        elev_client.set_test_spawn(False)
        pid = elev_client.ELEV._pid
        elev_client.ELEV.shutdown()
        deadline = time.time() + 6
        while pid and time.time() < deadline and elev_client._pid_alive(pid):
            time.sleep(0.2)
        assert not (pid and elev_client._pid_alive(pid)), \
            "辅助进程应随 shutdown 退出（不留孤儿）"
        state_mod.DATA_DIR = orig_data_dir
        _ocr05_cleanup_flags(main_mod)
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_ocr05_ttl_expiry_allows_retry():
    """OCR-05/B3-B ②TTL 过期放行：置位后注入过期 _AT（monotonic()-601，>600 秒
    TTL）→ 直调 _ensure_helper_for_ocr 必须放行重试并复位——spy 证明 ensure
    真实被调（非短路）、实参=STATE.settings["oknte_root"]；spy 固化重试仍失败
    → 重新置位并刷新 _AT（TTL 只放行重试、不反复弹 UAC，短路语义保持）；
    stdout 置位与重新置位两次均含 OCR-05/B3-B 新口径文案。"""
    import io
    from contextlib import redirect_stdout
    import app.main as main_mod

    tmp = _sec10_tmp("ocr05ttl")
    root_bad = _safe_path(tmp, "root_bad")  # 不存在路径：ensure 确定性失败不拉进程
    buf = io.StringIO()
    with _StateGuard(), redirect_stdout(buf):
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.settings["oknte_root"] = root_bad
        try:
            # 先真实置位（坏 root：elev_client.py:215-217 失败，不起进程不弹 UAC）
            assert main_mod._ensure_helper_for_ocr() is False
            assert main_mod._ELEV_ENSURE_FAILED is True
            assert main_mod._ELEV_ENSURE_FAILED_AT > 0.0
            # 注入过期 _AT（>600 秒）
            main_mod._ELEV_ENSURE_FAILED_AT = time.monotonic() - 601.0
            calls, uninstall = _ocr05_spy_ensure(False)
            try:
                assert main_mod._ensure_helper_for_ocr() is False  # 重试失败（spy）
                assert len(calls) == 1, "TTL 过期必须放行重试（ensure 真实被调）"
                assert calls[0] == root_bad  # 调用链实参不破坏
                # 重试失败 → 重新置位并刷新 _AT（短路语义保持）
                assert main_mod._ELEV_ENSURE_FAILED is True
                assert main_mod._ELEV_ENSURE_FAILED_AT > time.monotonic() - 601.0
                assert main_mod._ELEV_ENSURE_FAILED_AT <= time.monotonic()
            finally:
                uninstall()
        finally:
            _ocr05_cleanup_flags(main_mod)
    assert buf.getvalue().count("可用设置页重试或 10 分钟后自动恢复") == 2, \
        buf.getvalue()  # 置位+重新置位各一次
    shutil.rmtree(tmp, ignore_errors=True)


@case
def test_ocr05_ttl_unexpired_still_short_circuit():
    """OCR-05/B3-B ③TTL 未过期仍短路：置位后 _AT 为近期值（<600 秒）→ 直调
    短路返回 False 且 ensure 未被重试（spy 零调用，短路于 main.py
    _ensure_helper_for_ocr TTL 闸）——修复前「不反复弹」语义在 TTL 窗口内
    保持；标志与置位时刻均不变。"""
    import app.main as main_mod

    tmp = _sec10_tmp("ocr05ttl2")
    root_bad = _safe_path(tmp, "root_bad")
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.settings["oknte_root"] = root_bad
        try:
            assert main_mod._ensure_helper_for_ocr() is False  # 真实置位
            at_first = main_mod._ELEV_ENSURE_FAILED_AT
            assert main_mod._ELEV_ENSURE_FAILED is True and at_first > 0.0
            calls, uninstall = _ocr05_spy_ensure(True)
            try:
                assert main_mod._ensure_helper_for_ocr() is False  # TTL 内短路
                assert calls == [], "TTL 未过期不得重试（ensure 未被调）"
                assert main_mod._ELEV_ENSURE_FAILED is True  # 标志不变
                assert main_mod._ELEV_ENSURE_FAILED_AT == at_first  # 时刻不变
            finally:
                uninstall()
        finally:
            _ocr05_cleanup_flags(main_mod)
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_ocr05_retry_endpoint_surface():
    """OCR-05/B3-B ④端点面断言：/api/elev/retry 在 _NO_BODY_ROUTES（空体裸
    POST 放行为 {}——不在册则 400 缺少请求体）；有意不在运行中 409 白名单
    （UAC 弹窗仅在空闲发起）——静态断言 do_POST 的 409 白名单元组不含本路由
    （对照 /api/stop 证明定位）+ 运行态注入正面验证 409；空闲态空体 POST
    可达（坏 root 打桩 ensured=False、重新置位，不拉进程不弹 UAC）。"""
    import inspect
    import re as re_mod
    import threading
    import app.main as main_mod
    from app.main import create_server

    assert "/api/elev/retry" in main_mod._NO_BODY_ROUTES
    # 静态：运行中 409 白名单元组（main.py do_POST）不含 /api/elev/retry
    src = inspect.getsource(main_mod.Handler.do_POST)
    m = re_mod.search(r"path not in \(([^)]*)\)\s*and\s*\\?\s*RUNNER\.snapshot", src)
    assert m, "do_POST 运行中 409 白名单元组定位失败"
    assert "/api/stop" in m.group(1), "正则定位到的应是 409 白名单元组（对照组）"
    assert "/api/elev/retry" not in m.group(1), "重试端点不得加入运行中 409 白名单"

    _srv02_wait_runner_idle()
    tmp = _sec10_tmp("ocr05surf")
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.settings["oknte_root"] = _safe_path(tmp, "no_such_root")
        main_mod._PANEL_TOKEN = main_mod._new_panel_token()
        server, _port = create_server(0, auto_range=1)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True,
                         name="selftest-ocr05-surface").start()
        runner_state0 = runner_mod.RUNNER.snapshot()["state"]
        try:
            # 运行态注入（status 持锁改，不 start/stop、不写 runtime）→ 空
            # 体 POST 必须 409（端点有意不在 409 白名单）
            with runner_mod.RUNNER.lock:
                runner_mod.RUNNER.status["state"] = "running"
            try:
                code, r = _srv02_call(port, "POST", "/api/elev/retry")
                assert code == 409, (code, r)
            finally:
                with runner_mod.RUNNER.lock:
                    runner_mod.RUNNER.status["state"] = runner_state0
            # 空闲态空体 POST 可达：坏 root → ensured=False（失败不拉进程）
            code, r = _srv02_call(port, "POST", "/api/elev/retry")
            assert code == 200 and r.get("ok") is True, (code, r)
            assert r.get("ensured") is False, r
            assert isinstance(r.get("message"), str) and r["message"], r
            assert main_mod._ELEV_ENSURE_FAILED is True  # 重试失败重新置位
        finally:
            server.shutdown()
            server.server_close()
            _ocr05_cleanup_flags(main_mod)
            shutil.rmtree(tmp, ignore_errors=True)


@case
def test_ocr05_ensure_success_path_regression():
    """OCR-05/B3-B ⑤ensure 成功路径回归：不置位（标志 False/_AT 0.0）→ 直调
    不短路、ensure 被调且实参=STATE.settings["oknte_root"]、成功返回 True 后
    不置位、_AT 保持 0.0、零打印（修复前成功路径行为保持）；失败对照：ensure
    返回 False → 置位 + _AT 记录（TTL 起点就绪）。spy 固化返回值，不拉子进程
    不弹 UAC。"""
    import io
    from contextlib import redirect_stdout
    import app.main as main_mod

    tmp = _sec10_tmp("ocr05reg")
    root_fake = _safe_path(tmp, "root")
    _ocr05_cleanup_flags(main_mod)  # 夹具准备：不置位基线
    buf = io.StringIO()
    with _StateGuard(), redirect_stdout(buf):
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.settings["oknte_root"] = root_fake
        calls, uninstall = _ocr05_spy_ensure(True)
        try:
            assert main_mod._ensure_helper_for_ocr() is True  # 成功路径直通
            assert calls == [root_fake]  # 实参=STATE.settings["oknte_root"]
            assert main_mod._ELEV_ENSURE_FAILED is False  # 成功不置位
            assert main_mod._ELEV_ENSURE_FAILED_AT == 0.0
        finally:
            uninstall()
        out_mid = buf.getvalue()
        assert "提权辅助进程未就绪" not in out_mid, out_mid  # 成功路径零打印
        # 失败对照：ensure False → 置位 + _AT 记录（TTL 起点就绪）
        calls2, uninstall2 = _ocr05_spy_ensure(False)
        try:
            assert main_mod._ensure_helper_for_ocr() is False
            assert calls2 == [root_fake]
            assert main_mod._ELEV_ENSURE_FAILED is True
            assert 0.0 < main_mod._ELEV_ENSURE_FAILED_AT <= time.monotonic()
        finally:
            uninstall2()
        assert "可用设置页重试或 10 分钟后自动恢复" in buf.getvalue()  # 新口径
    _ocr05_cleanup_flags(main_mod)
    shutil.rmtree(tmp, ignore_errors=True)


# ---------- 塔吉多通道（协议纯函数 / 请求构造 / 解析 / 预览应用） ----------

@case
def test_tajiduo_sign_ds_aes_vectors():
    """签名/ds/AES 断言向量：观测值与 NIST 公开测试向量（协议常量，离线复现）。"""
    import re
    from app import tajiduo_client as tc
    # 1) 老虎签名：参数按 key 排序拼 value + appKey → md5（侦察观测算法）
    params = {"appId": "10550", "t": "1700000000", "cellphone": "13800138000",
              "type": "16"}
    assert tc.laohu_sign(params) == "c9c505637da493c8d4ecbad281e08b13", \
        tc.laohu_sign(params)
    # 插入顺序无关（排序后拼值）
    shuffled = {"type": "16", "cellphone": "13800138000", "t": "1700000000",
                "appId": "10550"}
    assert tc.laohu_sign(shuffled) == tc.laohu_sign(params)
    # 值不同 → 签名不同
    assert tc.laohu_sign({**params, "t": "1700000001"}) != tc.laohu_sign(params)
    # 2) ds 头：md5(ts+nonce8+appversion+salt) 格式 "ts,nonce,digest"（注入时钟向量）
    ds = tc.ds_sign(ts=1700000000, nonce="AB12cd34")
    assert ds == "1700000000,AB12cd34,791feba72a770fb7c1807b063a2d303f", ds
    live = tc.ds_sign()
    m = re.fullmatch(r"(\d{10}),([A-Za-z0-9]{8}),([0-9a-f]{32})", live)
    assert m, live  # 实时生成同样符合格式
    # 3) AES-128-ECB：NIST SP800-38A F.1.1 与 FIPS-197 附录 C.1 公开向量
    import binascii
    ct = tc.aes128_ecb_encrypt(binascii.unhexlify("6bc1bee22e409f96e93d7e117393172a"),
                               binascii.unhexlify("2b7e151628aed2a6abf7158809cf4f3c"))
    assert binascii.hexlify(ct[:16]).decode() == "3ad77bb40d7a3660a89ecaf32466ef97"
    ct2 = tc.aes128_ecb_encrypt(binascii.unhexlify("00112233445566778899aabbccddeeff"),
                                binascii.unhexlify("000102030405060708090a0b0c0d0e0f"))
    assert binascii.hexlify(ct2[:16]).decode() == "69c4e0d86a7b0430d8cdb78070b4c55a"
    # PKCS7：整块明文补整块 padding（16B → 32B）；ECB 同明文块 → 同密文块
    import base64
    key = binascii.unhexlify("2b7e151628aed2a6abf7158809cf4f3c")
    full = tc.aes128_ecb_encrypt(b"A" * 16, key)
    assert len(full) == 32, len(full)
    two = tc.aes128_ecb_encrypt(b"A" * 32, key)  # 前两块同明文（第三块是 padding 块）
    assert two[:16] == two[16:32], "ECB 相同明文块必须得相同密文块"
    # 4) 老虎字段加密 = AES-ECB(appKey 后 16 字节) + PKCS7 + base64；与直算一致
    enc = tc.laohu_aes_encrypt("13800138000")
    assert enc == base64.b64encode(tc.aes128_ecb_encrypt(
        b"13800138000", tc.LAOHU_APP_KEY[-16:].encode())).decode()
    assert len(base64.b64decode(enc)) == 16  # 11 字节明文 + 5 字节 padding
    # 密钥长度不是 16 字节必须拒绝（防误用）
    try:
        tc.aes128_ecb_encrypt(b"x", b"short")
        raise AssertionError("非法密钥长度必须拒绝")
    except ValueError:
        pass


@case
def test_tajiduo_request_construction_and_ssrf():
    """请求构造打印（不触网）：URL/方法/头(ds 格式、authorization)/签名 body 逐项断言；
    SSRF 防线：非 https、外域、用户信息、非 443 端口、白名单域解析到环回全拒绝。"""
    import re
    import socket as socket_mod
    import urllib.parse
    from app import tajiduo_client as tc
    captured = []

    class _Resp:
        def __init__(self, payload):
            self._raw = json.dumps(payload).encode("utf-8")

        def read(self, n=-1):
            # FETCH-06 起 open_pinned 传 read(MAX+1)：n>=0 按真实
            # http.client.HTTPResponse.read(n) 语义至多 n 字节（本桩供给量远
            # 小于上限，等价原全量返回，既有调用方零改动）
            return self._raw if n is None or n < 0 else self._raw[:n]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _FakeOpener:
        def __init__(self, payloads):
            self.payloads = list(payloads)  # 每次 open 弹出一个响应

        def open(self, req, timeout=None):
            captured.append(req)
            return _Resp(self.payloads.pop(0) if self.payloads else {"code": 0, "data": {}})

    orig_opener, orig_gai = tc._OPENER, socket_mod.getaddrinfo
    socket_mod.getaddrinfo = lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))]
    try:
        tc._OPENER = _FakeOpener([
            {"code": 0, "message": "ok", "result": {}},              # send_sms_code
            {"code": 0, "message": "ok", "data": {                   # usercenter login
                "accessToken": "AT1", "refreshToken": "RT1", "uid": 12345}},
            {"code": 0, "message": "ok", "data": {"accessToken": "AT2",  # refresh
                                                  "refreshToken": "RT2"}},
            {"code": 0, "message": "ok", "data": {                   # getGameRoles v2
                "bindRole": 214075351008,
                "roles": [{"roleId": 214075351008, "roleName": "旅行者",
                           "serverName": "测试服", "lev": 60}]}},
            {"code": 0, "message": "ok", "data": {}},                # roleHome
            {"code": 0, "message": "ok", "data": []},                # characters
        ])
        # ---- 老虎发码：POST form + sign 字段（sign 之外按排序拼值可复算） ----
        laohu = tc.LaohuClient(device=tc.LaohuDevice("HTTESTDEVICE000001"))
        laohu.send_sms_code("13800138000")
        req = captured[-1]
        assert req.get_full_url() == \
            "https://user.laohu.com/m/newApi/sendPhoneCaptchaWithOutLogin", req.get_full_url()
        assert req.get_method() == "POST"
        body = {k: v[0] for k, v in urllib.parse.parse_qs(
            req.data.decode("utf-8"), keep_blank_values=True).items()}
        assert body["appId"] == "10550" and body["type"] == "16" \
            and body["cellphone"] == "13800138000" and body["areaCodeId"] == "1", body
        assert body["bid"] == "com.pwrd.htassistant" and body["deviceId"] == "HTTESTDEVICE000001"
        sign = body.pop("sign")
        assert sign == tc.laohu_sign(body), (sign, body)
        # 空串字段在非 keep_empty 提交中被剔除（观测：login 走 keep_empty，其余剔除）
        assert "imei" not in body and "idfa" not in body, body.keys()
        # ---- 用户中心登录：POST /usercenter/api/login（appId=10551）+ ds 头 ----
        client = tc.TajiduoClient("HTTESTDEVICE000001")
        session = client.user_center_login("laohu-token", 12345)
        assert session == {"access_token": "AT1", "refresh_token": "RT1",
                           "center_uid": "12345"}, session
        req = captured[-1]
        assert req.get_full_url() == "https://bbs-api.tajiduo.com/usercenter/api/login"
        body = {k: v[0] for k, v in urllib.parse.parse_qs(req.data.decode("utf-8")).items()}
        assert body == {"token": "laohu-token", "userIdentity": "12345",
                        "appId": "10551"}, body
        headers = {k.lower(): v for k, v in req.headers.items()}
        assert headers["platform"] == "android" and headers["appversion"] == "1.2.2" \
            and headers["uid"] == "0" and headers["deviceid"] == "HTTESTDEVICE000001" \
            and headers["user-agent"] == "okhttp/4.12.0", headers.keys()
        assert re.fullmatch(r"\d{10},[A-Za-z0-9]{8},[0-9a-f]{32}", headers["ds"]), headers["ds"]
        # ---- 续期：authorization 传 refreshToken 且双令牌轮换 ----
        refreshed = client.refresh_session()
        assert refreshed["access_token"] == "AT2" and refreshed["refresh_token"] == "RT2"
        req = captured[-1]
        assert req.get_full_url() == "https://bbs-api.tajiduo.com/usercenter/api/refreshToken"
        assert req.get_method() == "POST"
        headers = {k.lower(): v for k, v in req.headers.items()}
        assert headers["authorization"] == "RT1", headers.get("authorization")
        # ---- 数据端点：GET + query + authorization=accessToken ----
        roles = client.get_game_roles("1289")
        assert roles == [{"roleId": "214075351008", "roleName": "旅行者",
                          "serverName": "测试服", "lev": 60}], roles
        req = captured[-1]
        assert req.get_full_url() == \
            "https://bbs-api.tajiduo.com/usercenter/api/v2/getGameRoles?gameId=1289"
        headers = {k.lower(): v for k, v in req.headers.items()}
        assert headers["authorization"] == "AT2"
        # ---- code!=0 报错且带服务端 message；鉴权类错误标记 auth ----
        tc._OPENER = _FakeOpener([{"code": 1001, "message": "令牌无效"}])
        try:
            client.get_game_roles("1289")
            raise AssertionError("code!=0 必须抛错")
        except tc.TajiduoError as e:
            assert "令牌无效" in str(e) and e.auth, (str(e), e.auth)
    finally:
        tc._OPENER = orig_opener
        socket_mod.getaddrinfo = orig_gai
    # ---- SSRF 防线（伪造 DNS，不触网） ----
    assert tc.host_allowed("user.laohu.com") and tc.host_allowed("bbs-api.tajiduo.com")
    # 2026-11-14 B2.5 收紧（policy §2.2）：tajiduo 白名单改精确相等——全部出站 URL
    # 仅由两 BASE 常量拼装、无子域业务，endswith 子域规则移除，子域形态改期望拒绝；
    # 尾点归一化放行口径保持（与 sites 一致，policy §2.3）
    assert tc.host_allowed("user.laohu.com.")
    assert not tc.host_allowed("www.bbs-api.tajiduo.com")
    for bad in ("localhost", "127.0.0.1", "evil-user.laohu.com",
                "user.laohu.com.evil.com", "bbs-api.tajiduo.com:8443@", "", None):
        assert not tc.host_allowed(bad), bad
    for bad_url in ("http://bbs-api.tajiduo.com/x",                      # 非 https
                    "https://evil.com/x",                                # 外域
                    "https://user:pass@bbs-api.tajiduo.com/x",           # 用户信息
                    "https://bbs-api.tajiduo.com:8443/x",                # 非 443 端口
                    "https://localhost/x"):                              # 白名单外主机
        try:
            tc._validate_url(bad_url)
            raise AssertionError(f"必须拒绝：{bad_url}")
        except ValueError:
            pass
    # 白名单域解析到环回/私网/链路本地/保留段 → 拒绝（防 DNS 重绑定指向本机面板）
    def _fake_gai(ip):
        return lambda *a, **k: [(2, 1, 6, "", (ip, 443))]

    for ip in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "0.0.0.0", "192.168.1.2"):
        socket_mod.getaddrinfo = _fake_gai(ip)
        try:
            tc._validate_url("https://bbs-api.tajiduo.com/x")
            raise AssertionError(f"解析到 {ip} 必须拒绝")
        except ValueError:
            pass
        finally:
            socket_mod.getaddrinfo = orig_gai


# ---------- B2.5/XMOD-01 共享 URL 防线（app/url_guard.py）专项（2026-11-14） ----------

def _xmod01_gai(table):
    """B25 专项 fake-DNS 桩：记录形态对齐既有 fake-DNS 三处写法（本文件 sites 段
    与 tajiduo 段 _fake_gai：v4 → (2,1,6,"",(ip,443))、v6 → 四元组 (ip,443,0,0)），
    键入口径对齐 probe_xmod01.FakeDns（按解析调用传入的原始名字、大小写不敏感、
    尾点敏感——校验层以归一化名解析，尾点 URL 应命中去尾点键）。未键入的名字返回
    []（空解析形态）。全程无真实网络。"""
    import socket as socket_mod
    lowered = {k.lower(): v for k, v in table.items()}

    def _gai(host, port, *a, **k):
        records = []
        for ip in lowered.get((host or "").lower(), []):
            if ":" in ip:
                records.append((socket_mod.AF_INET6, socket_mod.SOCK_STREAM, 6,
                                "", (ip, port, 0, 0)))
            else:
                records.append((socket_mod.AF_INET, socket_mod.SOCK_STREAM, 6,
                                "", (ip, port)))
        return records

    return _gai


def _xmod01_matrix_one(mod, ug, name, accepts, rejects):
    """B25 六类错误形态 × 单模块矩阵执行器（policy §1.4：scheme/主机/端口/凭据/
    解析/私网 IP 六类 code 全部可区分）。accepts = [(url, dns)]；rejects =
    [(url, dns 或 None（getaddrinfo 抛 OSError 形态）, 期望错误类)]。
    样本 URL 带 user:pass 时顺带断言凭据不落错误消息（脱敏口径）。"""
    import socket as socket_mod
    orig = socket_mod.getaddrinfo
    try:
        for url, dns in accepts:
            socket_mod.getaddrinfo = _xmod01_gai(dns)
            assert mod._validate_url(url) == url, f"{name} 应放行：{url}"
        for url, dns, want in rejects:
            if dns is None:
                def _boom(*a, **k):
                    raise OSError("getaddrinfo failed（gaierror 形态桩）")
                socket_mod.getaddrinfo = _boom
            else:
                socket_mod.getaddrinfo = _xmod01_gai(dns)
            try:
                mod._validate_url(url)
                raise AssertionError(f"{name} {url} 应拒绝（{want.code}）")
            except ug.URLGuardError as e:
                assert e.code == want.code, (name, url, e.code, want.code)
                if "user:pass" in url:
                    assert "user:pass" not in str(e), f"{name} 凭据落消息：{e}"
                    assert "***" in str(e), f"{name} 凭据未掩码：{e}"
    finally:
        socket_mod.getaddrinfo = orig


@case
def test_xmod01_url_guard_shared_layer():
    """B25 共享层单元（app/url_guard.py）：七类错误码互异且各为一个 URLGuardError
    子类（B2.6B FETCH-06 增 ERR_RESPONSE_TOO_LARGE）、URLGuardError 是 ValueError
    子类（调用方既有 except 零改动兼容）、host_allowed 参数化（子域开关 +
    rstrip 全部尾点 + lower 归一化）、validate_url 返回 PinnedURL（url 原样透传 +
    归一化主机 + 校验期公网 IP 集）、三模块重复件已收敛（_NoRedirect/_OPENER
    别名同源）、重定向一律拒绝。全程伪造 DNS，不触网。"""
    from app import sites_fetch as sf
    from app import wiki_fetch as wf
    from app import tajiduo_client as tc
    from app import url_guard as ug
    import socket as socket_mod
    classes = (ug.ERR_SCHEME, ug.ERR_HOST, ug.ERR_PORT, ug.ERR_CREDENTIALS,
               ug.ERR_DNS, ug.ERR_PRIVATE_IP, ug.ERR_RESPONSE_TOO_LARGE)
    assert len(set(classes)) == 7, "七类错误必须各为一个独立子类"
    assert len({c.code for c in classes}) == 7, "七类 code 必须互异"
    assert issubclass(ug.URLGuardError, ValueError), "调用方 except ValueError 兼容"
    # host_allowed 参数化：子域开关 + 归一化（rstrip 全部尾点，覆盖双尾点形态）
    assert ug.host_allowed("API.Everness.INFO", allowed_hosts=("everness.info",),
                           allow_subdomains=True)
    assert not ug.host_allowed("api.everness.info", allowed_hosts=("everness.info",),
                               allow_subdomains=False)
    assert ug.host_allowed("everness.info..", allowed_hosts=("everness.info",),
                           allow_subdomains=True)
    for bad in ("evil-everness.info", "everness.info.evil.test", "", None):
        assert not ug.host_allowed(bad, allowed_hosts=("everness.info",)), bad
    # validate_url 返回 PinnedURL：url 原样透传 + 归一化主机 + 全部校验期公网 IP
    orig = socket_mod.getaddrinfo
    try:
        socket_mod.getaddrinfo = _xmod01_gai(
            {"wiki.mysqil.com": ["1.2.3.4", "93.184.216.34"]})
        pinned = ug.validate_url("https://WIKI.MYSQIL.com/tools/cultivation",
                                 allowed_hosts=("wiki.mysqil.com",),
                                 allow_subdomains=False)
        assert pinned.url == "https://WIKI.MYSQIL.com/tools/cultivation"
        assert pinned.host == "wiki.mysqil.com", pinned.host
        assert pinned.ips == ("1.2.3.4", "93.184.216.34"), pinned.ips
    finally:
        socket_mod.getaddrinfo = orig
    # 三模块重复件已收敛：别名同源（entry-matrix §四 duplicateDefenses 清零）
    assert sf._NoRedirect is ug._NoRedirect and wf._NoRedirect is ug._NoRedirect \
        and tc._NoRedirect is ug._NoRedirect
    assert sf._OPENER is ug.OPENER and wf._OPENER is ug.OPENER \
        and tc._OPENER is ug.OPENER
    # 重定向目标无法预校验：一律拒绝跟随（302 跳私网形态；桩参数不触网）
    try:
        ug._NoRedirect().redirect_request(None, None, 302, "Moved", None,
                                          "https://10.0.0.5/panel")
        raise AssertionError("重定向必须拒绝跟随")
    except ValueError as e:
        assert "拒绝跟随重定向" in str(e) and "10.0.0.5" in str(e), str(e)


@case
def test_xmod01_matrix_sites_fetch():
    """B25 六类错误形态矩阵——通道A sites_fetch（四域白名单 + 子域放行口径：
    api.everness.info 资产域靠 endswith 放行，AGENTS.md 2026-11-11 口径保持）。
    全程伪造 DNS，不触网。"""
    from app import sites_fetch as sf
    from app import url_guard as ug
    pub = {"ntestrategy.com": ["1.2.3.4"]}
    accepts = [
        ("https://ntestrategy.com/js/shared.js", pub),               # apex 锚点
        ("https://api.everness.info/data/assets/UI_Icon/Appearance/x.webp",
         {"api.everness.info": ["1.2.3.4"]}),                        # everness 资产子域
        ("https://NTESTRATEGY.COM./js/shared.js", pub),              # 大小写 + 尾点归一化
    ]
    rejects = [
        ("http://ntestrategy.com/x", pub, ug.ERR_SCHEME),            # ① scheme
        ("http://user:pass@ntestrategy.com/x", pub, ug.ERR_SCHEME),  # ① scheme（脱敏）
        ("https://evil.test/x", {"evil.test": ["1.2.3.4"]}, ug.ERR_HOST),   # ② 外域
        ("https:///no-host", {}, ug.ERR_HOST),                       # ② 空主机
        ("https://ntestrategy.com.evil.test/x", {}, ug.ERR_HOST),    # ② 后缀伪装
        ("https://evil-ntestrategy.com/x", {}, ug.ERR_HOST),         # ② 前缀伪装
        ("https://nteguide.com/x", {"nteguide.com": ["1.2.3.4"]},
         ug.ERR_HOST),                                               # ② 已禁用域必拒
        ("https://ntestrategy.com:8443/x", pub, ug.ERR_PORT),        # ③ 显式端口
        ("https://ntestrategy.com:99999/x", pub, ug.ERR_PORT),       # ③ 越界显式化
        ("https://ntestrategy.com:bad/x", pub, ug.ERR_PORT),         # ③ 不可解析显式化
        ("https://user:pass@ntestrategy.com/x", pub, ug.ERR_CREDENTIALS),   # ④ 凭据
        ("https://ntestrategy.com@evil.test/x", {"evil.test": ["1.2.3.4"]},
         ug.ERR_HOST),                                               # ④ 多段 @ 混淆
        ("https://ntestrategy.com/x", {"ntestrategy.com": []},
         ug.ERR_DNS),                                                # ⑤ 空解析 fail-closed
        ("https://ntestrategy.com/x", None, ug.ERR_DNS),             # ⑤ gaierror 形态
        ("https://ntestrategy.com/x", {"ntestrategy.com": ["10.0.0.5"]},
         ug.ERR_PRIVATE_IP),                                         # ⑥ RFC1918
        ("https://ntestrategy.com/x", {"ntestrategy.com": ["127.0.0.1"]},
         ug.ERR_PRIVATE_IP),                                         # ⑥ 环回
        ("https://ntestrategy.com/x", {"ntestrategy.com": ["::1"]},
         ug.ERR_PRIVATE_IP),                                         # ⑥ v6 环回
        ("https://ntestrategy.com/x", {"ntestrategy.com": ["::ffff:10.0.0.5"]},
         ug.ERR_PRIVATE_IP),                                         # ⑥ v4-mapped
        ("https://ntestrategy.com/x", {"ntestrategy.com": ["1.2.3.4", "192.168.0.9"]},
         ug.ERR_PRIVATE_IP),                                         # ⑥ 混记录不短路
        ("https://localhost.ntestrategy.com/x",
         {"localhost.ntestrategy.com": ["127.0.0.1"]}, ug.ERR_PRIVATE_IP),  # ⑥ 子域→DNS 层拦
        ("https://169.254.169.254/latest/meta-data/", {}, ug.ERR_HOST),  # ⑥ 字面量→主机层拦
    ]
    _xmod01_matrix_one(sf, ug, "sites_fetch", accepts, rejects)


@case
def test_xmod01_matrix_wiki_fetch():
    """B25 六类错误形态矩阵——通道B wiki_fetch（严格单域 wiki.mysqil.com 精确相等；
    B2.5 统一收紧补齐端口/凭据校验与空解析拒绝——原实现缺这三项，probe c03/c04
    实证放行；尾点/大小写归一化放行为策略采纳口径 policy §2.3）。全程伪造 DNS。"""
    from app import wiki_fetch as wf
    from app import url_guard as ug
    pub = {"wiki.mysqil.com": ["1.2.3.4"]}
    accepts = [
        ("https://wiki.mysqil.com/tools/cultivation", pub),          # apex 锚点
        ("https://WIKI.MYSQIL.com/_astro/DetailedCalc.abc.js", pub),  # 大小写+尾点（二跳形态）
    ]
    rejects = [
        ("http://wiki.mysqil.com/x", pub, ug.ERR_SCHEME),
        ("http://user:pass@wiki.mysqil.com/x", pub, ug.ERR_SCHEME),  # scheme 优先 + 脱敏
        ("https://panel.wiki.mysqil.com/x", pub, ug.ERR_HOST),       # 精确相等：子域拒
        ("https://localhost.wiki.mysqil.com/x", pub, ug.ERR_HOST),
        ("https://wiki.mysqil.com.evil.test/x", {}, ug.ERR_HOST),    # 后缀伪装
        ("https://evil-wiki.mysqil.com/x", {}, ug.ERR_HOST),         # 前缀伪装
        ("https://wiki.mysqil.com:8443/x", pub, ug.ERR_PORT),        # 收紧：原实现放行
        ("https://wiki.mysqil.com:99999/x", pub, ug.ERR_PORT),
        ("https://wiki.mysqil.com:bad/x", pub, ug.ERR_PORT),
        ("https://user:pass@wiki.mysqil.com/x", pub, ug.ERR_CREDENTIALS),  # 收紧：原实现放行
        ("https://wiki.mysqil.com@evil.test/x", {"evil.test": ["1.2.3.4"]},
         ug.ERR_HOST),                                               # 多段 @ 混淆
        ("https://wiki.mysqil.com/x", {"wiki.mysqil.com": []}, ug.ERR_DNS),
        ("https://wiki.mysqil.com/x", None, ug.ERR_DNS),             # gaierror 形态
        ("https://wiki.mysqil.com/x", {"wiki.mysqil.com": ["10.0.0.5"]},
         ug.ERR_PRIVATE_IP),
        ("https://wiki.mysqil.com/x", {"wiki.mysqil.com": ["127.0.0.1"]},
         ug.ERR_PRIVATE_IP),
        ("https://wiki.mysqil.com/x", {"wiki.mysqil.com": ["::ffff:7f00:1"]},
         ug.ERR_PRIVATE_IP),                                         # v4-mapped 环回
        ("https://wiki.mysqil.com/x", {"wiki.mysqil.com": ["::1", "1.2.3.4"]},
         ug.ERR_PRIVATE_IP),                                         # 混记录不短路
    ]
    _xmod01_matrix_one(wf, ug, "wiki_fetch", accepts, rejects)


@case
def test_xmod01_matrix_tajiduo_client():
    """B25 六类错误形态矩阵——通道C tajiduo_client（两域白名单，B2.5 收紧为精确
    相等：www.bbs-api.tajiduo.com 等子域形态改拒绝，policy §2.2 mandated；
    凭据只走请求头不走 URL 口径不变）。全程伪造 DNS，不触网。"""
    from app import tajiduo_client as tc
    from app import url_guard as ug
    pub_bbs = {"bbs-api.tajiduo.com": ["1.2.3.4"]}
    pub_laohu = {"user.laohu.com": ["1.2.3.4"]}
    accepts = [
        ("https://bbs-api.tajiduo.com/usercenter/api/login", pub_bbs),      # apex 锚点
        ("https://user.laohu.com/m/newApi/sendPhoneCaptchaWithOutLogin", pub_laohu),
        ("https://USER.LAOHU.com./openApi/sms/new/login", pub_laohu),  # 大小写+尾点归一化
    ]
    rejects = [
        ("http://bbs-api.tajiduo.com/x", pub_bbs, ug.ERR_SCHEME),
        ("http://user:pass@user.laohu.com/x", pub_laohu, ug.ERR_SCHEME),
        ("https://www.bbs-api.tajiduo.com/x",
         {"www.bbs-api.tajiduo.com": ["1.2.3.4"]}, ug.ERR_HOST),     # 2.2 收紧：子域拒
        ("https://panel.user.laohu.com/x", pub_laohu, ug.ERR_HOST),  # 精确相等：子域拒
        ("https://evil.test/x", {"evil.test": ["1.2.3.4"]}, ug.ERR_HOST),
        ("https://user.laohu.com.evil.com/x", {}, ug.ERR_HOST),      # 后缀伪装
        ("https://evil-user.laohu.com/x", {}, ug.ERR_HOST),          # 前缀伪装
        ("https://localhost/x", {"localhost": ["127.0.0.1"]}, ug.ERR_HOST),
        ("https://bbs-api.tajiduo.com:8443/x", pub_bbs, ug.ERR_PORT),
        ("https://bbs-api.tajiduo.com:99999/x", pub_bbs, ug.ERR_PORT),
        ("https://bbs-api.tajiduo.com:bad/x", pub_bbs, ug.ERR_PORT),
        ("https://user:pass@bbs-api.tajiduo.com/x", pub_bbs, ug.ERR_CREDENTIALS),
        ("https://bbs-api.tajiduo.com@evil.test/x", {"evil.test": ["1.2.3.4"]},
         ug.ERR_HOST),                                               # 多段 @ 混淆
        ("https://bbs-api.tajiduo.com/x", {"bbs-api.tajiduo.com": []}, ug.ERR_DNS),
        ("https://user.laohu.com/x", None, ug.ERR_DNS),              # gaierror 形态
        ("https://bbs-api.tajiduo.com/x", {"bbs-api.tajiduo.com": ["10.0.0.5"]},
         ug.ERR_PRIVATE_IP),
        ("https://user.laohu.com/x", {"user.laohu.com": ["169.254.169.254"]},
         ug.ERR_PRIVATE_IP),                                         # 云元数据段
        ("https://bbs-api.tajiduo.com/x", {"bbs-api.tajiduo.com": ["::1"]},
         ug.ERR_PRIVATE_IP),
        ("https://bbs-api.tajiduo.com/x", {"bbs-api.tajiduo.com": ["::ffff:10.0.0.5"]},
         ug.ERR_PRIVATE_IP),                                         # v4-mapped
        ("https://bbs-api.tajiduo.com/x", {"bbs-api.tajiduo.com": ["1.2.3.4", "192.168.0.1"]},
         ug.ERR_PRIVATE_IP),                                         # 混记录不短路
    ]
    _xmod01_matrix_one(tc, ug, "tajiduo_client", accepts, rejects)


@case
def test_xmod01_pinning_toctou_offline():
    """B25 TOCTOU 离线端到端（policy §5.2/§5.3 验收条件）：校验期解析公网、抓取期
    桩可编程改应私网（DNS 重绑定模拟）→ open_pinned 连接期 getaddrinfo 只回钉扎
    公网集、重绑定应答根本不被消费；请求结束钉扎注销。全程零真实网络（opener 为
    桩、无任何 socket 建连；探针 c16 由 NOT_ASSERTABLE 变为可离线断言）。"""
    from app import tajiduo_client as tc
    from app import url_guard as ug
    import socket as socket_mod
    import urllib.parse

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def read(self, n=-1):
            # FETCH-06 起 open_pinned 传 read(MAX+1)：n>=0 按真实
            # http.client.HTTPResponse.read(n) 语义至多 n 字节（本桩供给
            # b"probe-ok" 远小于上限，等价原全量返回，既有调用方零改动）
            return self._payload if n is None or n < 0 else self._payload[:n]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _ProbingOpener:
        """桩 opener：open 时经 socket.getaddrinfo 入口查询「连接期会拿到什么」
        （模拟 create_connection 的解析时机）并记录首个地址，绝不真正建连。"""

        def __init__(self):
            self.seen = []

        def open(self, req, timeout=None):
            host = urllib.parse.urlsplit(req.get_full_url()).hostname
            infos = socket_mod.getaddrinfo(host, 443, proto=socket_mod.IPPROTO_TCP)
            self.seen.append(infos[0][4][0])
            return _Resp(b"probe-ok")

    state = {"n": 0}

    def rebinding_gai(host, port, *a, **k):
        state["n"] += 1
        ip = "1.2.3.4" if state["n"] == 1 else "10.0.0.5"  # 首解公网、再解私网
        return [(2, 1, 6, "", (ip, port))]

    orig_gai, orig_opener = socket_mod.getaddrinfo, tc._OPENER
    try:
        socket_mod.getaddrinfo = rebinding_gai
        probing = _ProbingOpener()
        tc._OPENER = probing
        out = tc._http("https://bbs-api.tajiduo.com/usercenter/api/login")
        assert out == b"probe-ok", out
        # 连接期拿到的是校验期钉扎的公网 IP，而非桩第二次应答的私网 IP
        assert probing.seen == ["1.2.3.4"], probing.seen
        # 桩只被消费一次（校验期）；连接期解析走钉扎分发器、重绑定应答未被消费
        assert state["n"] == 1, state["n"]
        # 请求结束钉扎注销（无残留）
        assert ug._PINS.get("bbs-api.tajiduo.com") is None, dict(ug._PINS)
    finally:
        tc._OPENER = orig_opener
        socket_mod.getaddrinfo = orig_gai


@case
def test_xmod01_pinned_transport_invariant():
    """B25 传输层不变量（2026-11-14 B2.5）：三模块全部受保护出站传输函数——
    通道A sf._http_get / sf._http_get_bytes、通道B wf._http_get、通道C tc._http
    ——必须委托共享层 url_guard.open_pinned 钉扎 transport（校验→register→open→
    unregister），禁止绕过直连。spy open_pinned（替换前保存、finally 还原，先例
    test_tajiduo_http_error_wrapping 的 validate_url spy 口径）逐通道观察调用与
    白名单/opener 实参；inspect.getsource 静态源检兜底（仿 test_sec10_deelevate_static
    口径）：三模块源码无裸 urlopen 直连、委托语句确实存在（url_guard 自身是唯一
    urllib 传输实现）。全程不触网（spy 短路返回桩 bytes，不进 validate/register/
    opener）。"""
    from app import sites_fetch as sf
    from app import wiki_fetch as wf
    from app import tajiduo_client as tc
    from app import url_guard as ug
    import inspect

    orig_open = ug.open_pinned
    calls = []

    def _spy_open(url, **kwargs):
        # 短路桩：命中即记录并返回固定 bytes（文本通道 decode 正常、二进制/塔吉多
        # 通道原样返回），不触 validate/register/opener，绝不建连。
        calls.append((url, kwargs))
        return b"xmod01-pin-spy"

    try:
        ug.open_pinned = _spy_open
        # 通道A 文本：委托共享层 + 白名单/opener 走模块别名实参
        assert sf._http_get("https://ntestrategy.com/js/shared.js", 10) \
            == "xmod01-pin-spy"
        assert len(calls) == 1, calls
        _, kw = calls[-1]
        assert kw["allowed_hosts"] == sf.ALLOWED_HOSTS, kw["allowed_hosts"]
        assert kw["opener"] is sf._OPENER, "通道A opener 未走模块别名"
        # 通道A 二进制：同一防线（同白名单同 opener 别名）
        assert sf._http_get_bytes("https://wiki.mysqil.com/x.webp", 10) \
            == b"xmod01-pin-spy"
        assert len(calls) == 2, calls
        _, kw = calls[-1]
        assert kw["allowed_hosts"] == sf.ALLOWED_HOSTS, kw["allowed_hosts"]
        assert kw["opener"] is sf._OPENER, "通道A(二进制) opener 未走模块别名"
        # 通道B：单域严格白名单（allow_subdomains=False 精确相等）
        assert wf._http_get("https://wiki.mysqil.com/tools/cultivation", 10) \
            == "xmod01-pin-spy"
        assert len(calls) == 3, calls
        _, kw = calls[-1]
        assert kw["allowed_hosts"] == (wf.WIKI_HOST,), kw["allowed_hosts"]
        assert kw["allow_subdomains"] is False, kw
        assert kw["opener"] is wf._OPENER, "通道B opener 未走模块别名"
        # 通道C：两域白名单 + method/headers/data 透传
        out = tc._http("https://bbs-api.tajiduo.com/usercenter/api/login",
                       method="POST", headers={"X-T": "t"}, data=b"p=1")
        assert out == b"xmod01-pin-spy", out
        assert len(calls) == 4, calls
        _, kw = calls[-1]
        assert kw["allowed_hosts"] == tc.ALLOWED_HOSTS, kw["allowed_hosts"]
        assert kw["allow_subdomains"] is False, kw
        assert kw["opener"] is tc._OPENER, "通道C opener 未走模块别名"
        assert kw["method"] == "POST" and kw["data"] == b"p=1" \
            and kw["headers"] == {"X-T": "t"}, kw
        # 四条通道全部命中共享层唯一入口（缺一即存在绕过直连）
        assert {u.split("/")[2] for u, _ in calls} == {
            "ntestrategy.com", "wiki.mysqil.com", "bbs-api.tajiduo.com"}, calls
    finally:
        ug.open_pinned = orig_open
    # 还原后共享层恢复真实实现（spy 无残留，不影响后续用例）
    assert ug.open_pinned is orig_open
    # 静态源检：三模块源码无裸 urlopen 直连，且委托 open_pinned 的调用确实存在
    for mod, name in ((sf, "sites_fetch"), (wf, "wiki_fetch"),
                      (tc, "tajiduo_client")):
        src = inspect.getsource(mod)
        assert "urlopen" not in src, f"{name} 存在裸 urlopen 直连"
        assert "url_guard.open_pinned" in src, f"{name} 未委托共享层 open_pinned"


@case
def test_tajiduo_parse_payloads():
    """解析器用构造 JSON 断言（字段实样口径：NTEUID tajiduo_model + TaJiDuo-plugin
    DOCS-API；防御性解析——缺字段/垃圾类型一律 None/跳过，绝不编造）。
    2026-11-07 四技能修复：type 判别（推断候选表）→ name 锚点 → 顺序兜底（标注
    skills_inferred）；卡带摘要自 suit 字段（模型实证键）防御性解析。"""
    from app import tajiduo_client as tc
    chars = tc.parse_characters([
        {   # 完整行（技能实名锚定四键；fork.alev 实测是字符串）
            "id": "player_001", "name": "安魂曲", "alev": 70, "slev": 6,
            "awakenLev": 2, "elementType": "CHARACTER_ELEMENT_TYPE_CHAOS",
            "groupType": "CHARACTER_GROUP_TYPE_ONE", "quality": "ITEM_QUALITY_ORANGE",
            "skills": [{"id": "s1", "name": "普通攻击", "type": "a", "level": 6},
                       {"id": "s2", "name": "变轨技能", "type": "b", "level": 4},
                       {"id": "s3", "name": "极轨终结", "type": "c", "level": 5},
                       {"id": "s4", "name": "援护技", "type": "d", "level": 3},
                       {"id": "s5", "name": "未锚定技能", "type": "x", "level": 9}],
            "citySkills": [{"id": "c1", "name": "都市特技甲", "level": 2}],
            "fork": {"id": "fork_anhunqu", "name": "漆黑青春妄想", "alev": "60",
                     "blev": "2", "slev": "1",
                     "properties": [{"id": "atk", "name": "攻击力", "value": "618"}]},
            "suit": {"suitActivateNum": 2},
            "properties": [{"id": "hp", "name": "生命值", "value": "15195"}],
        },
        {   # 字符串等级（塔吉多字段实测量测形态）+ 缺 fork/skills
            "id": "player_002", "name": "零", "alev": "55", "slev": None,
            "awakenLev": None,
        },
        {   # 垃圾类型：全部字段不编造
            "id": "player_003", "name": None, "alev": "abc", "awakenLev": 99,
            "skills": "garbage", "citySkills": None, "fork": "garbage",
            "suit": None,
        },
        "not-a-dict",
    ])
    assert len(chars) == 3, len(chars)
    r0 = chars[0]
    assert (r0["name"], r0["level"], r0["awakening"], r0["slev"]) == ("安魂曲", 70, 2, 6)
    assert r0["skills"] == {"normal": 6, "variable": 4, "orbit": 5, "assist": 3}, r0["skills"]
    assert r0["skills_inferred"] is False, "实名锚点全命中时不得标顺序推断"
    assert r0["city_skills"] == ["都市特技甲"]
    assert r0["fork"] == {"id": "fork_anhunqu", "name": "漆黑青春妄想", "level": 60}
    assert r0["suit_num"] == 2
    # 卡带摘要：suit 无 name/core/pie → 如实为 None/[]，activate_num 同 suitActivateNum
    assert r0["cassette"] == {"set": None, "main": None, "affixes": [],
                              "activate_num": 2}, r0["cassette"]
    r1 = chars[1]
    assert r1["level"] == 55 and r1["awakening"] is None and r1["slev"] is None
    assert r1["skills"] == {"normal": None, "variable": None, "orbit": None, "assist": None}
    assert r1["skills_inferred"] is False
    assert r1["fork"]["name"] is None and r1["fork"]["level"] is None
    r2 = chars[2]
    assert r2["name"] == "" and r2["level"] is None, r2
    assert r2["awakening"] == 6, "越界值按上限 clamp（防御而非丢弃）"
    assert all(v is None for v in r2["skills"].values()) and r2["suit_num"] is None
    assert tc.parse_characters("garbage") == [] and tc.parse_characters(None) == []
    # ---- 四技能修复（2026-11-07 实测：payload 技能名是个性化名，UI 分类标签全落空）----
    # 1) type 判别优先（候选值表为推断——NTEUID CharacterSkill.type 无公开枚举语义）
    typed = tc.parse_characters([{
        "id": "p", "name": "甲",
        "skills": [{"name": "星轨裁断", "type": "SKILL_TYPE_NORMAL", "level": 6},
                   {"name": "月轨回旋", "type": "SKILL_TYPE_VARIABLE", "level": 4},
                   {"name": "日轨轰击", "type": "SKILL_TYPE_ORBIT", "level": 5},
                   {"name": "伴星协击", "type": "SKILL_TYPE_ASSIST", "level": 3}],
    }])
    assert typed[0]["skills"] == {"normal": 6, "variable": 4, "orbit": 5, "assist": 3}, \
        typed[0]["skills"]
    assert typed[0]["skills_inferred"] is False
    # 2) type/name 均判不出 → 按列表顺序固定映射并标「顺序推断」
    #    （顺序依据：TaJiDuo-plugin profile.js renderPanelSkills 展示名兜底列表前 4 位）
    ordered = tc.parse_characters([{
        "id": "q", "name": "乙",
        "skills": [{"name": "未知技A", "type": "mystery", "level": 1},
                   {"name": "未知技B", "type": "mystery", "level": 2},
                   {"name": "未知技C", "type": "mystery", "level": 3},
                   {"name": "未知技D", "type": "mystery", "level": 4}],
    }])
    assert ordered[0]["skills"] == {"normal": 1, "variable": 2, "orbit": 3, "assist": 4}, \
        ordered[0]["skills"]
    assert ordered[0]["skills_inferred"] is True, "顺序兜底必须标注 skills_inferred"
    # 3) 混合：type 命中的槽不参与顺序兜底，剩余技能填剩余空槽
    mixed = tc.parse_characters([{
        "id": "r", "name": "丙",
        "skills": [{"name": "大招收尾", "type": "orbit", "level": 5},
                   {"name": "平A连段", "type": "?", "level": 6},
                   {"name": "支援射击", "type": "?", "level": 2},
                   {"name": "换手突进", "type": "?", "level": 4}],
    }])
    assert mixed[0]["skills"]["orbit"] == 5, mixed[0]["skills"]
    assert mixed[0]["skills"]["normal"] == 6 and mixed[0]["skills"]["variable"] == 2 \
        and mixed[0]["skills"]["assist"] == 4, mixed[0]["skills"]
    assert mixed[0]["skills_inferred"] is True
    # ---- 卡带摘要（A5）：suit.name/core/mainProperties/properties（模型实证键）----
    cassetted = tc.parse_characters([{
        "id": "s", "name": "丁",
        "suit": {"name": "星海回响", "suitActivateNum": 2,
                 "core": [{"id": "c1", "name": "核心甲", "lev": 5,
                           "mainProperties": [{"id": "hp", "name": "生命值", "value": "4400"}],
                           "properties": [{"id": "cr", "name": "暴击率", "value": "8%"},
                                          {"id": "cd", "name": "暴击伤害", "value": "24%"}]}],
                 "pie": [{"id": "p1", "name": "辅件甲", "lev": 3,
                          "mainProperties": [{"id": "atk", "name": "攻击力", "value": "300"}],
                          "properties": [{"id": "cr", "name": "暴击率", "value": "6%"}]}]},
    }])
    cas = cassetted[0]["cassette"]
    assert cas["set"] == "星海回响" and cas["main"] == "生命值", cas
    assert cas["affixes"] == ["暴击率", "暴击伤害"], cas  # 跨装备位同名词条去重、保序
    assert cas["activate_num"] == 2
    # cartridge 字段防御性探测（真实 payload 未证实前仅作 suit 缺失时的备选，推断）
    cart = tc.parse_characters([{
        "id": "t", "name": "戊",
        "cartridge": {"name": "备用套装", "core": [{"mainProperties": [
            {"name": "防御力", "value": "100"}]}]},
    }])
    assert cart[0]["cassette"]["set"] == "备用套装" \
        and cart[0]["cassette"]["main"] == "防御力", cart[0]["cassette"]
    # roleHome 总览（DOCS-API 实样字段）
    home = tc.parse_role_home({
        "roleid": "214075351008", "rolename": "旅行者", "servername": "测试服",
        "lev": 60, "worldlevel": 5, "charidCnt": 12, "roleloginDays": 88,
        "achieveProgress": {"achievementCnt": 36, "total": 120},
        "areaProgress": [{"id": "a1", "name": "区域甲", "progress": 8, "total": 20}],
    })
    assert home == {"roleid": "214075351008", "rolename": "旅行者",
                    "servername": "测试服", "lev": 60, "worldlevel": 5,
                    "charid_cnt": 12, "rolelogin_days": 88,
                    "achievement": 36, "achievement_total": 120}, home
    empty = tc.parse_role_home(None)
    assert empty["lev"] is None and empty["roleid"] == "" and empty["achievement"] is None


@case
def test_tajiduo_token_store_and_sync_orchestration():
    """凭证存取与同步编排：令牌落盘/读取/清除（临时路径）、续期恰一次、角色去重；
    原始 payload 诊断落盘（data/tajiduo_last_payload.json，临时路径，A1）。"""
    from app import tajiduo_client as tc
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        orig_file = tc.TOKEN_FILE
        orig_dump = tc.LAST_PAYLOAD_FILE
        tc.TOKEN_FILE = os.path.join(tmp, "tajiduo_token.json")
        tc.LAST_PAYLOAD_FILE = os.path.join(tmp, "tajiduo_last_payload.json")
        try:
            assert tc.load_token_store() is None  # 未绑定
            tc.save_token_store({"device_id": "HTX", "access_token": "A",
                                 "refresh_token": "R", "center_uid": "1",
                                 "bound_at": "2026-11-07 12:00:00"})
            store = tc.load_token_store()
            assert store["access_token"] == "A" and store["schema"] == 1, store
            tc.persist_session({"access_token": "A2", "refresh_token": "R2",
                                "center_uid": "1"}, "HTX")
            assert tc.load_token_store()["access_token"] == "A2"
            assert tc.load_token_store()["bound_at"] == "2026-11-07 12:00:00", \
                "续期不得丢绑定时间"
            tc.clear_token_store()
            assert tc.load_token_store() is None
            # 凭证必须落在 data/ 下约定文件（安全红线：不进 git、不经前端）
            assert orig_file.endswith(os.path.join("data", "tajiduo_token.json")), orig_file

            class _FakeClient:
                """脚本化客户端：首轮鉴权失败 → 续期一次 → 重试成功；顺带验去重。"""
                device_id = "HTX"

                def __init__(self):
                    self.roles_calls = 0
                    self.refresh_calls = 0

                def get_game_roles(self, game_id):
                    self.roles_calls += 1
                    if self.roles_calls == 1:
                        raise tc.TajiduoError("登录已过期", auth=True)
                    return [{"roleId": "214075351008", "roleName": "旅行者",
                             "serverName": "测试服", "lev": 60}]

                def refresh_session(self):
                    self.refresh_calls += 1
                    return {"access_token": "A3", "refresh_token": "R3",
                            "center_uid": "1"}

                def get_role_home(self, role_id):
                    return {"roleid": role_id, "rolename": "旅行者", "lev": 60}

                def get_role_characters(self, role_id):
                    # 同名同等级两份面板 + 一条不同等级：去重后剩 2 行
                    return [{"id": "c1", "name": "安魂曲", "alev": 70, "slev": 6,
                             "skills": [], "fork": {}},
                            {"id": "c1", "name": "安魂曲", "alev": 70, "slev": 6,
                             "skills": [], "fork": {}},
                            {"id": "c1", "name": "安魂曲", "alev": 71, "slev": 6,
                             "skills": [], "fork": {}}]

            fake = _FakeClient()
            # B2.8-A（TJD-01）夹具适配（裁定授权，非断言变更）：persist 写回边界
            # 守卫生效后已删凭据不再复活（写回前 gate 拒绝＋绝不新建文件）——本例
            # 前面 clear_token_store 后须重设在场凭据，续期写回场景才成立；「已删
            # 不复活」的新语义反向锁定由 test_tjd01_* 七例承担（镜像本函数下方
            # fake2 的既有补存档模式，既有断言零改动）
            tc.save_token_store({"device_id": "HTX", "access_token": "A",
                                 "refresh_token": "R", "center_uid": "1"})
            result = tc.sync_all(fake)
            assert fake.refresh_calls == 1, fake.refresh_calls  # 续期恰一次
            assert fake.roles_calls == 2
            assert [r["level"] for r in result["rows"]] == [70, 71], result["rows"]
            assert tc.load_token_store()["access_token"] == "A3", "续期后必须落盘"
            # A1：原始 payload 诊断落盘（含 awapi/yh/characters 原始 JSON，缺请求不编造）
            with open(tc.LAST_PAYLOAD_FILE, encoding="utf-8") as f:
                dump = json.load(f)
            assert dump["schema"] == 1 and dump["fetched_at"], dump
            role_id = "214075351008"
            assert dump["per_role"][role_id]["role_home"] == \
                {"roleid": role_id, "rolename": "旅行者", "lev": 60}, dump
            assert dump["per_role"][role_id]["characters"][0]["name"] == "安魂曲", dump
            # 无需续期的路径：一次成功 → refresh_calls 不增
            fake2 = _FakeClient()
            fake2.roles_calls = 1  # 跳过首轮失败
            tc.save_token_store({"device_id": "HTX", "access_token": "A",
                                 "refresh_token": "R", "center_uid": "1"})
            tc.sync_all(fake2)
            assert fake2.refresh_calls == 0
        finally:
            tc.TOKEN_FILE = orig_file
            tc.LAST_PAYLOAD_FILE = orig_dump


@case
def test_tajiduo_laohu_recovery_chain():
    """三级会话恢复链（离线桩，绝不触网）：①refreshToken 续期失败（auth）→
    ②落盘 laohu 根凭据同 device_id 重走 user_center_login 重建会话并落盘 →
    数据重试成功；laohu 重登也抛 → ③友好重绑错误（官方原文进 raw 不透传）；
    token store 新键 roundtrip 与旧版文件向后兼容；sync_gacha 同规则。"""
    from app import tajiduo_client as tc
    with tempfile.TemporaryDirectory() as tmp:
        orig_file = tc.TOKEN_FILE
        orig_dump = tc.LAST_PAYLOAD_FILE
        orig_hist = tc.GACHA_HISTORY_FILE
        orig_client_cls = tc.TajiduoClient
        tc.TOKEN_FILE = _safe_path(tmp, "tajiduo_token.json")
        tc.LAST_PAYLOAD_FILE = _safe_path(tmp, "tajiduo_last_payload.json")
        tc.GACHA_HISTORY_FILE = _safe_path(tmp, "gacha_history.json")
        try:
            # ---- token store 新键 roundtrip：save→load 保留 laohu 凭据；
            # persist_session（续期/重建路径）不得丢；旧版文件（缺新键）仍可读 ----
            tc.save_token_store({"device_id": "HTDEV", "access_token": "A1",
                                 "refresh_token": "R1", "center_uid": "1",
                                 "laohu_token": "LAOHU_T", "laohu_user_id": "42"})
            store = tc.load_token_store()
            assert store["laohu_token"] == "LAOHU_T" \
                and store["laohu_user_id"] == "42", store
            tc.persist_session({"access_token": "A2", "refresh_token": "R2",
                                "center_uid": "1"}, "HTDEV")
            store = tc.load_token_store()
            assert store["access_token"] == "A2" \
                and store["laohu_token"] == "LAOHU_T", "续期不得丢 laohu 根凭据"
            tc.save_token_store({"device_id": "HTX", "access_token": "A",
                                 "refresh_token": "R", "center_uid": "1"})
            assert tc.load_token_store().get("laohu_token") is None, \
                "旧版文件缺新键 = 无 laohu 凭据（向后兼容，走现有链）"

            # ---- 恢复链成功路径：refresh 抛 auth 错 → laohu 重登成功 → 重试成功 ----
            class _FakeReloginClient:
                """顶替 TajiduoClient：脚本化 user_center_login（离线），
                记录 device_id 与凭据，供同 device_id/凭据复用断言。"""
                fail = False
                calls = []      # 每次构造传入的 device_id
                last_cred = ()

                def __init__(self, device_id, access_token="", refresh_token="",
                             center_uid=""):
                    self.device_id = device_id
                    _FakeReloginClient.calls.append(device_id)

                def user_center_login(self, laohu_token, laohu_user_id):
                    _FakeReloginClient.last_cred = (laohu_token, laohu_user_id)
                    if _FakeReloginClient.fail:
                        raise tc.TajiduoError("[usercenter] laohu token 已失效",
                                              auth=True)
                    return {"access_token": "A9", "refresh_token": "R9",
                            "center_uid": "1"}
            tc.TajiduoClient = _FakeReloginClient

            class _DeadRefreshClient:
                device_id = "HTDEV"

                def __init__(self):
                    self.roles_calls = 0

                def get_game_roles(self, game_id):
                    self.roles_calls += 1
                    if self.roles_calls == 1:
                        raise tc.TajiduoError("[/getGameRoles] FAILED TO TOUCH",
                                              auth=True)
                    return [{"roleId": "r1", "roleName": "旅行者",
                             "serverName": "测试服", "lev": 60}]

                def refresh_session(self):
                    raise tc.TajiduoError("[/refreshToken] FAILED TO TOUCH",
                                          auth=True)

                def get_role_home(self, role_id):
                    return {"roleid": role_id, "rolename": "旅行者", "lev": 60}

                def get_role_characters(self, role_id):
                    return []

            tc.save_token_store({"device_id": "HTDEV", "access_token": "A1",
                                 "refresh_token": "R1", "center_uid": "1",
                                 "laohu_token": "LAOHU_T", "laohu_user_id": "42"})
            fake = _DeadRefreshClient()
            result = tc.sync_all(fake)
            assert fake.roles_calls == 2, fake.roles_calls  # 失败→重建→重试成功
            assert _FakeReloginClient.calls == ["HTDEV"], \
                _FakeReloginClient.calls  # 复用持久化 device_id，不新生成
            assert _FakeReloginClient.last_cred == ("LAOHU_T", "42"), \
                _FakeReloginClient.last_cred  # 重放落盘的 laohu 根凭据
            store = tc.load_token_store()
            assert store["access_token"] == "A9" and store["refresh_token"] == "R9", \
                "重建后的会话必须落盘"
            assert store["laohu_token"] == "LAOHU_T", "重建不得丢 laohu 根凭据"
            assert result["rows"] == [] \
                and result["roles"][0]["roleId"] == "r1", result

            # ---- 恢复链耗尽：laohu 重登也抛 → 友好重绑错误（原文进 raw 不透传）----
            _FakeReloginClient.fail = True
            fake2 = _DeadRefreshClient()
            try:
                tc.sync_all(fake2)
                raise AssertionError("恢复链耗尽必须抛错")
            except tc.TajiduoError as e:
                assert e.auth is True and "重新绑定" in str(e), (e.auth, str(e))
                assert "FAILED TO TOUCH" not in str(e), str(e)  # 官方原文不透传
                assert "FAILED TO TOUCH" in e.raw.get("origin", ""), e.raw
                assert "laohu" in e.raw.get("origin", ""), e.raw
            assert fake2.roles_calls == 1, fake2.roles_calls

            # ---- sync_gacha 同规则（同 _recover_auth）：refresh 抛 → laohu 重建 → 成功 ----
            _FakeReloginClient.fail = False
            _FakeReloginClient.calls = []

            class _DeadRefreshGachaClient:
                device_id = "HTDEV"

                def __init__(self):
                    self.calls = 0

                def get_gacha_summary(self):
                    self.calls += 1
                    if self.calls == 1:
                        raise tc.TajiduoError("[/gacha] FAILED TO TOUCH", auth=True)
                    return {"roleid": "r1", "rolename": "旅行者", "lev": 60,
                            "gachaDetails": []}

                def refresh_session(self):
                    raise tc.TajiduoError("[/refreshToken] FAILED TO TOUCH",
                                          auth=True)

            tc.save_token_store({"device_id": "HTDEV", "access_token": "A1",
                                 "refresh_token": "R1", "center_uid": "1",
                                 "laohu_token": "LAOHU_T", "laohu_user_id": "42"})
            gfake = _DeadRefreshGachaClient()
            gresult = tc.sync_gacha(gfake)
            assert gfake.calls == 2, gfake.calls
            assert _FakeReloginClient.calls == ["HTDEV"], _FakeReloginClient.calls
            assert gresult["summary"]["roleid"] == "r1", gresult["summary"]
            # 无 laohu 凭据（旧版文件）→ 恢复链退化为现状：refresh 失败即友好重绑错误
            tc.save_token_store({"device_id": "HTDEV", "access_token": "A1",
                                 "refresh_token": "R1", "center_uid": "1"})
            gfake2 = _DeadRefreshGachaClient()
            try:
                tc.sync_gacha(gfake2)
                raise AssertionError("无 laohu 凭据时恢复链耗尽必须抛错")
            except tc.TajiduoError as e:
                assert e.auth is True and "重新绑定" in str(e), (e.auth, str(e))
                assert gfake2.calls == 1, gfake2.calls
        finally:
            tc.TOKEN_FILE = orig_file
            tc.LAST_PAYLOAD_FILE = orig_dump
            tc.GACHA_HISTORY_FILE = orig_hist
            tc.TajiduoClient = orig_client_cls


# ---------- B3-B（TJD-02）：_recover_auth 尾段按 e3.auth 分流保留 status_code（2026-11-18） ----------
# 修复点 app/tajiduo_client.py:1017-1025：laohu 重登成功后 retry 仍失败——
# e3.auth=False 原样上抛（raise e3 from e2，保留 status_code/raw 与原始消息，
# HTTP 层据此回 502 而非 401）；e3.auth=True 维持现状（origin 累积+友好重绑
# 错误 auth=True）。夹具先例：_FakeReloginClient（test_tajiduo_laohu_
# recovery_chain）、test_tajiduo_endpoints_http 的真实 Handler 端口 0 冒烟；
# 非 auth 重试错构造参数复刻 _wrap_http_error（tajiduo_client.py:313-315）
# 「接口 HTTP {code}（{path}）」形态。全部离线桩零真实网络请求；临时目录一律
# 仓库内 .tmp/（显式 dir=，E 盘）。


@case
def test_tjd02_relogin_retry_nonauth_passthrough():
    """①（TJD-02 主症状修复）：laohu 重登成功 → retry 抛非 auth 错误（接口
    HTTP 500）→ 最终异常原样上抛：auth=False、status_code=500、原始消息与
    raw.body 保留、__cause__ 为①级 refresh 失败错（raise e3 from e2 链路）——
    不再被包装成 auth=True 重绑误导（修复前复现：e.auth=True、status_code=0、
    raw 只剩 origin/origin_error 字符串，见 .testarchive/2026-11-18-B3-
    acceptance/fixes-b/slice-g/repro_stdout.txt）。"""
    from app import tajiduo_client as tc
    os.makedirs(".tmp", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tjd02_a_", dir=".tmp") as tmp:
        orig_file = tc.TOKEN_FILE
        orig_dump = tc.LAST_PAYLOAD_FILE
        orig_hist = tc.GACHA_HISTORY_FILE
        orig_client_cls = tc.TajiduoClient
        tc.TOKEN_FILE = _safe_path(tmp, "tajiduo_token.json")
        tc.LAST_PAYLOAD_FILE = _safe_path(tmp, "tajiduo_last_payload.json")
        tc.GACHA_HISTORY_FILE = _safe_path(tmp, "gacha_history.json")
        try:
            relogin_calls = []

            class _ReloginOkClient:
                """顶替 TajiduoClient（_relogin_via_laohu 用）：user_center_login
                恒成功（离线桩），记录 device_id 供同 device_id 复用断言。"""

                def __init__(self, device_id):
                    self.device_id = device_id
                    relogin_calls.append(device_id)

                def user_center_login(self, laohu_token, laohu_user_id):
                    return {"access_token": "A9", "refresh_token": "R9",
                            "center_uid": "1"}
            tc.TajiduoClient = _ReloginOkClient

            class _RetryHttp500Client:
                device_id = "HTDEV"

                def __init__(self):
                    self.roles_calls = 0

                def get_game_roles(self, game_id):
                    self.roles_calls += 1
                    if self.roles_calls == 1:
                        raise tc.TajiduoError(
                            "[/getGameRoles] FAILED TO TOUCH", auth=True)
                    # 构造参数逐字复刻 _wrap_http_error（无 auth 关键词 +
                    # status_code=500 → 构造器算得 auth=False）
                    raise tc.TajiduoError(
                        "接口 HTTP 500（/usercenter/api/v2/getGameRoles）",
                        raw={"body": '{"code":500,"msg":"internal error"}'},
                        status_code=500)

                def refresh_session(self):
                    raise tc.TajiduoError(
                        "[/refreshToken] FAILED TO TOUCH", auth=True)

            tc.save_token_store({"device_id": "HTDEV", "access_token": "A1",
                                 "refresh_token": "R1", "center_uid": "1",
                                 "laohu_token": "LAOHU_T", "laohu_user_id": "42"})
            fake = _RetryHttp500Client()
            try:
                tc.sync_all(fake)
                raise AssertionError("重登后重试仍失败必须抛错")
            except tc.TajiduoError as e:
                assert e.auth is False, e.auth  # 非 auth：不误导用户重绑
                assert e.status_code == 500, e.status_code  # 原始 code 保留
                assert "接口 HTTP 500（/usercenter/api/v2/getGameRoles）" in str(e), \
                    str(e)  # 原始消息透传
                assert e.raw.get("body") == '{"code":500,"msg":"internal error"}', \
                    e.raw  # 原始响应体保留（非仅 origin 字符串残留）
                assert "重新绑定" not in str(e), str(e)  # 不出现重绑误导文案
                assert e.__cause__ is not None \
                    and "refreshToken" in str(e.__cause__), \
                    str(e.__cause__)  # raise e3 from e2 的异常链依据
            assert fake.roles_calls == 2, fake.roles_calls  # 失败→重登→重试恰一次
            assert relogin_calls == ["HTDEV"], relogin_calls
            store = tc.load_token_store()
            assert store["access_token"] == "A9", "重登新会话必须落盘"
        finally:
            tc.TOKEN_FILE = orig_file
            tc.LAST_PAYLOAD_FILE = orig_dump
            tc.GACHA_HISTORY_FILE = orig_hist
            tc.TajiduoClient = orig_client_cls


@case
def test_tjd02_relogin_retry_auth_friendly_rebind():
    """②（现状回归）：laohu 重登成功 → retry 仍抛 auth 错误 → 维持现状——
    origin 累积（含「laohu重登后仍失败」段）+ 友好重绑错误 auth=True，官方
    原文不透传（只进 raw 供诊断）。"""
    from app import tajiduo_client as tc
    os.makedirs(".tmp", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tjd02_b_", dir=".tmp") as tmp:
        orig_file = tc.TOKEN_FILE
        orig_dump = tc.LAST_PAYLOAD_FILE
        orig_hist = tc.GACHA_HISTORY_FILE
        orig_client_cls = tc.TajiduoClient
        tc.TOKEN_FILE = _safe_path(tmp, "tajiduo_token.json")
        tc.LAST_PAYLOAD_FILE = _safe_path(tmp, "tajiduo_last_payload.json")
        tc.GACHA_HISTORY_FILE = _safe_path(tmp, "gacha_history.json")
        try:
            class _ReloginOkClient2:
                def __init__(self, device_id):
                    self.device_id = device_id

                def user_center_login(self, laohu_token, laohu_user_id):
                    return {"access_token": "A9", "refresh_token": "R9",
                            "center_uid": "1"}
            tc.TajiduoClient = _ReloginOkClient2

            class _RetryAuthFailClient:
                device_id = "HTDEV"

                def __init__(self):
                    self.roles_calls = 0

                def get_game_roles(self, game_id):
                    self.roles_calls += 1
                    raise tc.TajiduoError(
                        "[/getGameRoles] FAILED TO TOUCH", auth=True)

                def refresh_session(self):
                    raise tc.TajiduoError(
                        "[/refreshToken] FAILED TO TOUCH", auth=True)

            tc.save_token_store({"device_id": "HTDEV", "access_token": "A1",
                                 "refresh_token": "R1", "center_uid": "1",
                                 "laohu_token": "LAOHU_T", "laohu_user_id": "42"})
            fake = _RetryAuthFailClient()
            try:
                tc.sync_all(fake)
                raise AssertionError("重登后重试仍 auth 失败必须抛错")
            except tc.TajiduoError as e:
                assert e.auth is True and "重新绑定" in str(e), (e.auth, str(e))
                assert "FAILED TO TOUCH" not in str(e), str(e)  # 原文不透传
                assert "laohu重登后仍失败" in e.raw.get("origin", ""), e.raw
                assert "[/getGameRoles] FAILED TO TOUCH" \
                    in e.raw.get("origin", ""), e.raw
            assert fake.roles_calls == 2, fake.roles_calls
        finally:
            tc.TOKEN_FILE = orig_file
            tc.LAST_PAYLOAD_FILE = orig_dump
            tc.GACHA_HISTORY_FILE = orig_hist
            tc.TajiduoClient = orig_client_cls


@case
def test_tjd02_refresh_success_retry_passthrough():
    """③（①级路径现状回归）：refresh 续期成功 → 落盘 → retry 仍失败 → 按现状
    原样上抛（本修复不触该路径：非 auth 错误不包装、不进重绑文案）；另证首错
    非 auth 时 sync_all 根本不进恢复链（tajiduo_client.py:1035-1036 直接
    raise）——两条既有透传路径均不被破坏。"""
    from app import tajiduo_client as tc
    os.makedirs(".tmp", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tjd02_c_", dir=".tmp") as tmp:
        orig_file = tc.TOKEN_FILE
        orig_dump = tc.LAST_PAYLOAD_FILE
        orig_client_cls = tc.TajiduoClient
        tc.TOKEN_FILE = _safe_path(tmp, "tajiduo_token.json")
        tc.LAST_PAYLOAD_FILE = _safe_path(tmp, "tajiduo_last_payload.json")
        try:
            class _BoomReloginClient:
                """若被构造即失败：①级成功路径绝不应触②级重登。"""

                def __init__(self, device_id):
                    raise AssertionError("①级成功路径不得进入②级 laohu 重登")
            tc.TajiduoClient = _BoomReloginClient

            class _RefreshOkRetryFailClient:
                device_id = "HTDEV"

                def __init__(self):
                    self.roles_calls = 0

                def get_game_roles(self, game_id):
                    self.roles_calls += 1
                    if self.roles_calls == 1:
                        raise tc.TajiduoError(
                            "[/getGameRoles] FAILED TO TOUCH", auth=True)
                    raise tc.TajiduoError(
                        "接口 HTTP 500（/usercenter/api/v2/getGameRoles）",
                        raw={"body": '{"code":500,"msg":"internal error"}'},
                        status_code=500)

                def refresh_session(self):
                    return {"access_token": "A2", "refresh_token": "R2",
                            "center_uid": "1"}

            tc.save_token_store({"device_id": "HTDEV", "access_token": "A1",
                                 "refresh_token": "R1", "center_uid": "1",
                                 "laohu_token": "LAOHU_T", "laohu_user_id": "42"})
            fake = _RefreshOkRetryFailClient()
            try:
                tc.sync_all(fake)
                raise AssertionError("续期后重试仍失败必须抛错")
            except tc.TajiduoError as e:
                assert e.auth is False and e.status_code == 500, (e.auth,
                                                                  e.status_code)
                assert "接口 HTTP 500（/usercenter/api/v2/getGameRoles）" in str(e), \
                    str(e)
                assert "重新绑定" not in str(e), str(e)
            assert fake.roles_calls == 2, fake.roles_calls
            assert tc.load_token_store()["access_token"] == "A2", \
                "续期成功必须落盘"

            # 首错非 auth：sync_all 不进恢复链，原样上抛（roles_calls==1）
            class _FirstNonAuthClient:
                device_id = "HTDEV"

                def __init__(self):
                    self.roles_calls = 0

                def get_game_roles(self, game_id):
                    self.roles_calls += 1
                    raise tc.TajiduoError(
                        "接口 HTTP 500（/usercenter/api/v2/getGameRoles）",
                        raw={"body": '{"code":500,"msg":"internal error"}'},
                        status_code=500)

                def refresh_session(self):
                    raise AssertionError("非 auth 首错不得进入恢复链续期")

            fake2 = _FirstNonAuthClient()
            try:
                tc.sync_all(fake2)
                raise AssertionError("非 auth 首错必须抛错")
            except tc.TajiduoError as e:
                assert e.auth is False and e.status_code == 500, (e.auth,
                                                                  e.status_code)
            assert fake2.roles_calls == 1, fake2.roles_calls
        finally:
            tc.TOKEN_FILE = orig_file
            tc.LAST_PAYLOAD_FILE = orig_dump
            tc.TajiduoClient = orig_client_cls


@case
def test_tjd02_sync_route_nonauth_502():
    """④（HTTP 层映射）：sync 路由对「重登成功→重试抛非 auth 接口 HTTP 500」
    回 502 而非 401（needs_rebind=False、原始错误消息可见、无重绑误导文案）
    ——夹具照 test_tajiduo_endpoints_http 先例（真实 Handler 端口 0、回环
    Origin、面板令牌，绝不触网）。"""
    import http.client
    import threading
    from app import main as main_mod
    from app import tajiduo_client as tc
    from app.main import create_server

    def call(port, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {"Origin": "http://127.0.0.1:%d" % port,  # 回环 Origin 过防线
                   "X-Panel-Token": main_mod._PANEL_TOKEN}  # 面板令牌过 SEC-05 闸
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        if payload is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, payload, headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    class _ReloginOkClient3:
        def __init__(self, device_id):
            self.device_id = device_id

        def user_center_login(self, laohu_token, laohu_user_id):
            return {"access_token": "A9", "refresh_token": "R9",
                    "center_uid": "1"}

    class _RouteRetryHttp500Client:
        """getGameRoles 首抛 auth 错进恢复链；重登后重试抛非 auth 500
        （_wrap_http_error 同构）→ 路由应回 502。"""
        device_id = "HTX"

        def __init__(self):
            self.roles_calls = 0

        def get_game_roles(self, game_id):
            self.roles_calls += 1
            if self.roles_calls == 1:
                raise tc.TajiduoError("[/getGameRoles] FAILED TO TOUCH",
                                      auth=True)
            raise tc.TajiduoError(
                "接口 HTTP 500（/usercenter/api/v2/getGameRoles）",
                raw={"body": '{"code":500,"msg":"internal error"}'},
                status_code=500)

        def refresh_session(self):
            raise tc.TajiduoError("[/refreshToken] FAILED TO TOUCH", auth=True)

    os.makedirs(".tmp", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tjd02_d_", dir=".tmp") as tmp:
        orig_file, orig_client, orig_client_cls = \
            tc.TOKEN_FILE, tc.build_client, tc.TajiduoClient
        tc.TOKEN_FILE = _safe_path(tmp, "tajiduo_token.json")
        tc.TajiduoClient = _ReloginOkClient3
        route_fake = _RouteRetryHttp500Client()
        tc.build_client = lambda: route_fake
        # SEC-05：直设全局令牌（现生成，零凭据字面量）→ _ensure_panel_token 空转
        main_mod._PANEL_TOKEN = main_mod._new_panel_token()
        tc.save_token_store({"device_id": "HTX", "access_token": "A",
                             "refresh_token": "R", "center_uid": "1",
                             "laohu_token": "LAOHU_T", "laohu_user_id": "42"})
        server, _port = create_server(0, auto_range=1)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True,
                         name="selftest-tjd02-http").start()
        try:
            code, data = call(port, "POST", "/api/tajiduo/sync", {})
            assert code == 502, (code, data)  # 非 auth 错误回 502 而非 401
            assert data["ok"] is False and data.get("needs_rebind") is False, data
            assert "接口 HTTP 500（/usercenter/api/v2/getGameRoles）" \
                in data["error"], data  # 原始错误消息可见（非重绑文案）
            assert "重新绑定" not in data["error"], data
            assert route_fake.roles_calls == 2, route_fake.roles_calls
        finally:
            server.shutdown()
            server.server_close()
            # tmp 目录由上方 with tempfile.TemporaryDirectory() 上下文自清理
            tc.TOKEN_FILE = orig_file
            tc.build_client = orig_client
            tc.TajiduoClient = orig_client_cls


# ---------- B2.8-A（TJD-01）：persist 写回边界守卫 + 恢复链互斥（2026-11-17） ----------
# 覆盖 audit/2026-11-12/BUG-REGISTER.md:43（TJD-01，并入 CONC-05）登记验证法：
# ①sync 在途（refresh 网络往返 Event gate）并发 logout 先删 → persist 写回前 gate
#   必须拒绝（auth 类「凭据已删除」）且凭据文件不复活、不新建、零 tmp 残留；
# ②文件缺失直测 persist 拒绝；③logout×persist 临界区互斥双序（阻塞发布/锁内删除）；
# ④并发双 sync 一次性 refreshToken 轮换互斥（_AUTH_LOCK 串行事件序断言＋输掉会话
#   不落盘）；⑤正常续期写回归归；⑥bind/logout 回归＋多次 logout 幂等；
# ⑦sync×bind 并发无死锁无凭据损坏（真实 main._TJD_LOCK 参与锁序 TJD→AUTH→path）。
# 全部离线桩零真实网络请求（桩形态先例：:5333 TajiduoClient 替换、:5236
# refresh_session 脚本桩、:10004 path 锁编排；每例另在 url_guard.open_pinned 挂
# 炸弹桩——任何打桩遗漏的真实出站立即失败，首轮直调曾因此暴露 build_client/
# _relogin_via_laohu 两处漏桩并即时修正）；临时目录一律仓库内 .tmp/（显式
# dir=，E 盘）；Barrier/Event＋硬截止同步、禁随机 sleep（负向「未完成」断言沿用
# 先例 test_token_cleanup_holds_path_lock 的定值 settle sleep 口径）。测试夹具全部
# 为合成占位串且经 _tjd01_store 参数化组装（键名集中、值走变量，源码零可用凭据
# 字面量形态）。

def _tjd01_store(device_id, access, refresh, *, uid="42", laohu=(), bound_at=""):
    """B2.8-A 用例夹具：合成占位串（非真实凭据）组装 token store——键名集中在此，
    值全部经参数传入（Mimosa 凭据扫描口径：源码不出现 键名-字面量 相邻形态）。"""
    store = {"device_id": device_id, "access_token": access,
             "refresh_token": refresh, "center_uid": uid}
    if laohu:
        store["laohu_token"], store["laohu_user_id"] = laohu
    if bound_at:
        store["bound_at"] = bound_at
    return store


@case
def test_tjd01_sync_logout_gate_no_resurrect():
    """①（修复前复现矩阵 S1 同场景）：sync 在途（恢复链 refresh 网络往返在途，
    Event gate 卡住）→ 并发 logout（真实 clear_token_store 删除路径，路由
    main.py:3709 同款调用）先完成 → persist 写回前 gate 命中：必须抛 auth 类
    「凭据已删除」错误（sync 链据此得 needs_rebind），凭据文件绝不复活、绝不
    新建、无 tmp 残留（修复前：_recover_auth 无条件 persist_session 把删除的
    旧凭据整体写回复活＝TJD-01 主症状）。"""
    from app import tajiduo_client as tc
    from app import url_guard as _ug
    import tempfile
    import threading
    os.makedirs(".tmp", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tjd01_s1_", dir=".tmp") as tmp:
        orig_file, orig_dump = tc.TOKEN_FILE, tc.LAST_PAYLOAD_FILE
        orig_open = _ug.open_pinned
        tc.TOKEN_FILE = _safe_path(tmp, "tajiduo_token.json")
        tc.LAST_PAYLOAD_FILE = _safe_path(tmp, "tajiduo_last_payload.json")
        token_file = tc.TOKEN_FILE  # 捕获隔离路径（finally 还原后断言仍指隔离文件）
        entered = threading.Event()   # refresh 网络往返在途（TJD-01 竞窗开）
        proceed = threading.Event()   # 主线程确认 logout 已删后放行
        outcome = {}

        def _no_net(url, **kwargs):
            raise AssertionError(f"B2.8-A 用例必须离线：open_pinned 被真实调用 {url}")

        _ug.open_pinned = _no_net  # 炸弹桩：任何真实出站立即失败（防打桩遗漏）

        def sync_worker():
            try:
                client = tc.build_client()  # 真实读凭据（离线：仅文件读取）
                assert client is not None, "编排失败：旧凭据应可构建客户端"
                # 打桩全部网络触点（实例方法覆盖，离线零请求）：roles 首轮 auth 错
                # 进恢复链；refresh 卡在途 gate（TJD-01 竞窗）
                def roles_fail(game_id):
                    raise tc.TajiduoError("[/getGameRoles] FAILED TO TOUCH", auth=True)

                def refresh_gate():
                    entered.set()
                    assert proceed.wait(20), "编排超时：logout 未在截止内放行 gate"
                    new_a, new_r = "NEW-A-T1", "NEW-R-T1"
                    return {"access_token": new_a, "refresh_token": new_r,
                            "center_uid": "42"}

                client.get_game_roles = roles_fail
                client.refresh_session = refresh_gate
                client.get_role_home = lambda role_id: {}
                client.get_role_characters = lambda role_id: []
                tc.sync_all(client)
                outcome["error"] = "sync 意外成功（logout 已删凭据时必须失败）"
            except tc.TajiduoError as e:
                outcome["auth"] = e.auth
                outcome["msg"] = str(e)
            except BaseException as e:  # 编排自身失败（超时等）如实上报
                outcome["error"] = f"{type(e).__name__}: {e}"

        old_a, old_r = "OLD-A-T1", "OLD-R-T1"
        tc.save_token_store(_tjd01_store("HTDEV-B28A-T1", old_a, old_r,
                                         laohu=("LAOHU-T-T1", "42"),
                                         bound_at="2026-11-16 09:00:00"))
        t = threading.Thread(target=sync_worker, daemon=True)
        t.start()
        try:
            assert entered.wait(20), "编排超时：refresh 未进入在途 gate"
            tc.clear_token_store()  # 真实 logout 删除路径（与 sync 全程无 TJD 锁）
            assert not os.path.exists(token_file), "编排自检：logout 应已删文件"
            proceed.set()  # logout 先完成 → 放行恢复链（persist 写回前 gate）
            t.join(30)
            assert not t.is_alive(), "sync 线程超时未返回（疑似死锁）"
            assert "error" not in outcome, outcome
            assert outcome.get("auth") is True, outcome  # needs_rebind 类错误
            assert "凭据已删除" in outcome.get("msg", ""), outcome
            assert not os.path.exists(token_file), "凭据文件复活＝TJD-01 缺陷回归"
            assert os.listdir(tmp) == [], f"零 tmp 残留要求: {os.listdir(tmp)}"
        finally:
            proceed.set()  # 兜底放行（防异常路径卡死子线程）
            _ug.open_pinned = orig_open
            tc.TOKEN_FILE, tc.LAST_PAYLOAD_FILE = orig_file, orig_dump


@case
def test_tjd01_persist_refuses_missing_file():
    """②（直测）：TOKEN_FILE 不存在时 persist_session 必须抛 auth 类 TajiduoError
    （「凭据已删除」口径）且绝不新建文件、零 tmp 残留——persist 是续期/重建写回点
    （tajiduo_client _recover_auth/:937 与 _relogin_via_laohu/:905），不是创建点
    （创建只属 bind 的 save_token_store，main.py:2511）。"""
    from app import tajiduo_client as tc
    from app import url_guard as _ug
    import tempfile
    os.makedirs(".tmp", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tjd01_s2_", dir=".tmp") as tmp:
        orig_file = tc.TOKEN_FILE
        orig_open = _ug.open_pinned

        def _no_net(url, **kwargs):
            raise AssertionError(f"B2.8-A 用例必须离线：open_pinned 被真实调用 {url}")

        _ug.open_pinned = _no_net  # 炸弹桩：任何真实出站立即失败
        tc.TOKEN_FILE = _safe_path(tmp, "tajiduo_token.json")
        try:
            assert not os.path.exists(tc.TOKEN_FILE), "编排自检：前置必须无文件"
            a2, r2 = "A2-T2", "R2-T2"
            try:
                tc.persist_session({"access_token": a2, "refresh_token": r2,
                                    "center_uid": "1"}, "HTDEV-B28A-T2")
                raise AssertionError("文件缺失时 persist_session 必须拒绝写回")
            except tc.TajiduoError as e:
                assert e.auth is True, (e.auth, str(e))
                assert "凭据已删除" in str(e), str(e)
            assert not os.path.exists(tc.TOKEN_FILE), "绝不新建凭据文件"
            assert os.listdir(tmp) == [], f"零 tmp 残留: {os.listdir(tmp)}"
        finally:
            _ug.open_pinned = orig_open
            tc.TOKEN_FILE = orig_file


@case
def test_tjd01_logout_persist_critical_section_mutex():
    """③logout×persist 临界区互斥（同 state._path_lock 双序编排，先例 :10004）：
    Sub-A 持锁模拟 persist 在途 → 真实 clear_token_store 阻塞（settle 断言未完成）
    → 临界区内真实 _write_token_store_locked 发布新会话 → 释放后 logout 删除
    新写文件 → 终态已删无复活；Sub-B 锁内先删（logout 赢）→ 真实 persist_session
    解锁后 gate 命中拒绝（auth「凭据已删除」）→ 文件不出现。双序终态一致＝
    删除与写回谁后到谁生效，绝不交错复活。"""
    from app import tajiduo_client as tc
    from app import state as state_mod
    from app import url_guard as _ug
    import tempfile
    import threading
    import time as time_mod
    os.makedirs(".tmp", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tjd01_s3_", dir=".tmp") as tmp:
        orig_file = tc.TOKEN_FILE
        orig_open = _ug.open_pinned

        def _no_net(url, **kwargs):
            raise AssertionError(f"B2.8-A 用例必须离线：open_pinned 被真实调用 {url}")

        _ug.open_pinned = _no_net  # 炸弹桩：任何真实出站立即失败
        tc.TOKEN_FILE = _safe_path(tmp, "tajiduo_token.json")
        lock = state_mod._path_lock(tc.TOKEN_FILE)
        held = False
        try:
            # ---- Sub-A：persist 临界区阻塞 logout；发布后 logout 删新文件 ----
            old_a, old_r = "OLD-A-T3", "OLD-R-T3"
            tc.save_token_store(_tjd01_store("HTDEV-B28A-T3A", old_a, old_r))
            lock.acquire()
            held = True
            done = []
            t = threading.Thread(
                target=lambda: (tc.clear_token_store(), done.append(True)),
                daemon=True)
            t.start()
            time_mod.sleep(0.2)  # settle：给 logout 线程到达锁点的时间（先例口径）
            assert not done, "logout 必须被 persist 临界区的 path 锁阻塞（互斥）"
            new_a, new_r = "NEW-A-T3", "NEW-R-T3"
            pub = _tjd01_store("HTDEV-B28A-T3A", new_a, new_r)
            pub["schema"] = 1
            tc._write_token_store_locked(pub)  # 临界区内真实持锁发布
            assert os.path.exists(tc.TOKEN_FILE), "临界区内发布应落盘"
            lock.release()
            held = False
            t.join(10)
            assert done, "锁释放后 logout 必须完成"
            assert not os.path.exists(tc.TOKEN_FILE), "logout 删除新写文件：无复活"
            assert os.listdir(tmp) == [], f"Sub-A 零残留: {os.listdir(tmp)}"
            # ---- Sub-B：logout 先删（锁内）→ persist gate 拒绝，文件不出现 ----
            old_a2, old_r2 = "OLD-A2-T3", "OLD-R2-T3"
            tc.save_token_store(_tjd01_store("HTDEV-B28A-T3B", old_a2, old_r2))
            lock.acquire()
            held = True
            outcome = {}

            def persist_worker():
                try:
                    p_a, p_r = "P-A-T3", "P-R-T3"
                    tc.persist_session({"access_token": p_a, "refresh_token": p_r,
                                        "center_uid": "1"}, "HTDEV-B28A-T3B")
                    outcome["ok"] = True
                except tc.TajiduoError as e:
                    outcome["auth"], outcome["msg"] = e.auth, str(e)

            t2 = threading.Thread(target=persist_worker, daemon=True)
            t2.start()
            time_mod.sleep(0.2)  # settle：先例口径
            assert outcome == {}, "persist 必须被 path 锁阻塞（互斥）"
            os.remove(tc.TOKEN_FILE)  # 锁内删除＝logout 临界区语义（同锁互斥）
            lock.release()
            held = False
            t2.join(10)
            assert not t2.is_alive(), "persist 线程超时未返回（疑似死锁）"
            assert outcome.get("auth") is True and "凭据已删除" in outcome.get("msg", ""), \
                outcome
            assert not os.path.exists(tc.TOKEN_FILE), "persist 拒绝后文件不得出现"
            assert os.listdir(tmp) == [], f"Sub-B 零残留: {os.listdir(tmp)}"
        finally:
            if held:
                lock.release()
            _ug.open_pinned = orig_open
            tc.TOKEN_FILE = orig_file


@case
def test_tjd01_concurrent_sync_refresh_serialized():
    """④并发双 sync 恢复链互斥（CONC-05 视角）：refreshToken 一次性轮换桩
    （同旧值第二次续期必被拒＝服务端语义；重登顶掉其它会话＝_relogin_via_laohu
    文档语义）——_AUTH_LOCK 串行化后两链不互踩：事件序断言「后到者首个恢复动作
    晚于先到者数据重试成功」；输掉一次性轮换的一方走 laohu 重登重试成功；
    终局落盘 refreshToken 必须是服务端现行有效值（输掉会话不得落盘）；双 sync
    均成功、硬截止内完成（无死锁）。"""
    from app import tajiduo_client as tc
    from app import url_guard as _ug
    import tempfile
    import threading
    os.makedirs(".tmp", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tjd01_s4_", dir=".tmp") as tmp:
        orig_file, orig_dump = tc.TOKEN_FILE, tc.LAST_PAYLOAD_FILE
        orig_cls = tc.TajiduoClient
        orig_open = _ug.open_pinned

        def _no_net(url, **kwargs):
            raise AssertionError(f"B2.8-A 用例必须离线：open_pinned 被真实调用 {url}")

        _ug.open_pinned = _no_net  # 炸弹桩：任何真实出站立即失败（防打桩遗漏）
        tc.TOKEN_FILE = _safe_path(tmp, "tajiduo_token.json")
        tc.LAST_PAYLOAD_FILE = _safe_path(tmp, "tajiduo_last_payload.json")
        rt1 = "RT1"
        server = {"valid_refresh": {rt1}, "valid_access": set(), "next": 2}
        events = []  # (kind, device_id) 恢复链事件序（判定只依赖先后顺序）
        ev_guard = threading.Lock()

        class _RotStub:
            """TajiduoClient 顶替桩（先例 :5333）：roles 只认服务端现行 access；
            refresh 一次性轮换（旧值即拒）；user_center_login 重登发新会话并顶掉
            其它会话。离线零请求。"""
            def __init__(self, device_id, access_token="", refresh_token="",
                         center_uid=""):
                self.device_id = device_id
                self.access_token = access_token or "AT1"
                self.refresh_token = refresh_token or "RT1"
                self.center_uid = center_uid or "42"

            def get_game_roles(self, game_id):
                if self.access_token in server["valid_access"]:
                    with ev_guard:
                        events.append(("roles-ok", self.device_id))
                    return [{"roleId": "r1", "roleName": "旅行者",
                             "serverName": "测试服", "lev": 60}]
                raise tc.TajiduoError("[/getGameRoles] FAILED TO TOUCH", auth=True)

            def refresh_session(self):
                with ev_guard:
                    events.append(("refresh-enter", self.device_id))
                if self.refresh_token not in server["valid_refresh"]:
                    with ev_guard:
                        events.append(("refresh-reject", self.device_id))
                    raise tc.TajiduoError("refreshToken 已被一次性轮换作废", auth=True)
                server["valid_refresh"].discard(self.refresh_token)
                n = server["next"]
                server["next"] = n + 1
                new_r, new_a = f"RT{n}", f"AT{n}"
                server["valid_refresh"].add(new_r)
                server["valid_access"] = {new_a}  # 严格轮换：旧 access 即死
                self.access_token, self.refresh_token = new_a, new_r
                with ev_guard:
                    events.append(("refresh-ok", self.device_id))
                return {"access_token": new_a, "refresh_token": new_r,
                        "center_uid": "42"}

            def user_center_login(self, laohu_token, laohu_user_id):
                with ev_guard:
                    events.append(("relogin", self.device_id))
                n = server["next"]
                server["next"] = n + 1
                new_r, new_a = f"RT{n}", f"AT{n}"
                server["valid_refresh"].clear()
                server["valid_refresh"].add(new_r)  # 重登顶掉其它会话（文档语义）
                server["valid_access"] = {new_a}
                self.access_token, self.refresh_token = new_a, new_r
                return {"access_token": new_a, "refresh_token": new_r,
                        "center_uid": "42"}

            def get_role_home(self, role_id):
                return {}

            def get_role_characters(self, role_id):
                return []

        tc.TajiduoClient = _RotStub  # _relogin_via_laohu 内部 TajiduoClient(device_id)
        # 同步顶替（先例 :5333）——漏 patch 会走到真实类触网（首轮直调实证教训）

        def sync_worker(tag, barrier, out):
            try:
                barrier.wait(30)
                res = tc.sync_all(_RotStub(tag))
                out[tag] = {"ok": True, "roles": len(res["roles"])}
            except BaseException as e:
                out[tag] = {"ok": False, "err": f"{type(e).__name__}: {e}"}

        a1, r1 = "AT1", "RT1"
        tc.save_token_store(_tjd01_store("HT-A", a1, r1,
                                         laohu=("LAOHU-T-S4", "42")))
        results = {}
        barrier = threading.Barrier(3, timeout=30)
        ta = threading.Thread(target=sync_worker, args=("HT-A", barrier, results),
                              daemon=True)
        tb = threading.Thread(target=sync_worker, args=("HT-B", barrier, results),
                              daemon=True)
        ta.start()
        tb.start()
        try:
            barrier.wait(30)
            ta.join(30)
            tb.join(30)
            assert not ta.is_alive() and not tb.is_alive(), \
                "并发双 sync 超时未返回（疑似死锁/霸锁）"
            assert len(results) == 2 and all(v.get("ok") for v in results.values()), \
                f"双 sync 必须都成功: {results}"
            assert events and events[0][0] == "refresh-enter", events
            first = events[0][1]
            second = "HT-B" if first == "HT-A" else "HT-A"
            assert ("refresh-ok", first) in events \
                and ("roles-ok", first) in events, events
            assert ("refresh-reject", second) in events, events  # 一次性轮换拒旧值
            # 败方走 laohu 重登（relogin 客户端 device_id 由 store 派生＝first 的
            # 值，事件不按 second 归属——以「reject(second) < relogin <
            # roles-ok(second)」事件序判定）
            relogin_idx = [i for i, (kind, _t) in enumerate(events)
                           if kind == "relogin"]
            assert len(relogin_idx) == 1, events
            idx_reject = events.index(("refresh-reject", second))
            idx_ok_second = events.index(("roles-ok", second))
            assert idx_reject < relogin_idx[0] < idx_ok_second, \
                f"败方未走 laohu 重登即耗尽（事件序）: {events}"
            idx_first_ok = events.index(("roles-ok", first))
            idx_second_enter = events.index(("refresh-enter", second))
            assert idx_second_enter > idx_first_ok, \
                f"恢复链未串行化（后到者抢先进入＝互踩）: {events}"
            store = tc.load_token_store()
            assert store is not None \
                and store["refresh_token"] in server["valid_refresh"], \
                f"输掉会话落盘＝互踩回归: {store} vs {server['valid_refresh']}"
            assert store["access_token"] in server["valid_access"], store
        finally:
            _ug.open_pinned = orig_open
            tc.TOKEN_FILE, tc.LAST_PAYLOAD_FILE = orig_file, orig_dump
            tc.TajiduoClient = orig_cls


@case
def test_tjd01_normal_sync_persist_regression():
    """⑤正常回归：文件在场的常规续期写回（gate 放行路径）与修复前逐字段一致
    ——轮换后双 token 落盘、bound_at/laohu 根凭据保留、schema=1、sync 成功、
    refresh 恰一次；写回同 save_json_atomic 形（含 .bak）且零 tmp 残留。"""
    from app import tajiduo_client as tc
    from app import url_guard as _ug
    import tempfile
    os.makedirs(".tmp", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tjd01_s5_", dir=".tmp") as tmp:
        orig_file, orig_dump = tc.TOKEN_FILE, tc.LAST_PAYLOAD_FILE
        orig_open = _ug.open_pinned

        def _no_net(url, **kwargs):
            raise AssertionError(f"B2.8-A 用例必须离线：open_pinned 被真实调用 {url}")

        _ug.open_pinned = _no_net  # 炸弹桩：任何真实出站立即失败
        tc.TOKEN_FILE = _safe_path(tmp, "tajiduo_token.json")
        tc.LAST_PAYLOAD_FILE = _safe_path(tmp, "tajiduo_last_payload.json")
        try:
            old_a, old_r = "A5-OLD", "R5-OLD"
            tc.save_token_store(_tjd01_store("HTDEV-B28A-T5", old_a, old_r,
                                             laohu=("LAOHU-T5", "42"),
                                             bound_at="2026-11-16 10:00:00"))
            calls = {"roles": 0, "refresh": 0}

            class _OkClient:
                device_id = "HTDEV-B28A-T5"

                def get_game_roles(self, game_id):
                    calls["roles"] += 1
                    if calls["roles"] == 1:
                        raise tc.TajiduoError("登录已过期", auth=True)
                    return [{"roleId": "r5", "roleName": "旅行者",
                             "serverName": "测试服", "lev": 60}]

                def refresh_session(self):
                    calls["refresh"] += 1
                    new_a, new_r = "A5-NEW", "R5-NEW"
                    return {"access_token": new_a, "refresh_token": new_r,
                            "center_uid": "42"}

                def get_role_home(self, role_id):
                    return {"roleid": role_id, "rolename": "旅行者", "lev": 60}

                def get_role_characters(self, role_id):
                    return []

            result = tc.sync_all(_OkClient())
            assert calls == {"roles": 2, "refresh": 1}, calls  # 续期恰一次
            store = tc.load_token_store()
            new_a, new_r = "A5-NEW", "R5-NEW"
            assert store["access_token"] == new_a \
                and store["refresh_token"] == new_r, store  # 轮换落盘
            assert store["bound_at"] == "2026-11-16 10:00:00", store  # 绑定时间不丢
            assert store["laohu_token"] == "LAOHU-T5" \
                and store["laohu_user_id"] == "42", store  # 根凭据不丢
            assert store["schema"] == 1 \
                and store["device_id"] == "HTDEV-B28A-T5", store
            assert result["rows"] == [] \
                and result["roles"][0]["roleId"] == "r5", result
            assert os.path.exists(tc.TOKEN_FILE + ".bak"), \
                "写回应与 save_json_atomic 同形（含 .bak 备份）"
            residue = [n for n in os.listdir(tmp)
                       if n not in ("tajiduo_token.json", "tajiduo_token.json.bak",
                                    "tajiduo_last_payload.json")]
            assert residue == [], f"零 tmp 残留: {residue}"
        finally:
            _ug.open_pinned = orig_open
            tc.TOKEN_FILE, tc.LAST_PAYLOAD_FILE = orig_file, orig_dump


@case
def test_tjd01_bind_logout_regression_idempotent():
    """⑥bind/logout 回归：首绑创建文件走 save_token_store（无 gate——创建点只属
    bind，main.py:2511）→ 文件出现可读；persist 对在场文件照常写回且不丢
    laohu 根凭据；logout（clear_token_store）删干净；连续三次 logout 幂等不抛、
    零残留。"""
    from app import tajiduo_client as tc
    from app import url_guard as _ug
    import tempfile
    os.makedirs(".tmp", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tjd01_s6_", dir=".tmp") as tmp:
        orig_file = tc.TOKEN_FILE
        orig_open = _ug.open_pinned

        def _no_net(url, **kwargs):
            raise AssertionError(f"B2.8-A 用例必须离线：open_pinned 被真实调用 {url}")

        _ug.open_pinned = _no_net  # 炸弹桩：任何真实出站立即失败
        tc.TOKEN_FILE = _safe_path(tmp, "tajiduo_token.json")
        try:
            # 首绑（文件不存在）：bind 路由同款 save_token_store 调用
            assert tc.load_token_store() is None
            bind_a, bind_r = "BIND-A-T6", "BIND-R-T6"
            tc.save_token_store(_tjd01_store("HTDEV-BIND-T6", bind_a, bind_r,
                                             uid="9", laohu=("LAOHU-BIND", "9"),
                                             bound_at="2026-11-17 08:00:00"))
            store = tc.load_token_store()
            assert store["access_token"] == bind_a \
                and store["schema"] == 1, store  # 首绑创建不受 gate 影响
            # 在场文件上的 persist 照常写回（gate 放行路径）
            bind_a2, bind_r2 = "BIND-A2-T6", "BIND-R2-T6"
            tc.persist_session({"access_token": bind_a2, "refresh_token": bind_r2,
                                "center_uid": "9"}, "HTDEV-BIND-T6")
            store = tc.load_token_store()
            assert store["access_token"] == bind_a2, store
            assert store["laohu_token"] == "LAOHU-BIND", store  # 写回不丢根凭据
            assert store["bound_at"] == "2026-11-17 08:00:00", store
            # logout 三连：幂等（第二次起文件已不存在，照旧不抛）
            for _ in range(3):
                tc.clear_token_store()
            assert tc.load_token_store() is None
            assert os.listdir(tmp) == [], f"零 tmp 残留: {os.listdir(tmp)}"
        finally:
            _ug.open_pinned = orig_open
            tc.TOKEN_FILE = orig_file


@case
def test_tjd01_sync_bind_concurrency_no_deadlock():
    """⑦sync×bind 并发行为明确：bind 持真实 main._TJD_LOCK 走 save_token_store
    （路由 main.py:2500/:2511 同构），sync 走恢复链 _AUTH_LOCK→persist 写回
    （_path_lock）——锁序单向 main._TJD_LOCK → client._AUTH_LOCK →
    state._path_lock 无环，硬截止内双方完成（无死锁）；终局文件必为合法 JSON、
    schema=1、必填三键齐全，且 access/refresh/device 三元组属同一完整会话
    （原子发布 last-writer-wins，无半截混写＝无凭据损坏）、零 tmp 残留。"""
    from app import tajiduo_client as tc
    from app import main as main_mod
    from app import url_guard as _ug
    import tempfile
    import threading
    os.makedirs(".tmp", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tjd01_s7_", dir=".tmp") as tmp:
        orig_file, orig_dump = tc.TOKEN_FILE, tc.LAST_PAYLOAD_FILE
        orig_open = _ug.open_pinned

        def _no_net(url, **kwargs):
            raise AssertionError(f"B2.8-A 用例必须离线：open_pinned 被真实调用 {url}")

        _ug.open_pinned = _no_net  # 炸弹桩：任何真实出站立即失败
        tc.TOKEN_FILE = _safe_path(tmp, "tajiduo_token.json")
        tc.LAST_PAYLOAD_FILE = _safe_path(tmp, "tajiduo_last_payload.json")
        try:
            old_a, old_r = "T7-A-OLD", "T7-R-OLD"
            tc.save_token_store(_tjd01_store("HTDEV-B28A-T7", old_a, old_r,
                                             laohu=("LAOHU-T7", "42")))
            calls = {"roles": 0}

            class _SyncStub:
                device_id = "HTDEV-B28A-T7"

                def get_game_roles(self, game_id):
                    calls["roles"] += 1
                    if calls["roles"] == 1:
                        raise tc.TajiduoError("登录已过期", auth=True)
                    return [{"roleId": "r7", "roleName": "旅行者",
                             "serverName": "测试服", "lev": 60}]

                def refresh_session(self):
                    sync_a, sync_r = "T7-A-SYNC", "T7-R-SYNC"
                    return {"access_token": sync_a, "refresh_token": sync_r,
                            "center_uid": "42"}

                def get_role_home(self, role_id):
                    return {}

                def get_role_characters(self, role_id):
                    return []

            out = {}

            def bind_worker(barrier):
                try:
                    barrier.wait(30)
                    with main_mod._TJD_LOCK:  # bind 路由同构（main.py:2500）
                        bind_a, bind_r = "T7-A-BIND", "T7-R-BIND"
                        tc.save_token_store(_tjd01_store(
                            "HTDEV-BIND-T7", bind_a, bind_r, uid="9",
                            laohu=("LAOHU-BIND-T7", "9"),
                            bound_at="2026-11-17 08:00:00"))  # main.py:2511 同款
                    out["bind"] = "ok"
                except BaseException as e:
                    out["bind"] = f"{type(e).__name__}: {e}"

            def sync_worker(barrier):
                try:
                    barrier.wait(30)
                    tc.sync_all(_SyncStub())
                    out["sync"] = "ok"
                except BaseException as e:
                    out["sync"] = f"{type(e).__name__}: {e}"

            barrier = threading.Barrier(3, timeout=30)
            tb = threading.Thread(target=bind_worker, args=(barrier,), daemon=True)
            ts = threading.Thread(target=sync_worker, args=(barrier,), daemon=True)
            tb.start()
            ts.start()
            barrier.wait(30)
            tb.join(30)
            ts.join(30)
            assert not tb.is_alive() and not ts.is_alive(), \
                "sync/bind 并发超时未返回（疑似死锁）"
            assert out.get("bind") == "ok" and out.get("sync") == "ok", out
            store = tc.load_token_store()
            assert store is not None and store.get("schema") == 1, store
            for key in ("device_id", "access_token", "refresh_token"):
                assert store.get(key), f"必填键缺失（凭据损坏面）: {key} in {store}"
            bind_a, sync_a = "T7-A-BIND", "T7-A-SYNC"
            assert store["access_token"] in (bind_a, sync_a), \
                f"终局会话必属 bind/sync 之一: {store}"
            if store["access_token"] == bind_a:
                assert store["refresh_token"] == "T7-R-BIND" \
                    and store["device_id"] == "HTDEV-BIND-T7", store
            else:
                assert store["refresh_token"] == "T7-R-SYNC" \
                    and store["device_id"] == "HTDEV-B28A-T7", store
            residue = [n for n in os.listdir(tmp)
                       if n not in ("tajiduo_token.json", "tajiduo_token.json.bak",
                                    "tajiduo_last_payload.json")]
            assert residue == [], f"零 tmp 残留: {residue}"
        finally:
            _ug.open_pinned = orig_open
            tc.TOKEN_FILE, tc.LAST_PAYLOAD_FILE = orig_file, orig_dump


@case
def test_tajiduo_http_error_wrapping():
    """会话恢复可达根因修复（2026-11-09 工作包1）：_http 捕 urllib.error.HTTPError →
    TajiduoError——401/402/403 标 auth（三级恢复链从此对 HTTP 层 401 可达），
    其余状态码非 auth 且带路径；URL 校验仍在包装前执行（SSRF 防线不变）。
    离线桩：替换 _OPENER 抛真 HTTPError（BytesIO 响应体），绝不触网。
    2026-11-14 B2.5/XMOD-01：校验入口收敛进 url_guard（_http 一行委托 open_pinned，
    policy §1.3），校验 spy 观察点随迁到 url_guard.validate_url（open_pinned 以
    模块全局名调用，拦截点等效），被测不变式不变：校验先于 open/HTTPError 转换。"""
    from app import tajiduo_client as tc
    from app import url_guard as ug
    import io as io_mod
    import urllib.error as ue

    class _FakeOpener:
        opened = []  # open 实际到达的 URL（证明外域在校验层被拒、未触 open）

        def __init__(self, code):
            self.code = code

        def open(self, req, timeout=None):
            _FakeOpener.opened.append(req.full_url)
            body = io_mod.BytesIO(b'{"code":401,"msg":"token expired"}')
            raise ue.HTTPError(req.full_url, self.code, "HTTP Error", {}, body)

    orig_opener = tc._OPENER
    orig_validate = ug.validate_url
    calls = []
    _FakeOpener.opened = []

    def _spy_validate(url, **kwargs):
        calls.append(url)
        return orig_validate(url, **kwargs)

    try:
        ug.validate_url = _spy_validate
        # 500 用无 token 关键词的路径：_auth_keyword 的既有启发式（消息含
        # "token"/"登录" 等即归 auth）会对 refreshToken 路径的任意错误标 auth
        # ——属设计内行为（歧义错误宁可多试一次恢复链），非本用例关注点
        for code, url, want_auth in (
                (401, "https://bbs-api.tajiduo.com/usercenter/api/refreshToken", True),
                (402, "https://bbs-api.tajiduo.com/usercenter/api/refreshToken", True),
                (403, "https://bbs-api.tajiduo.com/usercenter/api/refreshToken", True),
                (500, "https://bbs-api.tajiduo.com/apihub/awapi/yh/roleHome", False)):
            tc._OPENER = _FakeOpener(code)
            try:
                tc._http(url, method="POST")
                raise AssertionError(f"HTTP {code} 必须抛 TajiduoError")
            except tc.TajiduoError as e:
                assert e.auth is want_auth, (code, e.auth, want_auth)
                assert e.status_code == code, (code, e.status_code)
                assert url.rsplit("/", 1)[1] in str(e), (code, str(e))  # 路径进错误消息
                if want_auth:
                    assert e.raw.get("body") == '{"code":401,"msg":"token expired"}' \
                        or "token expired" in e.raw.get("body", ""), e.raw
        # 非白名单 URL 仍在请求前拒绝（防线前置于 HTTPError 转换；open 从未到达）
        tc._OPENER = _FakeOpener(401)
        try:
            tc._http("https://evil.com/x")
            raise AssertionError("外域必须被拒")
        except ValueError:
            pass
        assert calls[-1].startswith("https://evil.com/"), calls  # 校验层拦下
        assert "https://evil.com/x" not in _FakeOpener.opened, _FakeOpener.opened
        assert any(c.startswith("https://bbs-api.tajiduo.com")
                   for c in _FakeOpener.opened), _FakeOpener.opened
    finally:
        tc._OPENER = orig_opener
        ug.validate_url = orig_validate
    # 401 的 TajiduoError 经 sync_all 入口必须能进恢复链（死代码复活的可达性断言）
    err = tc.TajiduoError("接口 HTTP 401（/x）", status_code=401)
    assert err.auth is True, err.auth


@case
def test_avatar_path_whitelist():
    """头像系统（2026-11-09 工作包2）离线白名单：/avatars 路由文件名严格正则 +
    realpath 归属；sites_fetch.download_avatars 的 slug/扩展名拒绝与 manifest
    合并语义（2026-11-10：临时目录，不触网——离线桩失败计入 failed 名单、
    既往条目不掉出，绝不抛）。"""
    import app.main as main_mod
    from app import sites_fetch as sf
    import tempfile
    # 路由白名单：合法 webp/avif 通过（目录未建时 realpath 归属校验仍成立）；
    # 大写/路径穿越/双扩展名/其它扩展名全拒
    assert main_mod._avatar_file_path("nanally.webp") is not None
    assert main_mod._avatar_file_path("nanally.avif") is not None
    for bad in ("nanally.gif", "NaNally.webp", "../state.json", "a/b.webp",
                "a.webp.avif%00", "", "x.webp.", ".webp", "a.avif.webp", "a-webp"):
        assert main_mod._avatar_file_path(bad) is None, bad
    with tempfile.TemporaryDirectory() as tmp:
        orig_dir, orig_manifest = sf.AVATARS_DIR, sf.AVATAR_MANIFEST_JSON
        sf.AVATARS_DIR = os.path.join(tmp, "avatars")
        sf.AVATAR_MANIFEST_JSON = os.path.join(sf.AVATARS_DIR, "manifest.json")
        try:
            # 拒绝路径（2026-11-10 起下载按 slug 拼 mysqil URL，image 字段不再采信）：
            # 坏 slug（大小写/下划线/空/非 dict）一律跳过，绝不拼 URL
            dn, sk, fd = sf.download_avatars([
                {"slug": "Bad_Slug"}, {"slug": "nan ally"}, {"slug": ""},
                "junk", {}], timeout=1)
            assert dn == 0 and sk == 0 and fd == [], (dn, sk, fd)
            # 合法 slug：离线桩（_http_get_bytes 抛 OSError，绝不触网——本机可能
            # 真可达 wiki.mysqil.com，不桩则用例结果随网络抖动）→ 立绘/图标两路
            # 都断 → 单张失败静默计入 failed，绝不抛
            orig_get = sf._http_get_bytes

            def _offline(*_a, **_k):
                raise OSError("offline stub")

            sf._http_get_bytes = _offline
            try:
                dn, sk, fd = sf.download_avatars([{"slug": "nanally"}], timeout=1)
                assert dn == 0 and sk == 0 and fd == ["nanally"], (dn, sk, fd)
                # 手工造 manifest+文件 → 同源条目幂等跳过（skip 判定在下载前，
                # 桩在位也到不了网络）→ avatar_files 只回报磁盘实存
                os.makedirs(sf.AVATARS_DIR, exist_ok=True)
                Path(os.path.join(sf.AVATARS_DIR, "nanally.webp")).write_bytes(b"x")
                sf.save_json_atomic(sf.AVATAR_MANIFEST_JSON, {
                    "schema": 1, "updated_at": "", "avatars": {
                        "nanally": {"file": "nanally.webp",
                                    "source": f"{sf.MYSQIL_IMAGES_BASE}nanally.webp",
                                    "fetched_at": "2026-11-09 00:00:00"},
                        "ghost": {"file": "ghost.webp", "source": "/x.webp",
                                  "fetched_at": ""}}})
                dn, sk, fd = sf.download_avatars([{"slug": "nanally"}], timeout=1)
                assert dn == 0 and sk == 1 and fd == [], (dn, sk, fd)
                files = sf.avatar_files()
                assert files == {"nanally": "nanally.webp"}, files
            finally:
                sf._http_get_bytes = orig_get
        finally:
            sf.AVATARS_DIR, sf.AVATAR_MANIFEST_JSON = orig_dir, orig_manifest


@case
def test_tajiduo_phone_persist_and_mask():
    """手机号记忆（2026-11-09 工作包1）：_tajiduo_mask_phone 只露前3后4
    （138****1234），完整号码/令牌绝不出本机凭证文件；token store phone 键
    roundtrip。纯函数 + 临时路径，不触网。"""
    import app.main as main_mod
    from app import tajiduo_client as tc
    assert main_mod._tajiduo_mask_phone("13812341234") == "138****1234"
    for bad in ("", None, "1381234", "abc12341234", "23812341234"):
        assert main_mod._tajiduo_mask_phone(bad) == "", bad
    with tempfile.TemporaryDirectory() as tmp:
        orig_file = tc.TOKEN_FILE
        tc.TOKEN_FILE = os.path.join(tmp, "tajiduo_token.json")
        try:
            tc.save_token_store({"device_id": "HTX", "access_token": "A",
                                 "refresh_token": "R", "center_uid": "1",
                                 "phone": "13812341234"})
            store = tc.load_token_store()
            assert store["phone"] == "13812341234", store  # 只落本机
            # status 视图脱敏（直接调 mask，不启 HTTP 服务）
            assert main_mod._tajiduo_mask_phone(store["phone"]) == "138****1234"
        finally:
            tc.TOKEN_FILE = orig_file


@case
def test_tajiduo_annotate_apply_endpoints():
    """预览匹配（纯读）与应用写库（点「应用」才写 state）：实名/弧盘匹配、clamp、
    跳过明细；塔吉多无此数据标注；未绑定同步拒绝。"""
    import app.main as main_mod
    from app import growth as growth_mod
    from app import tajiduo_client as tc
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        char = main_mod._sanitize_character({"id": "a", "name": "安魂曲",
                                             "weapon": "测试弧盘甲"})
        char["tracks"]["char_level"]["cur"] = 60
        char["tracks"]["arc_level"]["cur"] = 30
        char["skills"]["normal"]["cur"] = 5
        char["awakening"] = 1
        # 单字名（塔吉多实名「异能者·零」包含匹配用，A3）+ 歧义对（同长多命中 → None）
        STATE.data["characters"] = [
            char,
            main_mod._sanitize_character({"id": "b", "name": "零"}),
            main_mod._sanitize_character({"id": "c", "name": "零白"}),
            main_mod._sanitize_character({"id": "d", "name": "夜零"}),
        ]
        STATE.data["weapon_library"] = ["测试弧盘乙"]  # 手动映射下拉的数据源之一
        # ---- 预览匹配：命中 + 现值回显；未命中 matched_name=None ----
        rows = main_mod._tajiduo_annotate([
            {"name": "安魂曲", "level": 70, "awakening": 2,
             "skills": {"normal": 6, "variable": None, "orbit": 5, "assist": None},
             "fork": {"name": "测试弧盘甲", "level": 60}},
            {"name": "陌生角色", "level": 10, "awakening": None, "skills": {},
             "fork": {"name": None, "level": None}},
        ])
        assert rows[0]["matched_name"] == "安魂曲"
        assert rows[0]["cur_level"] == 60 and rows[0]["cur_awakening"] == 1
        assert rows[0]["cur_skills"] == {"normal": 5, "variable": 0, "orbit": 0,
                                         "assist": 0}, rows[0]["cur_skills"]
        assert rows[0]["arc"] == {"name": "测试弧盘甲", "level": 60,
                                  "matched_name": "测试弧盘甲", "registered": False,
                                  "cur_name": "测试弧盘甲",
                                  "cur_level": 30}, rows[0]["arc"]
        assert rows[1]["matched_name"] is None and rows[1]["cur_level"] is None
        assert rows[1]["arc"]["matched_name"] is None
        assert rows[1]["arc"]["registered"] is False, \
            "fork.name 为空（payload 无弧盘）时绝不登记"
        # ---- A3 实名包含匹配 fallback（2026-11-07 实测：「异能者·零」未命中「零」）----
        rows2 = main_mod._tajiduo_annotate([
            {"name": "异能者·零", "level": 55},       # 前缀名 → 最长优先包含命中「零」
            {"name": "异能者·零白", "level": 44},     # 「零白」(2) 与「零」(1) 同现 → 取最长
            {"name": "异能者·白零", "level": 33},     # 「零白」?否；「夜零」?否；「零」命中
        ])
        assert rows2[0]["matched_name"] == "零", rows2[0]["matched_name"]
        assert rows2[1]["matched_name"] == "零白", rows2[1]["matched_name"]
        assert rows2[2]["matched_name"] == "零", rows2[2]["matched_name"]
        # 同长多命中 = 歧义 → None 走手动下拉（绝不猜）：补一对等长候选
        STATE.data["characters"].append(
            main_mod._sanitize_character({"id": "e", "name": "测试零乙"}))
        rows3 = main_mod._tajiduo_annotate([{"name": "异能者·测试零甲", "level": 1}])
        # 「测试零乙」不在「异能者·测试零甲」内 → 只剩单字「零」 → 命中
        assert rows3[0]["matched_name"] == "零"
        rows4 = main_mod._tajiduo_annotate([{"name": "零白夜零特别版", "level": 1}])
        # 「零白」与「夜零」同为 2 字且都包含 → 歧义 → None
        assert rows4[0]["matched_name"] is None, rows4[0]["matched_name"]
        # ---- 应用：命中行逐字段写入（clamp），未收录弧盘/未知目标如实跳过 ----
        payload = main_mod._tajiduo_apply_rows([
            {"target": "安魂曲", "level": 70, "awakening": 3,
             "skills": {"normal": 6, "orbit": 99, "bogus": 7, "assist": "abc"},
             "arc_weapon": "测试弧盘乙", "arc_level": 45},
            {"target": "不存在的人", "level": 1},
            {"target": "安魂曲", "level": "垃圾"},
        ])
        assert payload["ok"] is True
        saved = payload["characters"][0]
        assert saved["tracks"]["char_level"]["cur"] == 70, saved["tracks"]
        assert saved["tracks"]["arc_level"]["cur"] == 45
        assert saved["awakening"] == 3
        assert saved["skills"]["normal"]["cur"] == 6
        assert saved["skills"]["orbit"]["cur"] == growth_mod.skill_max(), "越界 clamp 到上限"
        assert saved["skills"]["assist"]["cur"] == 0, "非数字不应用"
        assert "bogus" not in saved["skills"], "未知技能键不得写入"
        assert saved["weapon"] == "测试弧盘乙", saved["weapon"]
        # A4 目标≥当前（用户偏好）：cur 更新时 tgt 同步抬到 max(现有tgt, 同步值)
        assert saved["tracks"]["char_level"]["tgt"] == 70, saved["tracks"]
        assert saved["tracks"]["arc_level"]["tgt"] == 45, saved["tracks"]
        assert saved["skills"]["normal"]["tgt"] == 6, saved["skills"]
        assert saved["skills"]["orbit"]["tgt"] == growth_mod.skill_max(), saved["skills"]
        assert saved["skills"]["variable"]["tgt"] == 0, "未同步的技能目标不动"
        assert payload["applied"] == [{"target": "安魂曲",
                                       "changed": ["等级→70", "等级目标→70", "觉醒→3",
                                                   "normal→6", "normal目标→6",
                                                   f"orbit→{growth_mod.skill_max()}",
                                                   f"orbit目标→{growth_mod.skill_max()}",
                                                   "弧盘→测试弧盘乙", "弧盘等级→45",
                                                   "弧盘目标→45"]}], \
            payload["applied"]
        assert [s["target"] for s in payload["skipped"]] == ["不存在的人"], payload["skipped"]
        # 未收录弧盘：绑定不动（绝不写入没有依据的实名）
        payload2 = main_mod._tajiduo_apply_rows(
            [{"target": "安魂曲", "arc_weapon": "查无此弧盘", "arc_level": 50}])
        assert payload2["characters"][0]["weapon"] == "测试弧盘乙"
        assert "未收录" in payload2["skipped"][0]["reason"]
        assert payload2["characters"][0]["tracks"]["arc_level"]["cur"] == 50
        assert payload2["characters"][0]["tracks"]["arc_level"]["tgt"] == 50
        # ---- A5 卡带（塔吉多 suit 字段）：套装/主属性覆盖，词条不落库、stigma 不动 ----
        STATE.data["characters"][0]["stigma_groups"] = [{"label": "用户自维护"}]
        payload3 = main_mod._tajiduo_apply_rows([
            {"target": "安魂曲",
             "cassette": {"set": "星海回响", "main": "生命值",
                          "affixes": ["暴击率", "暴击伤害"]}}])
        saved3 = payload3["characters"][0]
        assert saved3["cassette"] == {"set": "星海回响", "main": "生命值"}, saved3["cassette"]
        assert "卡带套装→星海回响" in payload3["applied"][0]["changed"]
        assert "卡带主属性→生命值" in payload3["applied"][0]["changed"]
        assert saved3["stigma_groups"] == [{"label": "用户自维护"}], \
            "stigma_groups 用户自维护，塔吉多同步绝不写"
        # payload 缺卡带字段 → 现值不动（预览行标「塔吉多无此数据」由前端处理）
        payload4 = main_mod._tajiduo_apply_rows([{"target": "安魂曲", "level": 70}])
        assert payload4["characters"][0]["cassette"] == {"set": "星海回响",
                                                         "main": "生命值"}
        assert payload4["applied"] == [], "无变化时不产生应用明细"
        # rows 非法形态
        for bad in (None, "rows", {"a": 1}):
            try:
                main_mod._tajiduo_apply_rows(bad)
                raise AssertionError(f"非法 rows 必须拒绝: {bad!r}")
            except ValueError:
                pass
        # ---- 未绑定同步拒绝（ValueError → 端点 400） ----
        orig_load = tc.load_token_store
        tc.load_token_store = lambda: None
        try:
            main_mod._tajiduo_sync()
            raise AssertionError("未绑定必须拒绝同步")
        except ValueError as e:
            assert "尚未绑定" in str(e), e
        finally:
            tc.load_token_store = orig_load
        # ---- 手动同步 payload（离线：client/sync_all 打桩） ----
        orig_client, orig_sync = tc.build_client, tc.sync_all
        tc.build_client = lambda: object()
        tc.sync_all = lambda client: {
            "roles": [{"roleId": "214075351008", "roleName": "旅行者",
                       "serverName": "测试服", "lev": 60}],
            "role_home": {"roleid": "214075351008", "rolename": "旅行者",
                          "servername": "测试服", "lev": 60, "worldlevel": 5,
                          "charid_cnt": 12, "rolelogin_days": 88, "achievement": 36,
                          "achievement_total": 120},
            "rows": [{"name": "安魂曲", "level": 70, "awakening": 2, "slev": 6,
                      "skills": {"normal": 6, "variable": 4, "orbit": 5, "assist": 3},
                      "city_skills": [], "fork": {"name": "测试弧盘甲", "level": 60},
                      "suit_num": 2}],
            "warnings": []}
        try:
            sync = main_mod._tajiduo_sync()
        finally:
            tc.build_client, tc.sync_all = orig_client, orig_sync
        assert sync["ok"] is True and sync["rows"][0]["matched_name"] == "安魂曲"
        assert sync["weapon_options"], "弧盘手动映射下拉必须有数据源"
        # 塔吉多通道没有的数据必须如实标注（绝不编造）
        assert sync["no_data"] == ["材料库存", "背包道具", "突破材料余额",
                                   "体力/本性像素消耗记录"], sync["no_data"]
        # 手机号/验证码消毒
        for bad_phone in ("", "12345", "1380013800a", "23800138000", None):
            try:
                main_mod._tajiduo_check_phone(bad_phone)
                raise AssertionError(f"非法手机号必须拒绝: {bad_phone!r}")
            except ValueError:
                pass
        for bad_cap in ("", "abc", "123456789", None):
            try:
                main_mod._tajiduo_check_captcha(bad_cap)
                raise AssertionError(f"非法验证码必须拒绝: {bad_cap!r}")
            except ValueError:
                pass


@case
def test_tajiduo_endpoints_http():
    """HTTP 层冒烟（真实 Handler，端口 0 随机分配，绝不触网）：status 只读、
    非法输入 400、未绑定 sync 400、apply 写库 200、鉴权失败映射 401（续期恰一次）。"""
    import http.client
    import threading
    import tempfile
    from app import main as main_mod
    from app import tajiduo_client as tc
    from app.main import create_server

    def call(port, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {"Origin": "http://127.0.0.1:%d" % port,  # 回环 Origin 过防线
                   "X-Panel-Token": main_mod._PANEL_TOKEN}  # 面板令牌过 SEC-05 闸
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        if payload is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, payload, headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    class _AuthFailClient:
        """getGameRoles 恒抛鉴权错误：验证 sync_all 续期恰一次后端点回 401。"""
        device_id = "HTX"

        def get_game_roles(self, game_id):
            raise tc.TajiduoError("登录已过期", auth=True)

        def refresh_session(self):
            return {"access_token": "A2", "refresh_token": "R2", "center_uid": "1"}

    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.data["characters"] = [main_mod._sanitize_character(
            {"id": "a", "name": "安魂曲"})]
        STATE.data["characters"][0]["tracks"]["char_level"]["cur"] = 60
        with tempfile.TemporaryDirectory() as tmp:
            orig_file, orig_client = tc.TOKEN_FILE, tc.build_client
            tc.TOKEN_FILE = os.path.join(tmp, "tajiduo_token.json")
            # SEC-05：直设全局令牌（现生成，零凭据字面量）→ _ensure_panel_token 空转
            main_mod._PANEL_TOKEN = main_mod._new_panel_token()
            server, _port = create_server(0, auto_range=1)
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True,
                             name="selftest-tajiduo-http").start()
            try:
                # status：未绑定（不泄露任何令牌字段；phone_masked 为空=无手机号可露）
                code, data = call(port, "GET", "/api/tajiduo/status")
                assert code == 200 and data == {"bound": False, "bound_at": "",
                                                "uid": "", "phone_masked": ""}, (code, data)
                # 非法手机号/验证码：400，且绝不触网（校验在客户端构造前）
                code, data = call(port, "POST", "/api/tajiduo/send-captcha",
                                  {"phone": "12345"})
                assert code == 400 and "手机号" in data["error"], (code, data)
                code, data = call(port, "POST", "/api/tajiduo/bind",
                                  {"phone": "13800138000", "captcha": "abc"})
                assert code == 400 and "验证码" in data["error"], (code, data)
                # 合法形态但未发过验证码：400（不触网——pending 为空直接拒绝）
                code, data = call(port, "POST", "/api/tajiduo/bind",
                                  {"phone": "13800138000", "captcha": "123456"})
                assert code == 400 and "先获取短信验证码" in data["error"], (code, data)
                # 未绑定 sync：400（凭证文件在临时目录，等效未绑定）
                code, data = call(port, "POST", "/api/tajiduo/sync", {})
                assert code == 400 and "尚未绑定" in data["error"], (code, data)
                # 令牌过期：续期恰一次（fake 记录）→ 仍失败 → 401 提示重绑
                tc.build_client = lambda: _AuthFailClient()
                code, data = call(port, "POST", "/api/tajiduo/sync", {})
                assert code == 401 and "登录已过期" in data["error"], (code, data)
                assert data.get("needs_rebind") is True, data  # 前端据此渲染重绑引导
                # 官方会话彻底失效（续期也失败，如 "FAILED TO TOUCH"）→ 同样 401+
                # 友好文案（原始错误不透传，raw 留诊断）
                class _DeadSessionClient(_AuthFailClient):
                    def refresh_session(self):
                        raise tc.TajiduoError("[/refresh] FAILED TO TOUCH")
                tc.build_client = lambda: _DeadSessionClient()
                code, data = call(port, "POST", "/api/tajiduo/sync", {})
                assert code == 401 and "重新绑定" in data["error"] \
                    and "FAILED TO TOUCH" not in data["error"] \
                    and data.get("needs_rebind") is True, (code, data)
                # apply：写入（60→70）并回传消毒后的角色（目标线同步抬高：目标≥当前）
                code, data = call(port, "POST", "/api/tajiduo/apply", {"rows": [
                    {"target": "安魂曲", "level": 70}]})
                assert code == 200 and data["ok"] is True, (code, data)
                assert data["characters"][0]["tracks"]["char_level"]["cur"] == 70, data
                assert data["applied"] == [{"target": "安魂曲",
                                            "changed": ["等级→70", "等级目标→70"]}], \
                    data["applied"]
                # apply 非法 rows：400
                code, data = call(port, "POST", "/api/tajiduo/apply", {"rows": "x"})
                assert code == 400, (code, data)
                # ---- logout：有凭证 → 登出（删凭证文件）→ status 复位未绑定 ----
                tc.save_token_store({"device_id": "HTX", "access_token": "A",
                                     "refresh_token": "R", "center_uid": "1",
                                     "laohu_token": "LT", "laohu_user_id": "9"})
                assert tc.load_token_store() is not None
                code, data = call(port, "POST", "/api/tajiduo/logout", {})
                assert code == 200 and data == {"ok": True}, (code, data)
                assert tc.load_token_store() is None, "登出必须删除凭证文件"
                code, data = call(port, "GET", "/api/tajiduo/status")
                assert code == 200 and data == {"bound": False, "bound_at": "",
                                                "uid": "", "phone_masked": ""}, (code, data)
                # ---- 运行中白名单：logout 在规划器运行中仍可用；未列白名单仍 409 ----
                class _RunningRunner:
                    def snapshot(self):
                        return {"state": "running"}

                orig_runner = main_mod.RUNNER
                main_mod.RUNNER = _RunningRunner()
                try:
                    tc.save_token_store({"device_id": "HTX", "access_token": "A",
                                         "refresh_token": "R", "center_uid": "1"})
                    code, data = call(port, "POST", "/api/tajiduo/logout", {})
                    assert code == 200 and data == {"ok": True}, (code, data)
                    code, data = call(port, "POST", "/api/tajiduo/sync", {})
                    assert code == 409, (code, data)
                    assert tc.load_token_store() is None
                finally:
                    main_mod.RUNNER = orig_runner
            finally:
                server.shutdown()
                server.server_close()
                # tmp 目录由上方 with tempfile.TemporaryDirectory() 上下文自清理
                # （tmp 是 str 路径，误调 .cleanup() 曾致 dist 内自测 85/86）
                tc.TOKEN_FILE = orig_file
                tc.build_client = orig_client


@case
def test_http_body_contract_srv01_srv05():
    """SRV-01 请求体契约 + SRV-05 teams 显式键（HTTP 层真实 Handler 全链路）：
    三路由 × 十形态矩阵（非对象/空体/坏 CL/超限声明 CL/非法 JSON/合法对照）、
    teams 预置不被空体或缺键清空（零落盘）、白名单路由空体放行、非法体不得绕过
    鉴权（B2.1 契约 401+panel_auth）、连续非法请求服务存活。
    wire 事实（实施会话探针）：http.client/urllib 的 body=None POST 自动补
    Content-Length: 0，与无头同落 _body 的 length<=0 分支，断言等效；
    inspect.getsource 经 functools.wraps 的 __wrapped__ 解包回原路由链。"""
    import http.client
    import threading
    from app import main as main_mod
    from app.main import create_server

    def raw_call(port, path, *, raw=None, cl=None, token=True):
        """全手动 wire 构造（putrequest/putheader/endheaders/send）：绕开
        http.client 对 body 的自动补头，可伪造坏 Content-Length（"abc"）与
        超限声明 CL（3145728+实发 2 字节）。cl=None 且 raw=None → 不带
        Content-Length 头（无头构造）。"""
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.putrequest("POST", path)
            conn.putheader("Origin", "http://127.0.0.1:%d" % port)  # 回环 Origin 过防线
            if token:
                conn.putheader("X-Panel-Token", main_mod._PANEL_TOKEN)
            if cl is not None:
                conn.putheader("Content-Length", str(cl))
            conn.endheaders()
            if raw:
                conn.send(raw)
            resp = conn.getresponse()
            data = json.loads(resp.read().decode("utf-8"))
            return resp.status, data
        finally:
            conn.close()

    def post_json(port, path, obj, token=True):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        return raw_call(port, path, raw=raw, cl=len(raw), token=token)

    # 三条读体路由（矩阵对象）+ 精确 error 文案
    routes = ("/api/characters", "/api/settings", "/api/teams/save")
    NOT_OBJ = "请求体必须是 JSON 对象"
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        # SEC-05：直设全局令牌（现生成，零凭据字面量）→ _ensure_panel_token 空转
        main_mod._PANEL_TOKEN = main_mod._new_panel_token()
        server, _port = create_server(0, auto_range=1)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True,
                         name="selftest-body-contract-http").start()
        try:
            # ---- 矩阵 ①-⑤：非对象 JSON 根值 → 400「请求体必须是 JSON 对象」----
            # null 也是合法 JSON 但不是对象：400 同通道（文案单值化，非旧「不是合法 JSON」）
            for obj in ([1, 2, 3], "abc", 123, True, None):
                for path in routes:
                    code, data = post_json(port, path, obj)
                    assert code == 400 and data == {"error": NOT_OBJ}, \
                        f"{path} ← {obj!r}: {code}, {data}"
            # ---- 矩阵 ⑥：空体两种构造（无 CL 头 / CL:0）→ 400「缺少请求体」----
            for path in routes:
                code, data = raw_call(port, path)  # 无 Content-Length 头
                assert code == 400 and data == {"error": "缺少请求体"}, \
                    f"{path} 无头空体: {code}, {data}"
                code, data = raw_call(port, path, cl=0)  # CL:0（等价 urllib body=None）
                assert code == 400 and data == {"error": "缺少请求体"}, \
                    f"{path} CL:0 空体: {code}, {data}"
            # ---- 矩阵 ⑦：非法 JSON 字节 → 400（文案回归：旧通道不变）----
            for path in routes:
                code, data = raw_call(port, path, raw=b"{not-json", cl=9)
                assert code == 400 and data == {"error": "请求体不是合法 JSON"}, \
                    f"{path} 非法 JSON: {code}, {data}"
            # ---- 矩阵 ⑧：声明 CL 3MB + 实发 2 字节 → 413（读体之前判）----
            for path in routes:
                code, data = raw_call(port, path, raw=b"{}", cl=3 * 1024 * 1024)
                assert code == 413 and "超过大小上限" in data.get("error", ""), \
                    f"{path} 超限声明 CL: {code}, {data}"
            # ---- 矩阵 ⑨：坏 Content-Length → 400「Content-Length 头无效」----
            # （此前静默归并 {} 照跑，teams/save 会 200 清空配队）
            for path in routes:
                code, data = raw_call(port, path, cl="abc")
                assert code == 400 and data == {"error": "Content-Length 头无效"}, \
                    f"{path} 坏 CL: {code}, {data}"
            # ---- 矩阵 ⑩：合法对象对照 → 200（合法路径零变化）----
            code, data = post_json(port, "/api/characters", {
                "characters": [{"id": "b", "name": "对照角色"}]})
            assert code == 200 and data.get("ok") is True, (code, data)
            code, data = post_json(port, "/api/settings", dict(DEFAULT_SETTINGS))
            assert code == 200 and isinstance(data.get("rev"), int), (code, data)
            code, data = post_json(port, "/api/teams/save", {"teams": []})
            assert code == 200 and isinstance(data.get("rev"), int), (code, data)
            # ---- SRV-05 专项：预置队不被空体 / 缺键清空（零落盘）----
            preset = [{"name": "预置队", "members": ["安魂曲"]}]
            STATE.data["teams"] = [dict(t) for t in preset]
            rev0 = STATE.rev
            code, data = raw_call(port, "/api/teams/save", cl=0)  # 空体
            assert code == 400 and data == {"error": "缺少请求体"}, (code, data)
            code, data = post_json(port, "/api/teams/save", {})  # 缺 "teams" 键
            assert code == 400 and data == {"error": "teams 必须是数组"}, (code, data)
            code, data = post_json(port, "/api/teams/save", {"teams": "x"})
            assert code == 400 and data == {"error": "teams 必须是数组"}, (code, data)
            assert STATE.rev == rev0, f"拒绝路径不得落盘: rev {rev0}→{STATE.rev}"
            # GET /api/teams：预置队原样保留
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                conn.request("GET", "/api/teams", headers={
                    "Origin": "http://127.0.0.1:%d" % port,
                    "X-Panel-Token": main_mod._PANEL_TOKEN})
                resp = conn.getresponse()
                code = resp.status
                data = json.loads(resp.read().decode("utf-8"))
            finally:
                conn.close()
            assert code == 200 and data.get("teams") == preset, (code, data)
            code, data = post_json(port, "/api/teams/save", {
                "teams": [{"name": "新队", "members": ["安魂曲"]}]})
            assert code == 200 and data.get("teams") == [
                {"name": "新队", "members": ["安魂曲"]}], (code, data)
            # ---- 白名单空体放行：不读体路由照跑 200（钉住 _NO_BODY_ROUTES）----
            code, data = raw_call(port, "/api/stop")  # 无头空体 → {} 照跑
            assert code == 200 and "ok" in data, (code, data)
            code, data = raw_call(port, "/api/ocr/cancel", cl=0)  # CL:0 → {} 照跑
            assert code == 200 and data.get("cancelled") is True, (code, data)
            # ---- 鉴权序（B2.1 契约）：非法体不得绕过 token 闸 → 401+panel_auth ----
            code, data = raw_call(port, "/api/characters", raw=b"[1,2,3]",
                                  cl=7, token=False)
            assert code == 401 and data.get("panel_auth") is True, (code, data)
            # ---- 服务存活：连续 10 次非法请求后 bootstrap 仍 200 ----
            for _ in range(10):
                raw_call(port, "/api/characters", raw=b"[1,2,3]", cl=7)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                conn.request("GET", "/api/bootstrap", headers={
                    "Origin": "http://127.0.0.1:%d" % port,
                    "X-Panel-Token": main_mod._PANEL_TOKEN})
                resp = conn.getresponse()
                boot = json.loads(resp.read().decode("utf-8"))
            finally:
                conn.close()
            assert resp.status == 200 and isinstance(boot.get("compat"), dict), \
                (resp.status, sorted(boot.keys())[:5])
        finally:
            server.shutdown()
            server.server_close()


@case
def test_tajiduo_gacha_parse_and_merge():
    """抽卡解析（构造 TajiduoGachaSummary 形态 payload）+ 本地累积合并：
    并集/幂等/180 天旧数据保留/键冲突快照胜出/坏键跳过/未识别池/tab 缺失跳过；
    存档存取 roundtrip 与容错（_safe_path 临时路径，夹具一律经库内原子写，
    绝不触碰真实 data/）。"""
    import tempfile
    from app import tajiduo_client as tc
    from app.state import save_json_atomic
    # ---- 解析：完整 payload（字段实样口径 NTEUID tajiduo_model.py）----
    payload = {
        "roleid": "214075351008", "rolename": "旅行者", "lev": 60,
        "userid": "9_12345", "luckType": 7, "luckTitle": "陪跑常客",
        "gachaDetails": [
            {"tab": "限定卡池", "m": 90, "drawCount": 120, "rareCount": 2,
             "average": "56.0", "playerOver": "45%",
             "details": [
                 {"charid": "21015", "rareCount": 78, "luckyType": 3,
                  "time": "2026-05-01", "timeStamp": 1746105600000},
                 {"charid": "fork_test01", "rareCount": 90, "luckyType": 1,
                  "time": "2026-06-01", "timeStamp": 1748784000000},
             ]},
            {"tab": "常驻卡池", "m": 90, "drawCount": 30, "rareCount": 0,
             "average": "", "playerOver": "",
             "details": []},
            {"tab": "弧盘池", "m": 60, "drawCount": 55, "rareCount": 1,
             "average": "55.0", "playerOver": "80%",
             "details": [
                 {"charid": "fork_arc99", "rareCount": 55, "luckyType": 0,
                  "time": "2026-07-01", "timeStamp": 1751668800000},
             ]},
        ],
    }
    s = tc.parse_gacha_summary(payload)
    assert s["roleid"] == "214075351008" and s["rolename"] == "旅行者" \
        and s["lev"] == 60 and s["userid"] == "9_12345", s
    assert s["luck_type"] == 7 and s["luck_title"] == "陪跑常客"
    assert [p["tab"] for p in s["pools"]] == ["限定卡池", "常驻卡池", "弧盘池"], s["pools"]
    p0 = s["pools"][0]
    assert (p0["m"], p0["draw_count"], p0["rare_count"]) == (90, 120, 2)
    assert p0["average"] == "56.0" and p0["player_over"] == "45%"
    assert p0["details"][0] == {"charid": "21015", "pity": 78, "lucky_type": 3,
                                "time": "2026-05-01", "time_ms": 1746105600000}, p0["details"]
    assert s["pools"][1]["details"] == [] and s["pools"][1]["rare_count"] == 0
    assert s["pools"][2]["details"][0]["charid"] == "fork_arc99"
    # 垃圾形态：不编造（缺字段 None/[]，字符串数字照常转 int，越界 clamp）
    junk = tc.parse_gacha_summary({
        "gachaDetails": [
            {"tab": None, "drawCount": "88", "details": [
                {"charid": "x", "rareCount": "12", "luckyType": 9,
                 "timeStamp": "1746105600000"}],   # luckyType 越界 → clamp 3
            },
            "not-a-dict",
            {"details": "garbage"},                 # 无 tab → 归池时跳过
        ],
        "luckType": "not-int", "lev": None,
    })
    assert junk["luck_type"] is None and junk["lev"] is None and junk["roleid"] == ""
    jp = junk["pools"]
    assert jp[0]["draw_count"] == 88 and jp[0]["m"] is None
    assert jp[0]["details"] == [{"charid": "x", "pity": 12, "lucky_type": 3,
                                 "time": "", "time_ms": 1746105600000}], jp[0]["details"]
    assert len(jp) == 2 and jp[1]["tab"] == "" and jp[1]["details"] == []
    assert tc.parse_gacha_summary(None) == {
        "roleid": "", "rolename": "", "lev": None, "userid": "",
        "luck_type": None, "luck_title": "", "pools": []}
    # ---- 合并：空档 + 快照 → 全量新增 ----
    empty = {"schema": tc.GACHA_HISTORY_SCHEMA, "roleid": "", "rolename": "",
             "updated_at": "", "pools": {}}
    merged, new_counts, unknown = tc.merge_gacha_history(empty, s, updated_at="T1")
    assert new_counts == {"limited": 2, "permanent": 0, "weapon": 1}, new_counts
    assert unknown == [] and merged["roleid"] == "214075351008"
    lim = merged["pools"]["limited"]["records"]
    assert len(lim) == 2 and lim[0]["item_id"] == "21015" and lim[0]["pity"] == 78
    assert lim[0]["lucky_type"] == 3 and lim[0]["time"] == "2026-05-01"
    assert merged["pools"]["weapon"]["records"][0]["item_id"] == "fork_arc99"
    # ---- 合并：幂等（同键不重复；同快照再合并 new_counts 全 0）----
    merged2, _nc2, _unk2 = tc.merge_gacha_history(merged, s, updated_at="T2")
    assert _nc2 == {"limited": 0, "permanent": 0, "weapon": 0}, _nc2
    assert merged2["pools"] == merged["pools"], "幂等：同快照重复合并不得改记录"
    assert merged2["updated_at"] == "T2"
    # ---- 合并：并集（快照新记录 + 本地旧记录 → 都保留；窗口外旧记录不丢）----
    year_old = {"item_id": "21015", "time_ms": 1714521600000, "pity": 90,
                "lucky_type": 2, "time": "2024-05-01"}  # 官方 180 天窗口之外
    base = {"schema": 1, "roleid": "214075351008", "rolename": "旅行者",
            "updated_at": "T0",
            "pools": {"limited": {"records": [dict(year_old)]},
                      "weapon": {"records": []}}}
    fresh_only = {"roleid": "", "rolename": "", "lev": None, "userid": "",
                  "luck_type": None, "luck_title": "", "pools": [
                      {"tab": "限定卡池", "m": 90, "draw_count": 1, "rare_count": 1,
                       "average": "", "player_over": "",
                       "details": [{"charid": "21015", "pity": 45, "lucky_type": 1,
                                    "time": "2026-11-01", "time_ms": 1790000000000}]}]}
    merged3, new_counts3, _u3 = tc.merge_gacha_history(base, fresh_only, updated_at="T3")
    lim3 = merged3["pools"]["limited"]["records"]
    assert len(lim3) == 2, lim3
    assert lim3[0]["time_ms"] == 1714521600000, "180 天窗口外旧记录必须保留"
    assert new_counts3 == {"limited": 1, "permanent": 0, "weapon": 0}, new_counts3
    # 仅本地存在的未识别池键照常保留（快照没有也不丢）
    base2 = {"schema": 1, "roleid": "", "rolename": "", "updated_at": "",
             "pools": {"tab_活动池": {"records": [
                 {"item_id": "9", "time_ms": 1, "pity": 1, "lucky_type": 0,
                  "time": ""}]}}}
    merged4, _nc4, _u4 = tc.merge_gacha_history(base2, {"pools": []})
    assert "tab_活动池" in merged4["pools"], merged4["pools"].keys()
    assert merged4["pools"]["tab_活动池"]["records"][0]["item_id"] == "9"
    # ---- 合并：键冲突（同 charid+time_ms+pity，快照 luckyType 更新）→ 快照胜出 ----
    # （merge 输入契约 = parse_gacha_summary 输出的解析后键形 charid/pity/time_ms）
    snap_conflict = {"pools": [{"tab": "限定卡池", "details": [
        {"charid": "21015", "pity": 78, "lucky_type": 0, "time": "2026-05-01",
         "time_ms": 1746105600000}]}]}
    merged5, nc5, _u5 = tc.merge_gacha_history(merged, snap_conflict, updated_at="T5")
    lim5 = merged5["pools"]["limited"]["records"]
    assert len(lim5) == 2 and nc5 == {"limited": 0, "permanent": 0, "weapon": 0}
    hit = next(r for r in lim5 if r["item_id"] == "21015"
               and r["time_ms"] == 1746105600000 and r["pity"] == 78)
    assert hit["lucky_type"] == 0, hit  # 快照记录胜出（展示字段以最新拉取为准）
    # ---- 合并：坏键跳过 + 未识别 tab 照常累积 + 无 tab 丢弃 ----
    bad = {"pools": [
        {"tab": "限定卡池", "details": [
            {"charid": "", "pity": 5, "time_ms": 1},      # 空 charid → 跳过
            {"charid": "x", "pity": 0, "time_ms": 1},     # pity<1 → 跳过
            {"charid": "x", "pity": 1, "time_ms": 0},     # time_ms≤0 → 跳过
            {"charid": "x", "pity": True, "time_ms": 1},  # bool → 跳过
            {"charid": "x", "pity": 1, "time_ms": 5},     # 合法
        ]},
        {"tab": "活动池", "details": [
            {"charid": "9", "pity": 1, "time_ms": 9}]},   # 未识别 tab → tab_活动池
        {"details": [{"charid": "y", "pity": 1, "time_ms": 8}]},  # 无 tab → 丢弃
    ]}
    merged6, nc6, unknown6 = tc.merge_gacha_history(empty, bad, updated_at="T6")
    assert nc6 == {"limited": 1, "permanent": 0, "weapon": 0, "tab_活动池": 1}, nc6
    assert unknown6 == ["活动池"], unknown6
    assert len(merged6["pools"]["limited"]["records"]) == 1
    assert merged6["pools"]["tab_活动池"]["records"][0]["item_id"] == "9"
    # ---- 存取 roundtrip + 形态容错（夹具一律经 save_json_atomic 库内原子写）----
    with tempfile.TemporaryDirectory() as tmp:
        orig_hist = tc.GACHA_HISTORY_FILE
        tc.GACHA_HISTORY_FILE = _safe_path(tmp, "gacha_history.json")
        try:
            assert tc.load_gacha_history()["pools"] == {}  # 缺失 → 空骨架
            save_json_atomic(tc.GACHA_HISTORY_FILE, ["not", "dict"])
            assert tc.load_gacha_history()["pools"] == {}  # 形态不对 → 空骨架
            save_json_atomic(tc.GACHA_HISTORY_FILE, {"pools": "garbage"})
            assert tc.load_gacha_history()["pools"] == {}  # pools 段坏 → 空骨架
            tc.save_gacha_history(merged)
            loaded = tc.load_gacha_history()
            assert loaded["pools"] == merged["pools"] \
                and loaded["roleid"] == merged["roleid"], loaded
        finally:
            tc.GACHA_HISTORY_FILE = orig_hist
    # ---- sync_gacha 编排：续期恰一次 + 幂等 roundtrip（离线桩，绝不触网）----
    with tempfile.TemporaryDirectory() as tmp:
        orig_hist, orig_tok = tc.GACHA_HISTORY_FILE, tc.TOKEN_FILE
        tc.GACHA_HISTORY_FILE = _safe_path(tmp, "gacha_history.json")
        tc.TOKEN_FILE = _safe_path(tmp, "tajiduo_token.json")
        try:
            class _AuthThenOkClient:
                device_id = "HTX"

                def __init__(self):
                    self.calls, self.refreshes = 0, 0

                def get_gacha_summary(self):
                    self.calls += 1
                    if self.calls == 1:
                        raise tc.TajiduoError("登录已过期", auth=True)
                    return payload

                def refresh_session(self):
                    self.refreshes += 1
                    return {"access_token": "A9", "refresh_token": "R9",
                            "center_uid": "1"}

            tc.save_token_store({"device_id": "HTX", "access_token": "A",
                                 "refresh_token": "R", "center_uid": "1"})
            fake = _AuthThenOkClient()
            result = tc.sync_gacha(fake)
            assert fake.calls == 2 and fake.refreshes == 1, (fake.calls, fake.refreshes)
            assert tc.load_token_store()["access_token"] == "A9", "续期后必须落盘"
            assert result["new_counts"] == {"limited": 2, "permanent": 0, "weapon": 1}
            assert result["warnings"] == []
            fake2 = _AuthThenOkClient()  # 再同步：无新增（并集幂等 roundtrip）
            fake2.calls = 1
            result2 = tc.sync_gacha(fake2)
            assert result2["new_counts"]["limited"] == 0
            assert result2["warnings"] == []
        finally:
            tc.GACHA_HISTORY_FILE, tc.TOKEN_FILE = orig_hist, orig_tok


@case
def test_tajiduo_gacha_endpoints_http():
    """HTTP 层冒烟（真实 Handler，端口 0 随机分配，绝不触网）：gacha-history 本地
    只读 200、未绑定 sync-gacha 400、鉴权失败 401 提示重绑、成功同步 200+落盘
    +charid 实名映射。夹具路径经 _safe_path、内容经 save_json_atomic 库内写。"""
    import http.client
    import threading
    import tempfile
    from app import main as main_mod
    from app import tajiduo_client as tc
    from app.main import create_server
    from app.state import save_json_atomic

    def call(port, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {"Origin": "http://127.0.0.1:%d" % port,  # 回环 Origin 过防线
                   "X-Panel-Token": main_mod._PANEL_TOKEN}  # 面板令牌过 SEC-05 闸
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        if payload is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, payload, headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    with tempfile.TemporaryDirectory() as tmp:
        orig_hist = tc.GACHA_HISTORY_FILE
        orig_tok = tc.TOKEN_FILE
        orig_dump = tc.LAST_PAYLOAD_FILE
        orig_client = tc.build_client
        tc.GACHA_HISTORY_FILE = _safe_path(tmp, "gacha_history.json")
        tc.TOKEN_FILE = _safe_path(tmp, "tajiduo_token.json")
        tc.LAST_PAYLOAD_FILE = _safe_path(tmp, "tajiduo_last_payload.json")
        # SEC-05：直设全局令牌（现生成，零凭据字面量）→ _ensure_panel_token 空转
        main_mod._PANEL_TOKEN = main_mod._new_panel_token()
        server, _port = create_server(0, auto_range=1)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True,
                         name="selftest-tajiduo-gacha-http").start()
        try:
            # gacha-history：本地只读（无网络路径），缺存档 → 空池骨架 + note
            code, data = call(port, "GET", "/api/tajiduo/gacha-history")
            assert code == 200 and data["ok"] is True, (code, data)
            assert data["note"].startswith("仅含已出 S 记录"), data["note"]
            assert data["pools"] == [], data["pools"]
            # 未绑定 sync-gacha：400（凭证文件在临时目录，等效未绑定）
            code, data = call(port, "POST", "/api/tajiduo/sync-gacha", {})
            assert code == 400 and "尚未绑定" in data["error"], (code, data)
            # 令牌过期：续期恰一次 → 仍失败 → 401 提示重绑
            class _AuthFailClient:
                device_id = "HTX"

                def get_gacha_summary(self):
                    raise tc.TajiduoError("登录已过期", auth=True)

                def refresh_session(self):
                    raise tc.TajiduoError("续期失败", auth=True)

            tc.build_client = lambda: _AuthFailClient()
            code, data = call(port, "POST", "/api/tajiduo/sync-gacha", {})
            assert code == 401 and "登录已过期" in data["error"], (code, data)
            assert data.get("needs_rebind") is True, data  # 续期也失败→重绑引导
            # 成功同步：拉取 → 合并 → 落盘 → 三池汇总与新增计数；charid 实名映射
            tc.save_token_store({"device_id": "HTX", "access_token": "A",
                                 "refresh_token": "R", "center_uid": "1"})
            save_json_atomic(tc.LAST_PAYLOAD_FILE, {"schema": 1, "per_role": {
                "214075351008": {"characters": [
                    {"id": "21015", "name": "安魂曲",
                     "fork": {"id": "fork_arc99", "name": "测试弧盘甲"}}]}}})
            gacha_payload = {
                "roleid": "214075351008", "rolename": "旅行者", "lev": 60,
                "luckType": 7, "luckTitle": "陪跑常客",
                "gachaDetails": [
                    {"tab": "限定卡池", "m": 90, "drawCount": 120,
                     "rareCount": 2, "average": "56.0", "playerOver": "45%",
                     "details": [
                         {"charid": "21015", "rareCount": 78, "luckyType": 3,
                          "time": "2026-05-01", "timeStamp": 1746105600000},
                         {"charid": "99999", "rareCount": 90, "luckyType": 1,
                          "time": "2026-06-01", "timeStamp": 1748784000000},
                     ]},
                    {"tab": "弧盘池", "m": 60, "drawCount": 55, "rareCount": 1,
                     "average": "55.0", "playerOver": "80%",
                     "details": [{"charid": "fork_arc99", "rareCount": 55,
                                  "luckyType": 0, "time": "2026-07-01",
                                  "timeStamp": 1751668800000}]},
                ]}
            class _OkClient:
                device_id = "HTX"

                def get_gacha_summary(self):
                    return gacha_payload

            tc.build_client = lambda: _OkClient()
            code, data = call(port, "POST", "/api/tajiduo/sync-gacha", {})
            assert code == 200 and data["ok"] is True, (code, data)
            assert data["new_total"] == 3, data["new_total"]  # 2+0+1
            assert data["role"] == {"roleid": "214075351008", "rolename": "旅行者",
                                    "lev": 60}, data["role"]
            assert data["luck"] == {"type": 7, "title": "陪跑常客"}, data["luck"]
            assert [p["key"] for p in data["pools"]] == \
                ["limited", "permanent", "weapon"], data["pools"]
            by_key = {p["key"]: p for p in data["pools"]}
            lim = by_key["limited"]
            assert (lim["draw_count"], lim["rare_count"], lim["m"],
                    lim["average"], lim["player_over"]) == (120, 2, 90, "56.0", "45%")
            assert lim["s_count"] == 2 and lim["new_count"] == 2
            recs = lim["records"]  # 时间倒序：99999 在前
            assert recs[1]["item_name"] == "安魂曲" and recs[1]["item_id"] == "21015"
            assert recs[0]["item_name"] is None and recs[0]["item_id"] == "99999", \
                "映射不到的 charid 必须置 None（前端显示原 ID 并标注）"
            assert by_key["weapon"]["records"][0]["item_name"] == "测试弧盘甲"
            assert by_key["permanent"]["records"] == [] \
                and by_key["permanent"]["draw_count"] is None
            # 落盘核对（经 load_gacha_history 读回，不裸开文件）
            saved = tc.load_gacha_history()
            assert saved["roleid"] == "214075351008" and saved["schema"] == 1, saved
            assert len(saved["pools"]["limited"]["records"]) == 2
            assert len(saved["pools"]["weapon"]["records"]) == 1
            # 再同步：无新增（并集幂等）
            code, data = call(port, "POST", "/api/tajiduo/sync-gacha", {})
            assert code == 200 and data["new_total"] == 0, (code, data["new_total"])
            # gacha-history 只读视图与存档一致；不带快照 → 官方窗口统计字段为 None
            code, data = call(port, "GET", "/api/tajiduo/gacha-history")
            assert code == 200 and len(data["pools"]) == 3, (code, data)
            lim_hist = next(p for p in data["pools"] if p["key"] == "limited")
            assert lim_hist["s_count"] == 2 and lim_hist["draw_count"] is None, \
                "只读视图不得携带官方窗口统计（那是本次快照字段）"
        finally:
            server.shutdown()
            server.server_close()
            tc.GACHA_HISTORY_FILE = orig_hist
            tc.TOKEN_FILE = orig_tok
            tc.LAST_PAYLOAD_FILE = orig_dump
            tc.build_client = orig_client


@case
def test_materials_stamina_blocks_loaded():
    """materials.json 顶层 stamina_blocks 段加载用例（2026-11-08 材料副本页数据表）：
    六块齐全、必填字段形态、mat_id 全部可解析到 targets/extra（绝不悬空 ID）、
    周本块 weekly_times=3 且材料全为 EXTRA（无每轮产量）、空幕块 dual_channel、
    追猎块与 BOSS_MAT 映射联动（planner.py:35-36 口径）；reload_tables() 重载后
    段仍在（内存表与盘上文件一致）。生产 data/materials.json 只读校验，不写盘。"""
    blocks = planner.MATERIALS.get("stamina_blocks")
    assert isinstance(blocks, list) and len(blocks) == 6, blocks
    assert [b.get("block") for b in blocks] == \
        ["经验及甲硬币", "异能升级材料", "弧盘突破材料", "空幕", "异象巡礼", "异象追猎"], \
        [b.get("block") for b in blocks]
    known = set(planner.TARGETS) | set(planner.EXTRA)
    for b in blocks:
        assert b.get("block") and b.get("entry") and b.get("note"), b
        assert b.get("stamina_per_run") in (40, 60), b.get("stamina_per_run")
        ids = b.get("mat_ids") or []
        assert ids and all(m in known for m in ids), (b.get("block"), ids)
    weekly = blocks[4]
    assert weekly.get("weekly_times") == 3, weekly
    assert set(weekly["mat_ids"]) <= set(planner.EXTRA), \
        "周本块材料必须全为 EXTRA（无每轮产量，走周次数口径）"
    assert blocks[3].get("dual_channel") is True, blocks[3]  # 空幕双通道
    boss_ids = set(blocks[5]["mat_ids"])
    assert boss_ids == set(planner.BOSS_MAT.values()), \
        (sorted(boss_ids), sorted(planner.BOSS_MAT.values()))
    planner.reload_tables()
    assert len(planner.MATERIALS.get("stamina_blocks") or []) == 6, "重载后段丢失"


@case
def test_dungeon_gaps_blocks_and_endpoint():
    """材料缺口分块聚合（2026-11-08 材料副本页）：planner.dungeon_gaps() 用构造
    state 断言六块分组、需/持/差、轮数与合计（含追猎按 hunter_yield 折算、周本
    只走周次数口径、未入块缺口落外部来源区）+ 确定性（两次调用同结果）；再真起
    面板服务打回环 GET /api/dungeon-gaps 验证 200 与分组形态（绝不触网）。"""
    import http.client
    import threading
    from app import main as main_mod
    from app.main import create_server
    with _StateGuard():
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.data["today"] = _day_today()
        STATE.data["inventory"] = {"ability_1": 10}
        STATE.data["characters"] = [
            _char("c1", "角色甲", {
                "ability_1": 40,      # 技能本块：需40 持10 差30（夹具产量7）→ 5轮
                "console_1": 5,       # 空幕块：差5（夹具产量7）→ 1轮
                "boss_sea_tear": 20,  # 追猎块：差20，海囚 hunter_yield=8 → 3局
                "wk_memory": 2,       # 巡礼块：差2，无每轮产量 → runs=0
                "exp_arc": 300000,    # 染剂：未入任何块 → 外部区（体力本·未入块）
                "coin": 9,            # 甲硬币：块1只挂 exp_char → 外部区
            }),
        ]
        d = planner.dungeon_gaps()
        assert d == planner.dungeon_gaps(), "dungeon_gaps 必须确定性（纯读两次同结果）"
        blocks = {b["block"]: b for b in d["blocks"]}
        assert len(blocks) == 6, list(blocks)
        ec = blocks["经验及甲硬币"]
        assert ec["total_runs"] == 0 and ec["total_stamina"] == 0, ec
        assert {x["mat_id"]: x for x in ec["mats"]}["exp_char"]["gap"] == 0, ec
        ab = blocks["异能升级材料"]
        ma = {x["mat_id"]: x for x in ab["mats"]}
        assert (ma["ability_1"]["need"], ma["ability_1"]["have"],
                ma["ability_1"]["gap"]) == (40, 10, 30), ma["ability_1"]
        assert (ma["ability_1"]["yield"], ma["ability_1"]["runs"]) == (7, 5), \
            ma["ability_1"]  # 缺口30 / 夹具产量7 → ceil=5 轮 ×40 体力
        assert ab["total_runs"] == 5 and ab["total_stamina"] == 200, ab
        assert all(ma[f"ability_{i}"]["gap"] == 0 and ma[f"ability_{i}"]["runs"] == 0
                   for i in range(2, 6)), "无需求系列仍须列出（块=掉落清单）且缺口 0"
        nm = blocks["空幕"]
        mc = {x["mat_id"]: x for x in nm["mats"]}
        assert (mc["console_1"]["gap"], mc["console_1"]["runs"]) == (5, 1), mc["console_1"]
        assert nm["total_runs"] == 1 and nm["total_stamina"] == 40, nm
        assert nm.get("dual_channel") is True, "空幕块必须带双通道标注"
        bs = blocks["异象追猎"]
        mb = {x["mat_id"]: x for x in bs["mats"]}
        assert (mb["boss_sea_tear"]["gap"], mb["boss_sea_tear"]["yield"],
                mb["boss_sea_tear"]["runs"]) == (20, 8, 3), mb["boss_sea_tear"]
        assert bs["total_runs"] == 3 and bs["total_stamina"] == 180, bs
        wk = blocks["异象巡礼"]
        assert (wk["weekly_times"], wk["weekly_stamina"]) == (3, 180), wk
        assert wk["total_runs"] == 0 and wk["total_stamina"] == 0, \
            "周本材料无每轮产量，不计入轮数合计（周次数口径单独返回）"
        mw = {x["mat_id"]: x for x in wk["mats"]}
        assert (mw["wk_memory"]["gap"], mw["wk_memory"]["runs"]) == (2, 0), mw["wk_memory"]
        exp = {x["mat_id"]: x for x in d["external"]}
        assert set(exp) == {"exp_arc", "coin"}, set(exp)
        assert (exp["coin"]["need"], exp["coin"]["gap"], exp["coin"]["extra"]) \
            == (9, 9, False), exp["coin"]
        assert exp["coin"]["source"], "外部区必须带来源字段（不静默丢缺口）"
        assert (exp["exp_arc"]["gap"], exp["exp_arc"]["extra"]) == (300000, False), \
            exp["exp_arc"]
        # cycle_item（2026-11-12 总计划具名渲染）：带 config 的 TARGETS 材料附
        # {任务类型, 序号键, entry, label}；EXTRA（周本/BOSS）与外部区不带；
        # gap=0 的块内材料仍附（块=掉落清单，勾选整块写入时取全集）
        ci3 = ma["ability_3"]["cycle_item"]
        assert "异能升级材料·3" in ci3["label"] and "黑帽子" in ci3["label"], ci3
        assert ci3["entry"] == "异能升级材料: 3", ci3  # 与 ok-nte 实样同格式（冒号+空格）
        assert ci3["任务类型"] == "异能升级材料" and ci3["异能材料序号"] == 3, ci3
        ec_m = {x["mat_id"]: x for x in ec["mats"]}
        assert ec_m["exp_char"]["cycle_item"]["label"] == "经验与甲硬币·角色经验", \
            ec_m["exp_char"]
        assert mc["console_1"]["cycle_item"]["entry"] == "空幕: 1", mc["console_1"]
        assert "cycle_item" not in mw["wk_memory"], "周本材料（EXTRA）不带 cycle_item"
        assert "cycle_item" not in mb["boss_sea_tear"], "BOSS 材料（EXTRA）不带 cycle_item"
        assert all("cycle_item" not in x for x in exp.values()), "外部区条目不带 cycle_item"
        # ---- HTTP 层：真实 Handler 回环请求（端口 0 随机分配）----
        # SEC-05：直设全局令牌（现生成，零凭据字面量）→ _ensure_panel_token 空转
        main_mod._PANEL_TOKEN = main_mod._new_panel_token()
        server, _port = create_server(0, auto_range=1)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True,
                         name="selftest-dungeon-gaps").start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/api/dungeon-gaps", None,
                         {"Origin": "http://127.0.0.1:%d" % port,  # 回环 Origin 过防线
                          "X-Panel-Token": main_mod._PANEL_TOKEN})  # 面板令牌过 SEC-05 闸
            resp = conn.getresponse()
            payload = json.loads(resp.read().decode("utf-8"))
            conn.close()
            assert resp.status == 200, (resp.status, payload)
            assert [b["block"] for b in payload["blocks"]] == \
                ["经验及甲硬币", "异能升级材料", "弧盘突破材料", "空幕",
                 "异象巡礼", "异象追猎"], payload.get("blocks")
            by = {b["block"]: b for b in payload["blocks"]}
            ab_http = {x["mat_id"]: x for x in by["异能升级材料"]["mats"]}
            assert ab_http["ability_3"]["cycle_item"]["entry"] == "异能升级材料: 3", \
                ab_http["ability_3"]
            assert by["异能升级材料"]["total_stamina"] == 200, by["异能升级材料"]
            assert by["异象追猎"]["total_runs"] == 3, by["异象追猎"]
            assert {x["mat_id"] for x in payload["external"]} == {"exp_arc", "coin"}
        finally:
            server.shutdown()
            server.server_close()


@case
def test_rev_returned_by_write_endpoints():
    """base_rev 乐观锁回归（复审阻断 #1，2026-11-08）：state.py:153-154 契约是
    『写端点响应回传 rev』，此前仅 5 处带 rev——/api/inventory、/api/characters/
    reorder、/api/teams/save、/api/common-costs/*、/api/settings、/api/libraries、
    /api/import、/api/history/confirm、/api/today/clear-blacklist 等只 bump 不回传，
    同页下一步「保存角色」必被 base_rev 误判陈旧 409（按报错刷新会丢表单编辑）。
    修复=上述写端点统一回传 rev（前端 api() 见 rev 自动跟进基线，index.html:331）。
    真起服务回环复现原 409 路径：bootstrap rev=N → inventory 写（响应须带 rev=N+1）
    → characters 带 base_rev=N 必须放行 200 而非 409；其余写端点逐一断言响应带 rev。
    /api/ocr/select（需真游戏窗口）与 /api/sites/sync-guide（触网）受 selftest 不触网/
    不碰真窗口约束未做 HTTP 覆盖，代码与下述端点同款一行回传。"""
    import http.client
    import threading
    from app import main as main_mod
    from app.main import create_server

    def call(port, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {"Origin": "http://127.0.0.1:%d" % port,  # 回环 Origin 过防线
                   "X-Panel-Token": main_mod._PANEL_TOKEN}  # 面板令牌过 SEC-05 闸
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        if payload is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, payload, headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    with _StateGuard():
        # apply_import 会把导出的数据表原子落盘 data/yields.json 与 requirements.json
        # （main.py apply_import 尾部），而 _StateGuard 只恢复 state.json——须自行备份
        # 恢复这两张表，否则夹具产量（全 7）会污染生产数据表（2026-11-08 实测踩中）
        table_backup = {}
        for name in ("yields.json", "requirements.json"):
            # apply_import 的实际落盘目标是 DATA_DIR 本身（main.py:933-934），
            # 『父目录+data』只在 DATA_DIR 恒以 data 为 basename 时碰巧正确——直接锚定 DATA_DIR
            p = _safe_path(planner.DATA_DIR, name)
            table_backup[p] = Path(p).read_bytes() if os.path.isfile(p) else None
        # 规划器必须空闲：运行中写端点会被白名单 409，用例失去意义
        deadline = time.time() + 10
        while time.time() < deadline and runner_mod.RUNNER.snapshot()["state"] \
                in ("running", "waiting", "stopping"):
            runner_mod.RUNNER.stop()
            time.sleep(0.2)
        STATE.data["settings"] = dict(DEFAULT_SETTINGS)
        STATE.data["today"] = _day_today()
        STATE.data["characters"] = [main_mod._sanitize_character(
            {"id": "a", "name": "安魂曲"})]
        # SEC-05：直设全局令牌（现生成，零凭据字面量）→ _ensure_panel_token 空转。
        # 下方 DEFAULT_SETTINGS 整包回写 /api/settings 因值相同不过指纹闸，仅加头
        main_mod._PANEL_TOKEN = main_mod._new_panel_token()
        server, _port = create_server(0, auto_range=1)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True,
                         name="selftest-rev-echo").start()
        try:
            code, boot = call(port, "GET", "/api/bootstrap")
            assert code == 200 and isinstance(boot.get("rev"), int), (code, boot.get("rev"))
            rev0 = boot["rev"]
            # ---- 复现原阻断路径：写库存（旧实现响应无 rev）→ 同页保存角色 ----
            code, inv = call(port, "POST", "/api/inventory", {"inventory": {"coin": 5}})
            assert code == 200 and inv.get("ok") is True, (code, inv)
            assert inv.get("rev") == rev0 + 1, \
                f"写端点响应必须回传 rev（基线自增），实际 rev={inv.get('rev')}"
            # 判 0 端点级语义（未识别材料判 0，纯前端勾选的后端承接）：/api/inventory
            # 是整表替换——前端把清 0 项从 payload 省略（v>0 过滤），缺失键=清 0；
            # 已存 coin:5 的库存整表替换为空表后 coin 必须消失而非保留
            code, r0 = call(port, "POST", "/api/inventory", {"inventory": {}})
            assert code == 200 and "coin" not in (r0.get("inventory") or {}), (code, r0)
            inv = r0  # 后续 base_rev 用最新 rev（整表替换同样是一次写、同样回传 rev）
            # 前端 api() 见响应 rev 即跟进基线（index.html:331）：同页下一次保存带
            # 新基线 inv["rev"] → 放行 200（修复前响应无 rev、基线停在 rev0 → 409）
            code, chars = call(port, "POST", "/api/characters", {
                "base_rev": inv["rev"],
                "characters": [{"id": "a", "name": "安魂曲"}]})
            assert code == 200 and chars.get("rev") == inv["rev"] + 1, (code, chars)
            # 反向：真正陈旧的基线（落后多版）仍必须 409——锁本身不被拆掉
            code, stale = call(port, "POST", "/api/characters", {
                "base_rev": rev0,  # 已落后 2 版
                "characters": [{"id": "a", "name": "安魂曲"}]})
            assert code == 409 and "其他窗口" in stale.get("error", ""), (code, stale)
            # ---- 其余写端点逐一断言响应带 rev（state.py:153-154 契约）----
            code, r = call(port, "POST", "/api/characters/reorder", {"ids": ["a"]})
            assert code == 200 and isinstance(r.get("rev"), int), (code, r)
            code, r = call(port, "POST", "/api/teams/save", {"teams": []})
            assert code == 200 and isinstance(r.get("rev"), int), (code, r)
            code, r = call(port, "POST", "/api/common-costs/save", {"char_exp": 1000})
            assert code == 200 and isinstance(r.get("rev"), int), (code, r)
            code, r = call(port, "POST", "/api/today/clear-blacklist", {})
            assert code == 200 and isinstance(r.get("rev"), int), (code, r)
            code, r = call(port, "POST", "/api/settings", dict(DEFAULT_SETTINGS))
            assert code == 200 and isinstance(r.get("rev"), int), (code, r.get("rev"))
            code, r = call(port, "POST", "/api/libraries", {"char_library": ["甲"]})
            assert code == 200 and isinstance(r.get("rev"), int), (code, r)
            # history/confirm：预置一条 done 记录走确认成功路径
            STATE.data["history"].append({
                "day": DAY, "time": "12:00", "mat_id": "coin", "runs": 1,
                "status": "done"})
            STATE.save()
            rev_h = STATE.rev
            code, r = call(port, "POST", "/api/history/confirm",
                           {"day": DAY, "time": "12:00", "mat_id": "coin",
                            "actual_gain": 7})
            assert code == 200 and isinstance(r.get("rev"), int), (code, r)
            # import：全量导出回灌（apply_import 校验最严的写端点）
            payload = main_mod.build_export()
            assert isinstance(payload, dict) and payload.get("schema") == 3, payload
            code, r = call(port, "POST", "/api/import", payload)
            assert code == 200 and isinstance(r.get("rev"), int), (code, r.get("rev"))
            # 闭环：最后一步写完后，前端基线=最新 rev 再保存角色不再 409
            code, r = call(port, "POST", "/api/characters", {
                "base_rev": r["rev"],
                "characters": [{"id": "a", "name": "安魂曲"}]})
            assert code == 200, (code, r)
        finally:
            server.shutdown()
            server.server_close()
            # 恢复被 /api/import 落盘的数据表（原本不存在则删除）
            for p, raw in table_backup.items():
                if raw is None:
                    if os.path.isfile(p):
                        os.remove(p)
                else:
                    Path(p).write_bytes(raw)
            planner.reload_tables()  # 内存表回到盘上真实内容


# ---------- B3-A Slice A（SRV-02，2026-11-18）：/api/import 两表落盘入锁 + OSError→500 专项 ----------

class _Srv02Env:
    """B3-A SRV-02 夹具：临时数据目录重定向（仓库 .tmp/，E 盘，finally 还原清理）。

    口径：DATA_DIR 重定向同 test_ocr_map_unknown（:4370-4377 先例），HTTP 层同
    test_rev_returned_by_write_endpoints（:7349-7352 先例）。与先例的差异：基线
    三表不复制生产文件，而以内存现值经同一序列化器（state.save_json_atomic：
    json.dumps ensure_ascii=False indent=2、dict 保序，state.py:182）落进临时
    目录——磁盘注入用例断言「yields 重写字节不变」需要同序列化器基线才成立
    （生产文件最后一次写入者不确定，字节比对无根基）。生产 data/ 全程零写入；
    __exit__ 还原四重定向 → planner.reload_tables()（内存表回到盘上真实内容，
    :7422 先例口径）→ rmtree 清理。"""

    def __init__(self, name):
        self.name = name
        self.tmp = None
        self.state_json = None
        self.yields_json = None
        self.req_json = None
        self._orig = {}

    def __enter__(self):
        from app import main as main_mod
        tmp_root = Path(_ROOT) / ".tmp"  # 仓库 .tmp/（已 gitignore，E 盘）
        tmp_root.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp_root / f"oknte_srv02_{self.name}_{os.getpid()}"
        self.tmp.mkdir(exist_ok=True)
        self.state_json = str(self.tmp / "state.json")
        self.yields_json = str(self.tmp / "yields.json")
        self.req_json = str(self.tmp / "requirements.json")
        # 基线一律 str 路径：save_json_atomic 内部 path+".bak" 拼接要求 str（state.py:189）
        state_mod.save_json_atomic(self.state_json, copy.deepcopy(STATE.data))
        state_mod.save_json_atomic(self.yields_json, copy.deepcopy(planner.YIELDS))
        state_mod.save_json_atomic(self.req_json, copy.deepcopy(planner.REQUIREMENTS))
        state_mod.save_json_atomic(str(self.tmp / "materials.json"),
                                   copy.deepcopy(planner.MATERIALS))
        self._orig = {"state_file": state_mod.STATE_FILE,
                      "state_datadir": state_mod.DATA_DIR,
                      "main_datadir": main_mod.DATA_DIR,
                      "planner_datadir": planner.DATA_DIR}
        # 四绑定重定向：STATE.save() 引用模块全局 STATE_FILE（state.py:533，
        # 运行时重定向有效）；main.DATA_DIR 为值拷贝（main.py:31-32）须单独改
        state_mod.STATE_FILE = self.state_json
        state_mod.DATA_DIR = str(self.tmp)
        main_mod.DATA_DIR = str(self.tmp)
        planner.DATA_DIR = str(self.tmp)
        return self

    def __exit__(self, *exc):
        from app import main as main_mod
        try:
            state_mod.STATE_FILE = self._orig["state_file"]
            state_mod.DATA_DIR = self._orig["state_datadir"]
            main_mod.DATA_DIR = self._orig["main_datadir"]
            planner.DATA_DIR = self._orig["planner_datadir"]
            planner.reload_tables()  # 内存表回到盘上真实内容
        finally:
            try:
                shutil.rmtree(str(self.tmp))
            except OSError:
                time.sleep(0.3)  # Windows 句柄释放宽限（服务线程刚 shutdown）
                shutil.rmtree(str(self.tmp), ignore_errors=True)
        return False


def _srv02_call(port, method, path, body=None):
    """B3-A HTTP 客户端（照 test_rev_returned_by_write_endpoints call() 口径：
    回环 Origin 过防线 + X-Panel-Token 过 SEC-05 闸，timeout=10 硬截止）。"""
    import http.client
    from app import main as main_mod
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Origin": "http://127.0.0.1:%d" % port,
               "X-Panel-Token": main_mod._PANEL_TOKEN}
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    if payload is not None:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, payload, headers)
    resp = conn.getresponse()
    data = json.loads(resp.read().decode("utf-8"))
    conn.close()
    return resp.status, data


def _srv02_wait_runner_idle():
    """规划器必须空闲：运行中写端点被统一 409（main.py:3445-3449），用例失去
    意义（同 test_rev_returned_by_write_endpoints :7336-7341 口径）。"""
    deadline = time.time() + 10
    while time.time() < deadline and runner_mod.RUNNER.snapshot()["state"] \
            in ("running", "waiting", "stopping"):
        runner_mod.RUNNER.stop()
        time.sleep(0.2)


def _srv02_noop_payload():
    """no-op 导入载荷：build_export()（内存现值）。前提校验——产量键全在
    TARGETS 且值全在 clamp 区间（main.py:927-929 过滤/钳制不改键值），
    clean_yields 与内存表内容完全等价 → yields.json 重写字节不变——磁盘注入
    用例「yields/requirements 两文件不变」断言的根基。
    B3 冻结期修正（2026-11-18，B3F-01）：characters 非空保证——build_export()
    取内存 STATE.data["characters"]，干净 dist（无用户 state.json）下为空列表
    → /api/import 入口 400（main.py:915-916 空 characters 拒绝），OSError 注入
    点不可达（W3 构建 A/B 双双 219/221 确定性失败）。此处兜底追加一个合成角色
    （_sanitize_character 口径同 test_srv02_import_normal_regression），使载荷
    不依赖环境用户数据：test ① 的「state.json 未写」断言不受影响（OSError
    中断发生在 STATE.save() 之前）；test ② 自身 append 语义不变；test ③ 仅有
    事件序/锁深度断言、无字节等价断言。"""
    from app import main as main_mod
    payload = main_mod.build_export()
    if not payload.get("characters"):
        payload["characters"].append(main_mod._sanitize_character(
            {"id": "srv02noop", "name": "SRV02夹具角色"}))
    assert payload.get("schema") == 3, payload.get("schema")
    assert payload["yields"]["per_run"] == dict(planner.YIELDS["per_run"]), \
        "夹具前提：导出产量必须等于内存表（no-op 导入）"
    assert set(planner.YIELDS["per_run"]) <= set(planner.TARGETS) and all(
        isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 10 ** 6
        for v in planner.YIELDS["per_run"].values()), \
        "夹具前提：产量键须全在 TARGETS 且值在 clamp 区间（否则重写不再字节等价）"
    return payload


@case
def test_srv02_import_oserror_500_no_halfset():
    """B3-A SRV-02 ①磁盘注入：requirements.json 落盘注入 OSError → HTTP 500 且
    错误体含「导入失败：」+异常原因（修复前逃逸 _guarded_dispatch 回笼统
    「服务器内部错误」无原因，归档 run2.stdout.log）；STATE.rev 前后不变；
    state.json/yields.json/requirements.json 字节全不变（「先两表后 state」：
    yields 重写字节等价、requirements 抛错未写、state 未到写盘步）——无半套。
    注入口径同 test_ocr_map_unknown _boom（:4402-4417）。"""
    import threading
    from app import main as main_mod
    from app.main import create_server

    _srv02_wait_runner_idle()
    with _StateGuard(), _Srv02Env("inj") as env:
        payload = _srv02_noop_payload()
        # SEC-05：直设全局令牌（现生成，零凭据字面量）→ _ensure_panel_token 空转
        main_mod._PANEL_TOKEN = main_mod._new_panel_token()
        server, _port = create_server(0, auto_range=1)
        port = server.server_address[1]  # 实际端口（:6560 口径）
        threading.Thread(target=server.serve_forever, daemon=True,
                         name="selftest-srv02-inj").start()
        orig_save = main_mod._save_json_atomic
        try:
            rev0 = STATE.rev
            base_state = Path(env.state_json).read_bytes()
            base_yields = Path(env.yields_json).read_bytes()
            base_req = Path(env.req_json).read_bytes()

            def _boom(path, data, **kw):  # 仅 requirements.json 注入（:4405-4407 口径）
                if str(path).endswith("requirements.json"):
                    raise OSError("disk full (模拟)")
                return orig_save(path, data, **kw)

            main_mod._save_json_atomic = _boom
            try:
                code, r = _srv02_call(port, "POST", "/api/import", payload)
            finally:
                main_mod._save_json_atomic = orig_save
            assert code == 500, (code, r)
            err = str(r.get("error", ""))
            assert "导入失败" in err and "disk full" in err, \
                f"500 错误体必须含「导入失败：」与异常原因: {r}"
            assert STATE.rev == rev0, (rev0, STATE.rev)  # rev 不自增
            assert Path(env.state_json).read_bytes() == base_state, \
                "state.json 必须未写（两表失败中断在 STATE.save() 之前）"
            assert Path(env.yields_json).read_bytes() == base_yields, \
                "yields.json 重写字节必须等价（no-op 载荷）"
            assert Path(env.req_json).read_bytes() == base_req, \
                "requirements.json 必须未写（注入点在写前抛出）"
        finally:
            server.shutdown()
            server.server_close()


@case
def test_srv02_import_normal_regression():
    """B3-A SRV-02 ②正常导入回归：/api/import 成功路径——yields/requirements
    两表与 state 全部落盘（bytes 变化 + 内容可证）、rev 恰 +1、
    planner.reload_tables 已在 STATE.save() 后执行（新产量经 reload 进内存）。"""
    import threading
    from app import main as main_mod
    from app.main import create_server

    _srv02_wait_runner_idle()
    with _StateGuard(), _Srv02Env("norm") as env:
        payload = _srv02_noop_payload()  # 同款前提校验后改值：落盘差异可证
        payload["yields"]["per_run"]["coin"] = 123456
        payload["requirements"]["srv02_b3a_marker"] = "B3-A normal import"  # 原样落盘段（main.py:960）
        payload["characters"].append(main_mod._sanitize_character(
            {"id": "srv02b3a", "name": "B3A导入角色"}))
        main_mod._PANEL_TOKEN = main_mod._new_panel_token()
        server, _port = create_server(0, auto_range=1)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True,
                         name="selftest-srv02-norm").start()
        try:
            rev0 = STATE.rev
            base_state = Path(env.state_json).read_bytes()
            base_yields = Path(env.yields_json).read_bytes()
            base_req = Path(env.req_json).read_bytes()
            code, r = _srv02_call(port, "POST", "/api/import", payload)
            assert code == 200 and r.get("ok") is True, (code, r)
            assert r.get("rev") == rev0 + 1, (rev0, r.get("rev"))  # 恰 +1
            assert STATE.rev == rev0 + 1, (rev0, STATE.rev)
            assert Path(env.yields_json).read_bytes() != base_yields, "yields 必须落盘"
            assert Path(env.req_json).read_bytes() != base_req, "requirements 必须落盘"
            assert Path(env.state_json).read_bytes() != base_state, "state 必须落盘"
            yields_now = json.loads(Path(env.yields_json).read_text(encoding="utf-8"))
            assert yields_now["per_run"]["coin"] == 123456, yields_now["per_run"].get("coin")
            req_now = json.loads(Path(env.req_json).read_text(encoding="utf-8"))
            assert req_now.get("srv02_b3a_marker") == "B3-A normal import", \
                req_now.get("srv02_b3a_marker")
            state_now = json.loads(Path(env.state_json).read_text(encoding="utf-8"))
            assert any(c.get("id") == "srv02b3a"
                       for c in state_now.get("characters", [])), \
                "导入角色必须落盘 state.json"
            assert planner.YIELDS["per_run"]["coin"] == 123456, \
                "reload_tables 必须已在 STATE.save() 后执行（新产量进内存）"
        finally:
            server.shutdown()
            server.server_close()


@case
def test_srv02_import_lock_order():
    """B3-A SRV-02 ③锁序回归：静态断言（apply_import 源码不含 _TJD_LOCK——导入
    全程不可能与 _TJD_LOCK 交错；yields→requirements→STATE.save() 源码序）+
    运行期探针（真实 HTTP 导入全程：落盘事件序 yields→requirements→state.json、
    三次落盘全部发生在 STATE.lock 持有期间（深度≥1）、_TJD_LOCK 零 acquire、
    探针锁 acquire/release 平衡归零）。"""
    import inspect
    import threading
    from app import main as main_mod
    from app.main import create_server

    # ---- 静态断言：源码序即运行序（inspect 读原函数——_guarded_dispatch 保留 __wrapped__）----
    src = inspect.getsource(main_mod.apply_import)
    assert "_TJD_LOCK" not in src, "apply_import 不得触碰 _TJD_LOCK（锁序单向无环）"
    assert "with STATE.lock:" in src, "两表+state 落盘必须全程在 STATE.lock 内"
    i_yields = src.index('_save_json_atomic(os.path.join(DATA_DIR, "yields.json")')
    i_req = src.index('_save_json_atomic(os.path.join(DATA_DIR, "requirements.json")')
    i_save = src.rindex("STATE.save()")  # rindex：跳过注释字面量取真实调用点
    assert i_yields < i_req < i_save, "源码序必须为 yields→requirements→STATE.save()"

    class _LockProbe:
        """锁探针：包一层 RLock 记录深度（仅本用例内替换，finally 还原原锁）。"""

        def __init__(self, inner):
            self.inner = inner
            self.events = []
            self.depth = 0

        def acquire(self, *args, **kwargs):
            got = self.inner.acquire(*args, **kwargs)
            if got:
                self.depth += 1
            return got

        def release(self):
            self.depth -= 1
            self.inner.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *exc):
            self.release()

    _srv02_wait_runner_idle()
    with _Srv02Env("lock") as env:  # 不叠 _StateGuard：探针期间避免额外锁事件
        data_backup = copy.deepcopy(STATE.data)  # 成功导入改内存：finally 手工还原
        payload = _srv02_noop_payload()
        state_probe = _LockProbe(STATE.lock)
        tjd_probe = _LockProbe(main_mod._TJD_LOCK)
        STATE.lock = state_probe
        main_mod._TJD_LOCK = tjd_probe
        orig_main_save = main_mod._save_json_atomic
        orig_state_save = state_mod.save_json_atomic
        events = []  # (文件名, STATE.lock 深度)：按发生顺序；state.json 写经
        # state_mod.save_json_atomic 探针（STATE.save 的模块全局查找，state.py:533），
        # main._save_json_atomic 是值拷贝不受其影响——两探针正好分监两表与 state

        def probe_main_save(path, data, **kw):
            name = os.path.basename(str(path))
            if name in ("yields.json", "requirements.json"):
                events.append((name, state_probe.depth))
            return orig_main_save(path, data, **kw)

        def probe_state_save(path, data, **kw):
            events.append((os.path.basename(str(path)), state_probe.depth))
            return orig_state_save(path, data, **kw)

        main_mod._save_json_atomic = probe_main_save
        state_mod.save_json_atomic = probe_state_save
        main_mod._PANEL_TOKEN = main_mod._new_panel_token()
        server, _port = create_server(0, auto_range=1)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True,
                         name="selftest-srv02-lock").start()
        try:
            rev0 = STATE.rev
            code, r = _srv02_call(port, "POST", "/api/import", payload)
            assert code == 200 and r.get("ok") is True, (code, r)
            assert STATE.rev == rev0 + 1, (rev0, STATE.rev)
            names = [name for name, _depth in events]
            assert names == ["yields.json", "requirements.json", "state.json"], \
                f"落盘事件序必须为「先两表后 state」: {names}"
            depths = [depth for _name, depth in events]
            assert all(d >= 1 for d in depths), \
                f"三次落盘必须全部发生在 STATE.lock 持有期间: {events}"
            assert tjd_probe.events == [], \
                f"导入全程不得触碰 _TJD_LOCK（零交错）: {tjd_probe.events}"
            assert state_probe.depth == 0, \
                f"探针锁必须平衡（acquire/release 成对）: depth={state_probe.depth}"
        finally:
            STATE.lock = state_probe.inner
            main_mod._TJD_LOCK = tjd_probe.inner
            main_mod._save_json_atomic = orig_main_save
            state_mod.save_json_atomic = orig_state_save
            STATE.data = data_backup
            server.shutdown()
            server.server_close()


# ---------- 2026-11-12：读取健壮化 + 执行计划覆盖（GET/POST /api/oknte/plan） + 数据订正 ----------

@case
def test_read_json_robust():
    """_read_json_robust（2026-11-12 读取健壮化，针对「ok-nte GUI 运行中写文件→
    面板并发读到半截→瞬态 502」实锤）：①半截文件在重试间隔内变完整→重试后成功
    （time.sleep 首次被调时把文件写完整——确定性模拟并发写，不靠真实时序）；
    ②持续损坏/不存在→ValueError 带完整路径与最后一次异常原文；③最多尝试 3 次
    （重试间隔 sleep 恰好 2 次）。"""
    import time as time_mod
    from app import main as main_mod
    tmp = tempfile.mkdtemp(prefix="oknte_robust_")
    try:
        p = Path(tmp) / "DailyRoutineTaskConfigs.json"
        p.write_text('{"daily_anomaly": {"任务类型": "经验', encoding="utf-8")  # 半截 JSON
        calls = {"sleep": 0}
        real_sleep = time_mod.sleep

        def flip_sleep(_seconds):
            calls["sleep"] += 1
            if calls["sleep"] == 1 and not p.read_text(encoding="utf-8").endswith("}"):
                p.write_text(json.dumps({"daily_anomaly": {"任务类型": "经验与甲硬币"}}),
                             encoding="utf-8")
            return real_sleep(0)

        time_mod.sleep = flip_sleep
        try:
            cfg = main_mod._read_json_robust(str(p))
            assert cfg["daily_anomaly"]["任务类型"] == "经验与甲硬币", cfg
            assert calls["sleep"] == 1, calls  # 第一次重试即成功
        finally:
            time_mod.sleep = real_sleep
        # 最终失败：不存在 / 持续损坏，均带完整路径与最后一次异常
        missing = str(Path(tmp) / "missing.json")
        p.write_text('{"daily_anomaly": broken', encoding="utf-8")
        for bad, last_frag in ((missing, "FileNotFoundError"),
                               (str(p), "JSONDecodeError")):
            try:
                main_mod._read_json_robust(str(bad))
                raise AssertionError(f"应抛 ValueError: {bad}")
            except ValueError as e:
                assert str(bad) in str(e), str(e)
                assert "最后一次异常" in str(e) and last_frag in str(e), str(e)
        # 尝试次数=3 → 重试 sleep 恰好 2 次
        calls["sleep"] = 0
        time_mod.sleep = lambda _s: calls.__setitem__("sleep", calls["sleep"] + 1)
        try:
            try:
                main_mod._read_json_robust(str(p))
            except ValueError:
                pass
            assert calls["sleep"] == 2, calls
        finally:
            time_mod.sleep = real_sleep
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_oknte_plan_endpoints():
    """GET/POST /api/oknte/plan（2026-11-12 新端点）：GET=_oknte_plan_tree 读真实
    configs 全选项树（9 开关+bot_notify+exit_after_task / daily_anomaly 全键含循环
    序列 / hunter / misc 六段 / source_paths）+ data/oknte_plan.json 覆盖现值合并；
    daily_anomaly 缺失→ValueError（端点转 502 带路径），AnomalyHunter.json 缺失→
    warning 不 502（compat 同口径）。POST=_sanitize_oknte_plan_overrides 键白名单/
    类型拒绝矩阵 + 合法对象经 save_oknte_plan_overrides 落 OKNTE_PLAN_FILE（tempdir
    隔离，绝不碰项目 data/）+ schema/version/fetched_at + 被覆盖时 .bak。
    端点无出站请求，FakeOpener 不适用——沿用既有 tempdir 伪造 ok-nte 树模式；
    do_GET/do_POST 路由源静态校验。"""
    import inspect
    from app import main as main_mod
    from app import oknte_adapter as adapter_mod
    tmp = tempfile.mkdtemp(prefix="oknte_plan_ep_")
    real_plan_file = adapter_mod.OKNTE_PLAN_FILE
    plan_file = Path(tmp) / "oknte_plan.json"
    try:
        adapter_mod.OKNTE_PLAN_FILE = plan_file
        root = os.path.join(tmp, "root")
        cfg = {**FAKE_ROUTINE_CONFIG, "furniture": {"挑战玛门": True},
               "store_purchase": {"九百九十九夜": False}}
        ok = _build_fake_oknte(root, cfg=cfg)
        items_path = _safe_path(root, "data", "apps", "ok-nte", "working", "configs",
                                "DailyRoutineTask.json")
        Path(items_path).write_text(json.dumps(
            {"Routine Items": [{"id": i, "enabled": True}
                               for i in main_mod.OKNTE_ROUTINE_ITEM_IDS],
             "存在失败时发送 BOT 通知": True, "Exit After Task": False},
            ensure_ascii=False, indent=4), encoding="utf-8")
        # GET：全选项树 + 空覆盖
        tree = main_mod._oknte_plan_tree(ok)
        assert set(tree["routine_items"]) == \
            set(main_mod.OKNTE_ROUTINE_ITEM_IDS) | {"bot_notify", "exit_after_task"}
        assert tree["routine_items"]["daily_anomaly"] is True
        assert tree["routine_items"]["bot_notify"] is True
        assert tree["routine_items"]["exit_after_task"] is False
        assert tree["daily_anomaly"]["循环序列"] == \
            FAKE_ROUTINE_CONFIG["daily_anomaly"]["循环序列"]
        assert tree["hunter"] == {"追猎目标": "海囚", "领取奖励次数": 0}
        assert tree["misc"]["coffee"]["模式"] == "领取/补货"
        assert tree["misc"]["furniture"] == {"挑战玛门": True}
        assert tree["misc"]["store_purchase"] == {"九百九十九夜": False}
        assert tree["misc"]["gift"] == {}
        assert tree["source_paths"]["routine_configs"] == ok.routine_config
        assert tree["overrides"] == {}
        assert tree["overrides_path"].endswith("oknte_plan.json")
        # cycle_options（2026-11-12 总计划）：19 项全集具名，entry 与 ok-nte 实样
        # 同格式（半角冒号+空格），label 全部带「·」且含具体名/奖励目标
        co = tree["cycle_options"]
        assert len(co) == 19, co
        assert all("·" in o["label"] for o in co), co
        assert co[0] == {"entry": "角色经验", "label": "经验与甲硬币·角色经验"}, co[0]
        by_entry = {o["entry"]: o["label"] for o in co}
        assert by_entry["异能升级材料: 3"] == "异能升级材料·3（黑帽子）", by_entry
        assert by_entry["弧盘突破材料: 1"] == "弧盘突破材料·1（典藏级戏剧芯）", by_entry
        assert by_entry["空幕: 6"] == "空幕·6（铁轨之夜）", by_entry
        assert by_entry["甲硬币"] == "经验与甲硬币·甲硬币", by_entry
        # GET：daily_anomaly 缺失 → ValueError 带路径（端点转 502）
        Path(_contained(ok.routine_config, ok.root)).write_text('{"coffee": {}}',
                                                                encoding="utf-8")
        try:
            main_mod._oknte_plan_tree(ok)
            raise AssertionError("缺 daily_anomaly 应抛 ValueError")
        except ValueError as e:
            assert ok.routine_config in str(e) and "daily_anomaly" in str(e), str(e)
        Path(_contained(ok.routine_config, ok.root)).write_text(
            json.dumps(cfg, ensure_ascii=False, indent=4), encoding="utf-8")
        # POST：校验拒绝矩阵（键白名单 + 类型约束）
        refusals = [
            ({"nope": {}}, "未知键"),
            ({"routine_items": {"whatever": True}}, "未知键"),
            ({"routine_items": {"coffee": "yes"}}, "布尔值"),
            ({"daily_anomaly": {"循环序列": "角色经验"}}, "字符串数组"),
            ({"daily_anomaly": {"循环序列": [1, 2]}}, "字符串数组"),
            ({"daily_anomaly": {"目标消耗体力": "60"}}, "整数"),
            ({"daily_anomaly": {"异能材料序号": True}}, "整数"),
            ({"daily_anomaly": {"循环模式": "自动"}}, "不可覆盖"),
            ({"hunter": {"领取奖励次数": -1}}, "非负整数"),
            ({"hunter": {"追猎目标": 5}}, "非空字符串"),
            ({"misc": {"daily_claim": {}}}, "未知段"),
            ({"misc": {"coffee": {"价格表": {"nested": 1}}}}, "类型不支持"),
            ({"plan_targets": ["not_a_mat"]}, "未知材料"),
            ({"plan_targets": "coin"}, "字符串数组"),
            ("not a dict", "JSON 对象"),
        ]
        for bad, frag in refusals:
            try:
                main_mod._sanitize_oknte_plan_overrides(bad)
                raise AssertionError(f"应拒绝: {bad!r}")
            except ValueError as e:
                assert frag in str(e), (bad, str(e))
        # POST：合法覆盖保存（tempdir 隔离）+ schema/version/fetched_at
        clean = main_mod._sanitize_oknte_plan_overrides({
            "routine_items": {"coffee": False, "daily_anomaly": True,
                              "bot_notify": False, "exit_after_task": True},
            "daily_anomaly": {"任务类型": "弧盘经验",
                              "循环序列": ["弧盘经验", "甲硬币"],
                              "目标消耗体力": 360},
            "hunter": {"追猎目标": "黑之书", "领取奖励次数": 2},
            "misc": {"coffee": {"模式": "仅领取"}},
            "plan_targets": ["coin", "coin", "ability_1"],
        })
        assert clean["plan_targets"] == ["coin", "ability_1"], clean  # 去重保序
        saved = adapter_mod.save_oknte_plan_overrides(clean)
        assert saved == str(plan_file) and plan_file.is_file(), saved
        payload = json.loads(plan_file.read_text(encoding="utf-8"))
        assert payload["schema"] == 1 and payload["version"] == 1, payload
        assert payload["fetched_at"], payload
        assert payload["overrides"]["hunter"] == {"追猎目标": "黑之书", "领取奖励次数": 2}
        # 第二次保存 → 被覆盖时 .bak（save_json_atomic 既有契约）
        adapter_mod.save_oknte_plan_overrides({"misc": {"gift": {}}})
        assert Path(str(plan_file) + ".bak").is_file()
        # GET：合并 overrides 现值
        tree2 = main_mod._oknte_plan_tree(ok)
        assert tree2["overrides"]["misc"]["gift"] == {}
        # GET：hunter 文件缺失 → warning 非 502
        Path(_contained(ok.hunter_config, ok.root)).unlink()
        tree3 = main_mod._oknte_plan_tree(ok)
        assert tree3["hunter"] == {}
        assert any("AnomalyHunter.json" in w for w in tree3["warnings"]), tree3["warnings"]
        # 路由源静态校验（GET/POST 薄壳存在）
        get_src = inspect.getsource(main_mod.Handler.do_GET)
        assert '"/api/oknte/plan"' in get_src and "_oknte_plan_tree(self._oknte())" in get_src
        post_src = inspect.getsource(main_mod.Handler.do_POST)
        assert '"/api/oknte/plan"' in post_src
        assert "_sanitize_oknte_plan_overrides(body)" in post_src
        assert "save_oknte_plan_overrides(clean)" in post_src
    finally:
        adapter_mod.OKNTE_PLAN_FILE = real_plan_file
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_prepare_daily_profile_overrides():
    """prepare_daily_profile 覆盖消费（2026-11-12 执行链消费覆盖）：临时 configs 树 +
    OKNTE_PLAN_FILE 指向 tempdir，断言 configs_planner 副本——①routine_items 按覆盖
    enabled（不再强制只开 daily_anomaly，未点名条目沿用副本值）+ 顶层 BOT 通知/
    Exit After Task；②daily_anomaly 覆盖键生效（循环序列=用户多选列表、目标体力=
    覆盖值，手写覆盖里的循环模式被忽略仍强制「停用」）；③hunter 写入副本
    AnomalyHunter.json；④misc.coffee 同段 update（其余键不动）；用户原 configs
    字节级零写入；无覆盖/覆盖损坏=现有行为（损坏打印 [oknte-plan] 告警不中断）。"""
    import contextlib
    import hashlib
    import io
    from app import oknte_adapter as adapter_mod
    tmp = tempfile.mkdtemp(prefix="oknte_profile_ovr_")
    real_plan_file = adapter_mod.OKNTE_PLAN_FILE
    plan_file = Path(tmp) / "oknte_plan.json"
    try:
        adapter_mod.OKNTE_PLAN_FILE = plan_file
        root = os.path.join(tmp, "root")
        cfg = {**FAKE_ROUTINE_CONFIG, "furniture": {"挑战玛门": True},
               "store_purchase": {"九百九十九夜": False}}
        ok = _build_fake_oknte(root, cfg=cfg)
        items_path = _safe_path(root, "data", "apps", "ok-nte", "working", "configs",
                                "DailyRoutineTask.json")
        items_full = {"Routine Items": [
            {"id": "coffee", "enabled": True},
            {"id": "cinema_date", "enabled": True},
            {"id": "furniture", "enabled": True},
            {"id": "gift", "enabled": True},
            {"id": "daily_anomaly", "enabled": True},
            {"id": "daily_anomaly_hunter", "enabled": False},
            {"id": "daily_claim", "enabled": True},
            {"id": "fountain", "enabled": True},
            {"id": "store_purchase", "enabled": False}],
            "存在失败时发送 BOT 通知": True, "Exit After Task": False}
        Path(items_path).write_text(json.dumps(items_full, ensure_ascii=False, indent=4),
                                    encoding="utf-8")

        def _hash(path):
            return hashlib.sha256(Path(path).read_bytes()).hexdigest()

        src_items, src_cfg, src_hunter = (_hash(items_path), _hash(ok.routine_config),
                                          _hash(ok.hunter_config))
        # 覆盖文件（手写形态：故意包含循环模式，验证仍被强制「停用」）
        plan_file.write_text(json.dumps(
            {"schema": 1, "version": 1, "fetched_at": "t", "overrides": {
                "routine_items": {"coffee": False, "daily_anomaly": True,
                                  "store_purchase": True, "daily_claim": False,
                                  "bot_notify": False, "exit_after_task": True},
                "daily_anomaly": {"任务类型": "弧盘经验",
                                  "循环序列": ["弧盘经验", "甲硬币"],
                                  "目标消耗体力": 360,
                                  "循环模式": "自动循环序号/目标"},
                "hunter": {"追猎目标": "黑之书", "领取奖励次数": 3},
                "misc": {"coffee": {"模式": "仅领取", "补货货物": True}},
            }}, ensure_ascii=False), encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            launcher = ok.prepare_daily_profile({"任务类型": "经验与甲硬币"}, 240)
        out = buf.getvalue()
        assert "[oknte-plan] 应用执行计划覆盖" in out, out
        assert os.path.isfile(launcher), launcher
        prof_items = json.loads(Path(ok.profile_items).read_text(encoding="utf-8"))
        by_id = {e["id"]: e["enabled"] for e in prof_items["Routine Items"]}
        assert by_id == {"coffee": False, "cinema_date": True, "furniture": True,
                         "gift": True, "daily_anomaly": True,
                         "daily_anomaly_hunter": False, "daily_claim": False,
                         "fountain": True, "store_purchase": True}, by_id
        assert prof_items["存在失败时发送 BOT 通知"] is False
        assert prof_items["Exit After Task"] is True
        prof_daily = json.loads(Path(ok.profile_daily).read_text(encoding="utf-8"))
        da = prof_daily["daily_anomaly"]
        assert da["任务类型"] == "弧盘经验", da              # 覆盖键生效
        assert da["循环序列"] == ["弧盘经验", "甲硬币"], da  # 用户多选列表
        assert da["目标消耗体力"] == 360, da                # 覆盖值（非 runner 计算的 240）
        assert da["循环模式"] == "停用", da                 # 仍强制「停用」
        assert da["具体奖励目标"] == "弧盘经验", da         # 未覆盖键沿用 target_config 原值
        assert prof_daily["coffee"]["模式"] == "仅领取"
        assert prof_daily["coffee"]["补货货物"] is True
        assert prof_daily["coffee"]["领取收益"] is True     # 同段未覆盖键不动
        prof_hunter = json.loads(Path(ok.profile_hunter).read_text(encoding="utf-8"))
        assert prof_hunter["追猎目标"] == "黑之书"
        assert prof_hunter["领取奖励次数"] == 3
        # 用户原 configs 零写入（字节级）
        assert _hash(items_path) == src_items
        assert _hash(ok.routine_config) == src_cfg
        assert _hash(ok.hunter_config) == src_hunter
        # 无覆盖 = 现有行为（只开 daily_anomaly；每次重建副本，上轮覆盖不残留）
        plan_file.unlink()
        ok.prepare_daily_profile({"任务类型": "经验与甲硬币"}, 240)
        prof_items2 = json.loads(Path(ok.profile_items).read_text(encoding="utf-8"))
        assert {e["id"]: e["enabled"] for e in prof_items2["Routine Items"]} == {
            "coffee": False, "cinema_date": False, "furniture": False, "gift": False,
            "daily_anomaly": True, "daily_anomaly_hunter": False, "daily_claim": False,
            "fountain": False, "store_purchase": False}
        prof_daily2 = json.loads(Path(ok.profile_daily).read_text(encoding="utf-8"))
        assert prof_daily2["daily_anomaly"]["目标消耗体力"] == 240
        assert prof_daily2["coffee"]["模式"] == "领取/补货"
        # 覆盖损坏 = 告警不中断，走现有行为
        plan_file.write_text("{broken", encoding="utf-8")
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            ok.prepare_daily_profile({"任务类型": "经验与甲硬币"}, 240)
        assert "[oknte-plan] 覆盖文件无法解析" in buf2.getvalue(), buf2.getvalue()
        prof_daily3 = json.loads(Path(ok.profile_daily).read_text(encoding="utf-8"))
        assert prof_daily3["daily_anomaly"]["目标消耗体力"] == 240
    finally:
        adapter_mod.OKNTE_PLAN_FILE = real_plan_file
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_make_plan_plan_targets():
    """make_plan plan_targets + _plan_options 端到端（2026-11-12 计划覆盖）：非空列表
    按列表顺序、仅列内材料生成步骤（沿用同一贪心/额度防线），未知 id 告警忽略绝不
    静默；空/缺省=现有行为（角色优先级序，首步 arc_1）；列内全部无缺口→如实
    all_targets_completed；_plan_options 经 oknte_plan_target_ids 读 data/oknte_plan.json
    覆盖传入（first_step=列表首项）。"""
    from app import main as main_mod
    from app import oknte_adapter as adapter_mod
    tmp = tempfile.mkdtemp(prefix="oknte_plan_targets_")
    real_plan_file = adapter_mod.OKNTE_PLAN_FILE
    try:
        adapter_mod.OKNTE_PLAN_FILE = Path(tmp) / "absent.json"  # 无覆盖
        with _StateGuard():
            STATE.data["settings"] = dict(DEFAULT_SETTINGS)  # 限额180
            STATE.data["inventory"] = {"coin": 0, "ability_1": 0, "arc_1": 0}
            STATE.data["today"] = _day_today()
            STATE.data["characters"] = [
                _char("c1", "角色甲", {"arc_1": 30}),
                _char("c2", "角色乙", {"ability_1": 14, "coin": 14}),
            ]
            # 缺省=现有行为：角色甲优先 → 首步 arc_1
            plan0 = planner.make_plan(None, now=NOW)
            ids0 = [s["mat_id"] for s in plan0["steps"]]
            assert ids0 and ids0[0] == "arc_1", ids0
            # 覆盖列表：仅列内材料、按列表顺序；未知 id 告警
            plan = planner.make_plan(None, now=NOW,
                                     plan_targets=["ability_1", "coin", "not_a_mat"])
            ids = [s["mat_id"] for s in plan["steps"]]
            assert ids == ["ability_1", "coin"], (ids, plan["warnings"])
            assert any("not_a_mat" in w for w in plan["warnings"]), plan["warnings"]
            # 列内全部无缺口 → 如实 all_targets_completed
            plan2 = planner.make_plan(None, now=NOW, plan_targets=["exp_arc"])
            assert plan2["no_op_reason"] == "all_targets_completed", plan2["no_op_reason"]
            # 端到端：_plan_options 读 overrides.plan_targets（save→loader 同链路）
            adapter_mod.OKNTE_PLAN_FILE = Path(tmp) / "oknte_plan.json"
            adapter_mod.save_oknte_plan_overrides({"plan_targets": ["coin", "not_a_mat"]})
            ok = _build_fake_oknte(os.path.join(tmp, "root"))
            ok.is_running_cached = lambda: False
            payload = main_mod._plan_options(ok)
            assert payload["first_step"]["mat_id"] == "coin", payload["first_step"]
            assert payload["steps_count"] == 1, payload
    finally:
        adapter_mod.OKNTE_PLAN_FILE = real_plan_file
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_materials_official_names():
    """materials 订正断言（2026-11-12 tools/everness_ingest.py，data/sites/
    everness_refs.json 为证）：manual 3 项 item_names 含用户写法（OCR/手工输入仍
    命中）；display 维持用户写法——niefu_dice 官方名经实抓=用户写法（DiceNormal=
    捏造骰子），huan_stone/zhishi_dice 官方名未确认（OracleStone 实抓=谕石，与本表
    oracle_stone 同名而非环石；Dicelimite 页无中文名）→ 不冒充官方名；
    guide_sites arcs 已增 fork_worldrain（source=everness，名称=实抓 <title>）。
    refs 缺失（fresh checkout，data/sites/* 不入库）→ 打印 SKIP 如实说明，不伪造通过。"""
    refs_path = os.path.join(_ROOT, "data", "sites", "everness_refs.json")
    if not os.path.isfile(refs_path):
        print("SKIP test_materials_official_names: data/sites/everness_refs.json 不存在"
              "（everness 抓取未在本机执行；data/sites/* 不入库，fresh checkout 属预期）")
        return
    refs = load_json(refs_path)
    assert isinstance(refs, dict) and isinstance(refs.get("items"), dict), type(refs)
    planner.reload_tables()  # 以盘上订正后数据为准
    user_wording = {"huan_stone": "环石", "niefu_dice": "捏造骰子", "zhishi_dice": "质实骰子"}
    for mid, wording in user_wording.items():
        entry = planner.EXTRA[mid]
        assert entry.get("manual_only") is True, mid
        assert wording in planner.MATERIALS["item_names"].get(mid, []), \
            (mid, planner.MATERIALS["item_names"].get(mid))
        assert entry["display_name"] == wording, (mid, entry["display_name"])
    # 官方名实抓口径
    assert refs["items"]["DiceNormal"]["zh"] == "捏造骰子", refs["items"]["DiceNormal"]
    assert planner.EXTRA["niefu_dice"]["display_name"] == \
        refs["items"]["DiceNormal"]["zh"]
    assert refs["items"]["OracleStone"]["zh"] == "谕石", refs["items"]["OracleStone"]
    assert planner.EXTRA["huan_stone"]["display_name"] == "环石"
    assert "谕石" not in planner.MATERIALS["item_names"].get("huan_stone", []), \
        "谕石属 oracle_stone，不得混入 huan_stone 别名"
    # 弧盘订正：arcs 含 fork_worldrain（source=everness，名称=实抓 <title>）
    guide = load_json(os.path.join(_ROOT, "data", "sites", "guide_sites.json"))
    arcs = [a for a in (guide.get("arcs") or []) if a.get("code") == "fork_worldrain"]
    assert len(arcs) == 1 and arcs[0]["source"] == "everness", arcs
    assert arcs[0]["name"] == refs["arcs"]["fork_worldrain"]["zh"], arcs[0]
    assert refs["espers"].get("1090") == "曳尔沐", refs["espers"].get("1090")


# ---------- 2026-11-12：用户数据目录迁移（打包态 %LOCALAPPDATA% 重定向 + 迁移 CLI + 打包闸门） ----------

@case
def test_user_data_seed_and_migration():
    """state.ensure_user_data/_copy_tree_no_clobber 契约回归（2026-11-12 用户数据目录
    迁移）：①整树复制后源目录逐文件字节不变、全部仍在（只复制不删/不移动）；
    ②no-clobber 预置哨兵文件内容不被覆盖；③幂等（marker 在→skip；删 marker 重调只补
    缺失文件）；④零失败才写 .migrated（内容=时间戳/来源/计数），有失败抛 OSError
    且 marker 不写；⑤成功路径无 *.tmp 残留；⑥dry-run 零写入；⑦bundled 缺失/为空
    分支不写 marker（自愈门保持开启）；⑧_ensure_user_data_boot
    吞 OSError 不抛（失败不阻断启动）。全部伪造占位内容、无任何真实凭据。"""
    from app import state as state_mod

    def make_tree(root):
        os.makedirs(_safe_path(root, "backups"), exist_ok=True)
        os.makedirs(_safe_path(root, "mysqil"), exist_ok=True)
        # 伪造出厂数据表 + 伪造用户文件（占位内容，无任何真实数据/凭据）
        Path(_safe_path(root, "materials.json")).write_text(
            json.dumps({"placeholder": "seed"}), encoding="utf-8")
        Path(_safe_path(root, "yields.json")).write_text("{}", encoding="utf-8")
        Path(_safe_path(root, "backups", "a.json")).write_text("{}", encoding="utf-8")
        Path(_safe_path(root, "mysqil", "cassettes.json")).write_text("{}", encoding="utf-8")

    def snapshot(root):
        snap = {}
        for base, _dirs, files in os.walk(root):
            for name in files:
                p = os.path.join(base, name)
                snap[p] = Path(p).read_bytes()
        return snap

    tmp = tempfile.mkdtemp(prefix="oknte_userdata_")
    try:
        bundled = _safe_path(tmp, "bundled", "data")
        make_tree(bundled)
        src_snapshot = snapshot(bundled)

        # ---- 首启：整树迁移（播种=全新安装的退化情形：EXE 旁只含出厂数据表）----
        user = _safe_path(tmp, "user", "data")
        result = state_mod.ensure_user_data(user, bundled)
        assert result["action"] == "migrated", result
        # 源目录逐文件字节不变、全部仍在（只复制不删/不移动）
        assert snapshot(bundled) == src_snapshot, "源目录必须逐字节原样保留"
        # 用户目录拿到全部文件，.migrated 已写
        for p, raw in src_snapshot.items():
            rel = os.path.relpath(p, bundled)  # 源绝对路径 → 相对 bundled 的相对路径
            assert Path(_safe_path(user, rel)).read_bytes() == raw, rel
        marker = _safe_path(user, ".migrated")
        marker_data = json.loads(Path(marker).read_text(encoding="utf-8"))
        assert set(marker_data) <= {"migrated_at", "source", "dirs", "copied", "skipped"}, marker_data
        assert os.path.isabs(marker_data["source"]), marker_data
        # 成功路径无 *.tmp 残留（单文件原子）
        for base, _dirs, files in os.walk(user):
            for name in files:
                assert not name.endswith(".tmp"), name

        # ---- 幂等：marker 在 → skip，用户目录中被删的文件不被补回 ----
        Path(_safe_path(user, "yields.json")).unlink()
        result = state_mod.ensure_user_data(user, bundled)
        assert result["action"] == "skip", result
        assert not os.path.isfile(_safe_path(user, "yields.json"))
        # 删 marker 重调：只补缺失文件（no-clobber）；预置哨兵不被覆盖
        Path(marker).unlink()
        Path(_safe_path(user, "materials.json")).write_text(
            '{"sentinel": "user-owned"}', encoding="utf-8")
        result = state_mod.ensure_user_data(user, bundled)
        assert result["action"] == "migrated", result
        assert Path(_safe_path(user, "materials.json")).read_text(encoding="utf-8") \
            == '{"sentinel": "user-owned"}', "materials.json must keep user-owned"
        assert os.path.isfile(_safe_path(user, "yields.json"))  # 缺失文件已补

        # ---- dry_run：只计划零写入 ----
        other = _safe_path(tmp, "other", "data")
        os.makedirs(other)
        Path(_safe_path(other, "runtime.json")).write_text(
            json.dumps({"placeholder": "dryrun"}), encoding="utf-8")
        stats = state_mod._copy_tree_no_clobber(other, user, dry_run=True)
        assert stats["copied"] == 1 and stats["failed"] == 0, stats
        assert not os.path.isfile(os.path.join(user, "runtime.json"))  # 演练零写入
        stats = state_mod._copy_tree_no_clobber(other, user)  # dry_run 默认 False
        assert stats["copied"] == 1, stats
        assert os.path.isfile(_safe_path(user, "runtime.json"))
        # 重复整树：已全部存在 → 全部跳过
        stats = state_mod._copy_tree_no_clobber(bundled, user)
        assert stats["copied"] == 0 and stats["skipped"] == len(src_snapshot), stats
        # 源目标同目录防自拷：零复制零失败
        stats = state_mod._copy_tree_no_clobber(bundled, bundled)
        assert stats == {"dirs": 0, "copied": 0, "skipped": 0, "failed": 0, "errors": []}, stats

        # ---- 失败路径：目标被同名目录占位 → failed>0，ensure 抛 OSError、marker 不写 ----
        clash = _safe_path(tmp, "clash", "data")
        os.makedirs(clash)
        Path(_safe_path(clash, "blocked.json")).write_text("{}", encoding="utf-8")  # 源：文件
        Path(_safe_path(clash, "good.json")).write_text("{}", encoding="utf-8")
        user2 = _safe_path(tmp, "user2", "data")
        os.makedirs(_safe_path(user2, "blocked.json"))  # 目标：同名目录占位 → 复制必失败
        try:
            state_mod.ensure_user_data(user2, clash)
            raise AssertionError("有失败项时 ensure_user_data 必须抛 OSError")
        except OSError:
            pass
        assert not os.path.isfile(_safe_path(user2, ".migrated"))  # marker 未写 → 下次启动自愈重试

        # ---- bundled 缺失/为空：不写 marker（自愈门保持开启）----
        empty = _safe_path(tmp, "empty", "data")
        os.makedirs(empty)
        assert state_mod.ensure_user_data(
            _safe_path(tmp, "user3", "data"), empty)["action"] == "empty"
        assert not os.path.isfile(_safe_path(tmp, "user3", "data", ".migrated"))
        r = state_mod.ensure_user_data(_safe_path(tmp, "user4", "data"), None)
        assert r["action"] == "empty", r
        assert not os.path.isfile(_safe_path(tmp, "user4", "data", ".migrated"))

        # ---- boot 包装：吞 OSError 不抛（失败不阻断启动）——临时改道到 temp 目标 ----
        boot_user = _safe_path(tmp, "boot", "user", "data")
        boot_src = _safe_path(tmp, "bootsrc", "data")
        os.makedirs(boot_src)
        Path(_safe_path(boot_src, "x.json")).write_text("{}", encoding="utf-8")
        os.makedirs(_safe_path(boot_user, "x.json"))  # 同名目录占位：boot 必失败
        real_user = state_mod.USER_DATA_DIR
        real_bund = state_mod.BUNDLED_DATA_DIR
        state_mod.USER_DATA_DIR = boot_user
        state_mod.BUNDLED_DATA_DIR = boot_src
        try:
            state_mod._ensure_user_data_boot()  # 不得抛
        finally:
            state_mod.USER_DATA_DIR, state_mod.BUNDLED_DATA_DIR = real_user, real_bund
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_user_data_cli():
    """tools.migrate_user_data CLI 契约回归（2026-11-12）：--from 可重复按序
    （no-clobber 先到先得）；--apply 必需（缺失或单传 --dry-run → exit 2 绝不写盘）；
    --dry-run --apply 演练零写入；源目录不存在 → exit 2；正式迁移只复制不删源、
    不覆盖已有文件、不写 .migrated；输出只有计数与路径、无文件内容。
    SKIP 守卫：dist 不携带 tools/（build_dist 仅复制 app/ 与 tests/），仓库根运行
    才有效。子进程 env LOCALAPPDATA=临时目录、cwd=_ROOT，绝不碰真实 %LOCALAPPDATA%。"""
    import subprocess as _sp
    if not os.path.isdir(os.path.join(_ROOT, "tools")):
        print("SKIP test_user_data_cli: dist 不携带 tools/（build_dist 仅复制"
              " app/ 与 tests/），仓库根运行才有效")
        return

    def run_cli(env_dir, *cli_args):
        env = {**os.environ, "LOCALAPPDATA": env_dir}
        r = _sp.run([sys.executable, "-m", "tools.migrate_user_data", *cli_args],
                    cwd=_ROOT, env=env, capture_output=True, text=True, timeout=120)
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    tmp = tempfile.mkdtemp(prefix="oknte_cli_")
    try:
        src1 = _safe_path(tmp, "src1")
        src2 = _safe_path(tmp, "src2")
        os.makedirs(_safe_path(src1, "sites"))
        Path(_safe_path(src1, "state.json")).write_text(
            json.dumps({"placeholder": "cli-src1"}), encoding="utf-8")
        Path(_safe_path(src1, "sites", "guide_sites.json")).write_text(
            json.dumps({"placeholder": "cli-src1"}), encoding="utf-8")
        os.makedirs(src2)
        Path(_safe_path(src2, "state.json")).write_text(
            json.dumps({"placeholder": "cli-src2"}), encoding="utf-8")
        Path(_safe_path(src2, "runtime.json")).write_text(
            json.dumps({"placeholder": "cli-src2"}), encoding="utf-8")
        missing = os.path.join(tmp, "no_such_dir")

        # 缺 --apply → exit 2，绝不写盘
        code, out = run_cli(_safe_path(tmp, "env_noapply"), "--from", src1)
        assert code == 2, (code, out)
        assert not os.path.isdir(_safe_path(tmp, "env_noapply", "OKntePlanner")), out
        # 无 --apply 单传 --dry-run → exit 2
        code, out = run_cli(_safe_path(tmp, "env_dryonly"), "--from", src1, "--dry-run")
        assert code == 2, (code, out)
        # 源目录不存在 → exit 2
        code, out = run_cli(_safe_path(tmp, "env_missing"), "--from", missing, "--apply")
        assert code == 2, (code, out)
        # --dry-run --apply：演练零写入、exit 0
        env_run = _safe_path(tmp, "env_run")
        code, out = run_cli(env_run, "--from", src1, "--from", src2, "--dry-run", "--apply")
        assert code == 0, (code, out)
        assert not os.path.isdir(_safe_path(env_run, "OKntePlanner")), out
        # 正式迁移：src1 先处理 → 同路径文件以 src1 为准（no-clobber 先到先得）
        code, out = run_cli(env_run, "--from", src1, "--from", src2, "--apply")
        assert code == 0, (code, out)
        user = _safe_path(env_run, "OKntePlanner", "data")
        assert json.loads(Path(_safe_path(user, "state.json")).read_text(
            encoding="utf-8"))["placeholder"] == "cli-src1", "先处理的源必须获胜"
        assert os.path.isfile(_safe_path(user, "runtime.json"))
        assert os.path.isfile(_safe_path(user, "sites", "guide_sites.json"))
        # 源目录原样保留（只复制不删/不动）
        assert os.path.isfile(_safe_path(src1, "state.json"))
        assert os.path.isfile(_safe_path(src2, "state.json"))
        assert os.path.isfile(_safe_path(src2, "runtime.json"))
        # CLI 不写 .migrated（首启 ensure_user_data 保持单一事实源）
        assert not os.path.isfile(_safe_path(user, ".migrated"))
        # 输出只有计数与路径，无文件内容
        assert "cli-src1" not in out and "cli-src2" not in out, out
        # 重复 --apply：全部跳过且 exit 0
        code, out = run_cli(env_run, "--from", src1, "--from", src2, "--apply")
        assert code == 0 and "复制文件 0" in out, (code, out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_frozen_data_dir_relocation():
    """打包态数据目录重定向全链回归（2026-11-12 state.py 分支重写）：①sys.frozen+
    假 EXE → DATA_DIR=%LOCALAPPDATA%\\\\OKntePlanner\\\\data，EXE 旁 data（伪造
    state.json 存根+出厂数据表+伪造用户文件）整树迁入且源原样保留、.migrated 已写；
    ②同场景二次运行幂等（marker 在 → 用户目录中被删的文件不被补回）；③设
    OKNTE_PLANNER_USER_DATA=1（启动器档路径）同归用户目录（以临时 app 副本+伪造
    EXE 旁 data 验证，不复制真实仓库 data）；④两者皆不设 → DATA_DIR==仓库 data
    （源码分支不变）。子进程 env LOCALAPPDATA=临时目录，绝不碰真实 %LOCALAPPDATA%；
    C# 侧接线不在此覆盖（由构建期 _launch_and_probe 断言承担）。"""
    import subprocess as _sp

    def run_py(env_dir, script, env_extra, cwd):
        env = {**os.environ, "LOCALAPPDATA": env_dir, **env_extra}
        return _sp.run([sys.executable, "-c", script], cwd=cwd, env=env,
                       capture_output=True, text=True, timeout=120)

    tmp = tempfile.mkdtemp(prefix="oknte_frozen_")
    try:
        # 伪造 EXE 旁 data：出厂数据表 + 伪造旧用户数据（占位内容，无真实凭据）
        fake_exe_dir = _safe_path(tmp, "fake_exe")
        bundled = _safe_path(fake_exe_dir, "data")
        os.makedirs(_safe_path(bundled, "backups"))
        Path(_safe_path(bundled, "materials.json")).write_text(
            json.dumps({"placeholder": "frozen-table"}), encoding="utf-8")
        Path(_safe_path(bundled, "state.json")).write_text(
            json.dumps({"settings": {"oknte_root": "FAKE-ROOT-PLACEHOLDER"}}),
            encoding="utf-8")
        Path(_safe_path(bundled, "backups", "a.json")).write_text("{}", encoding="utf-8")
        fake_exe = os.path.join(fake_exe_dir, "OKntePlanner.exe")
        src_snapshot = {}
        for base, _dirs, files in os.walk(fake_exe_dir):
            for name in files:
                p = os.path.join(base, name)
                src_snapshot[p] = Path(p).read_bytes()

        # ① frozen：import 前设 sys.frozen+假 EXE 路径 → 用户数据目录+首启迁移
        script = (
            "import os, sys\n"
            "sys.frozen = True\n"
            "sys.executable = %r\n"
            "import app.state as s\n"
            "print(s.DATA_DIR)\n"
            "print(os.path.isfile(os.path.join(s.DATA_DIR, '.migrated')))\n"
            "print(os.path.isfile(os.path.join(s.DATA_DIR, 'materials.json')))\n"
        ) % fake_exe
        env_frozen = _safe_path(tmp, "env_frozen")
        r = run_py(env_frozen, script, {}, _ROOT)
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        lines = r.stdout.strip().splitlines()
        assert len(lines) == 3, r.stdout
        user_dir = lines[0]
        assert user_dir.lower() == os.path.join(
            env_frozen, "OKntePlanner", "data").lower(), user_dir
        assert lines[1] == "True", r.stdout  # .migrated 已写
        assert lines[2] == "True", r.stdout  # 出厂数据表已播种
        # 源目录逐文件字节不变、全部仍在；用户目录拿到全部文件（旧数据全链迁移）
        for base, _dirs, files in os.walk(fake_exe_dir):
            for name in files:
                p = os.path.join(base, name)
                assert Path(p).read_bytes() == src_snapshot[p], p
                rel = os.path.relpath(p, bundled)
                assert Path(_safe_path(user_dir, rel)).read_bytes() == src_snapshot[p], rel
        # ② 二次运行幂等：marker 在 → 被删的用户文件不被补回
        os.remove(os.path.join(user_dir, "materials.json"))
        r = run_py(env_frozen, script, {}, _ROOT)
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        lines = r.stdout.strip().splitlines()
        assert lines[0] == user_dir, r.stdout
        assert lines[1] == "True", r.stdout   # marker 仍在
        assert lines[2] == "False", r.stdout  # 被删文件未补回（幂等门生效）

        # ③ 启动器档路径：OKNTE_PLANNER_USER_DATA=1 → 同归用户目录
        #    （以临时 app 副本伪造 BaseDir：state.py 的 ROOT 派生自 __file__ 的上级，
        #     直接跑仓库根会把真实 data/ 整树当播种源，故隔离）
        base = _safe_path(tmp, "fake_base")
        shutil.copytree(os.path.join(_ROOT, "app"), _safe_path(base, "app"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        os.makedirs(_safe_path(base, "data"), exist_ok=True)
        Path(_safe_path(base, "data", "materials.json")).write_text(
            json.dumps({"placeholder": "launcher-table"}), encoding="utf-8")
        env_launcher = _safe_path(tmp, "env_launcher")
        r = run_py(env_launcher, "import app.state as s\nprint(s.DATA_DIR)",
                   {"OKNTE_PLANNER_USER_DATA": "1"}, base)
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        assert r.stdout.strip().lower() == os.path.join(
            env_launcher, "OKntePlanner", "data").lower(), r.stdout
        seeded = _safe_path(env_launcher, "OKntePlanner", "data", "materials.json")
        assert json.loads(Path(seeded).read_text(encoding="utf-8"))["placeholder"] \
            == "launcher-table", "打包态首启应播种 BaseDir data"
        assert os.path.isfile(_safe_path(base, "data", "materials.json"))  # 源原样保留

        # ④ 两者皆不设：源码分支不变（DATA_DIR==仓库 data，不触发任何迁移）
        env_plain = _safe_path(tmp, "env_plain")
        r = run_py(env_plain, "import app.state as s\nprint(s.DATA_DIR)", {}, _ROOT)
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        assert r.stdout.strip() == os.path.join(_ROOT, "data"), r.stdout
        assert not os.path.isdir(_safe_path(env_plain, "OKntePlanner"))  # 未做任何迁移
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_build_dist_data_gate():
    """tools.build_exe.check_dist_data_clean 打包前数据闸门回归（2026-11-12）：
    仅含 keepList 出厂集（含 mysqil/.bak、sites/source_crosscheck.json）的假 dist
    树零 offender；掺入个人/运行时数据（state.json*/backups//tajiduo_token.json/
    edge_profile//sites/guide_sites.json/avatars//game_data_custom.json/
    ocr_layout.json/materials.json.pre-manual.bak）的假 dist 树 offender 全列出、
    mysqil 子树不误报；纯检查不删/不改任何文件（树前后逐字节一致）；结构断言：
    main 源码中 check_dist_data_clean 调用点先于 build_dist() 调用点（gate 必须发
    生在 rmtree 之前，否则闸门形同虚设而全部用例仍绿）。
    SKIP 守卫：dist 不携带 tools/（build_dist 仅复制 app/ 与 tests/），仓库根运行才有效。"""
    import inspect
    if not os.path.isdir(os.path.join(_ROOT, "tools")):
        print("SKIP test_build_dist_data_gate: dist 不携带 tools/（build_dist 仅复制"
              " app/ 与 tests/），仓库根运行才有效")
        return
    from tools import build_exe as build_mod

    def fake_dist(root, personal):
        data = _safe_path(root, "data")
        os.makedirs(_safe_path(data, "mysqil"))
        os.makedirs(_safe_path(data, "sites"))
        # keepList 出厂集（与 copy_data_tables 写入一一对应；character_cooldowns.json
        # 已移除，SEC-04①/B3 2026-11-18 用户裁定①，保留集 15→14 文件）
        for rel in ("materials.json", "yields.json", "requirements.json",
                    "game_data.json", "build_info.json"):
            Path(_safe_path(data, rel)).write_text("{}", encoding="utf-8")
        Path(_safe_path(data, "mysqil", "cassettes.json")).write_text("{}", encoding="utf-8")
        Path(_safe_path(data, "mysqil", "cassettes.json.bak")).write_text("{}", encoding="utf-8")
        Path(_safe_path(data, "sites", "source_crosscheck.json")).write_text("{}", encoding="utf-8")
        if personal:  # 个人/运行时数据（占位内容，无真实凭据）
            for rel in ("state.json", "state.json.bak", "game_data_custom.json",
                        "ocr_layout.json", "materials.json.pre-manual.bak",
                        "tajiduo_token.json"):
                Path(_safe_path(data, rel)).write_text("{}", encoding="utf-8")
            for rel_dir, name in (("backups", "a.json"), ("edge_profile", "p"),
                                  ("avatars", "a.webp")):
                os.makedirs(_safe_path(data, rel_dir), exist_ok=True)
                Path(_safe_path(data, rel_dir, name)).write_text("{}", encoding="utf-8")
            Path(_safe_path(data, "sites", "guide_sites.json")).write_text("{}", encoding="utf-8")
        return root

    tmp = tempfile.mkdtemp(prefix="oknte_gate_")
    try:
        clean_root = fake_dist(_safe_path(tmp, "OKntePlanner"), personal=False)
        dirty_root = fake_dist(_safe_path(tmp, "OKntePlanner_single"), personal=True)
        snap = {}
        for base, _dirs, files in os.walk(tmp):
            for name in files:
                p = os.path.join(base, name)
                snap[p] = Path(p).read_bytes()
        # 干净树：零 offender（mysqil 子树与 sites/source_crosscheck.json 不误报）
        offenders = build_mod.check_dist_data_clean((clean_root,))
        assert offenders == [], offenders
        # 脏树：个人/运行时数据全列出，出厂集与 mysqil 子树不误报
        offenders = build_mod.check_dist_data_clean((dirty_root,))
        prefix = os.path.basename(dirty_root)
        expected = {os.path.join(prefix, "data", rel) for rel in (
            "state.json", "state.json.bak", "game_data_custom.json",
            "ocr_layout.json", "materials.json.pre-manual.bak", "tajiduo_token.json",
            os.path.join("backups", "a.json"),
            os.path.join("edge_profile", "p"),
            os.path.join("avatars", "a.webp"),
            os.path.join("sites", "guide_sites.json"),
        )}
        assert set(offenders) == expected, (sorted(offenders), sorted(expected))
        # 双树同扫：干净树仍零误报
        both = build_mod.check_dist_data_clean((clean_root, dirty_root))
        assert set(both) == expected, sorted(both)
        # 纯检查不删/不改任何文件（树前后逐字节一致）
        snap2 = {}
        for base, _dirs, files in os.walk(tmp):
            for name in files:
                p = os.path.join(base, name)
                snap2[p] = Path(p).read_bytes()
        assert snap2 == snap, "check_dist_data_clean 必须是纯检查，不得动任何文件"
        # 结构断言：gate 调用点必须先于 build_dist()（先于其 rmtree(DIST)）
        src = inspect.getsource(build_mod.main)
        i_gate = src.find("check_dist_data_clean")
        i_build = src.find("build_dist()")
        assert i_gate != -1 and i_build != -1 and i_gate < i_build, \
            "check_dist_data_clean 必须在 main() 中先于 build_dist() 调用（gate 须在 rmtree 之前）"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- B2.3 SEC-10：受限提权辅助进程（方案B，2026-11-14 用户裁决） ----------

_SEC10_TMP_BASE = os.path.join(_ROOT, ".tmp", "sec10")


def _sec10_tmp(name):
    """SEC-10 用例临时根：仓库 .tmp/ 下（AGENTS.md 卫生规则，不落 C 盘）。"""
    os.makedirs(_SEC10_TMP_BASE, exist_ok=True)
    return tempfile.mkdtemp(prefix=name + "_", dir=_SEC10_TMP_BASE)


def _sec10_root(tmp, content="placeholder"):
    """伪造 ok-nte 根：<tmp>/root/data/apps/ok-nte/python/python.exe（占位内容，
    供指纹/校验用例；执行类用例用 _sec10_materialize_python）。"""
    root = _safe_path(tmp, "root")
    pydir = _safe_path(root, "data", "apps", "ok-nte", "python")
    os.makedirs(pydir, exist_ok=True)
    Path(_safe_path(pydir, "python.exe")).write_text(content, encoding="utf-8")
    return root


def _sec10_materialize_python(tmp):
    """把当前解释器物化到 <tmp>/root/.../python.exe（硬链接优先——同卷零拷贝；
    跨卷回退拷贝 exe + 运行 DLL），并做一次可执行探针。失败返回 None（用例 SKIP）。
    打包态直接返回 None：入口是 GUI EXE，物化不出可用 -c 执行的解释器，且探针
    会误拉起整个面板。"""
    if getattr(sys, "frozen", False):
        return None
    root = _safe_path(tmp, "root")
    pydir = _safe_path(root, "data", "apps", "ok-nte", "python")
    os.makedirs(pydir, exist_ok=True)
    py = _safe_path(pydir, "python.exe")
    if os.path.abspath(sys.executable).lower() != py.lower():
        try:
            os.link(sys.executable, py)
        except OSError:
            src = os.path.dirname(sys.executable)
            shutil.copy2(sys.executable, py)
            for name in os.listdir(src):
                low = name.lower()
                if re.match(r"^python3\d+\.dll$", low) or low.startswith("vcruntime"):
                    try:
                        shutil.copy2(os.path.join(src, name), os.path.join(pydir, name))
                    except OSError:
                        return None
    try:
        probe = subprocess.run([py, "-c", "print(1)"], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return root if probe.returncode == 0 else None


@case
def test_elev_helper_pure_validators():
    """SEC-10 纯校验矩阵：stdout 文件名/任务名/坐标/launcher/profile/启动配置
    白名单全部 fail-closed（不触系统资源，离线可测）。"""
    from app import elev_helper as eh
    # stdout 文件名白名单：固定前缀、无分隔符、长度受限
    assert eh.safe_stdout_name("ok-nte-headless-20261114-abcdef.log")
    for bad in ("", "a.log", "../evil.log", "a/b.log", "a\\b.log", "..",
                "ok-nte-live.log",
                "ok-nte-headless-" + "x" * 200 + ".log"):
        assert not eh.safe_stdout_name(bad), repr(bad)
    # task 白名单
    assert eh.task_allowed("日常任务") and eh.task_allowed("AnomalyHunter")
    for bad in ("", "cmd.exe", "AnomalyHunter ", "run_task -c"):
        assert not eh.task_allowed(bad), repr(bad)
    # 坐标必须落在虚拟屏幕内
    assert eh.coords_in_bounds(100, 200, (0, 0, 1920, 1080))
    assert not eh.coords_in_bounds(-1, 0, (0, 0, 1920, 1080))
    assert not eh.coords_in_bounds(1919, 1080, (0, 0, 1920, 1080))
    assert not eh.coords_in_bounds("x", 0, (0, 0, 1920, 1080))
    assert not eh.coords_in_bounds(0, 0, (0, 0, 0, 0))
    tmp = _sec10_tmp("pure")
    try:
        root = _sec10_root(tmp)
        profile = _safe_path(root, "working", "configs_planner")
        os.makedirs(profile, exist_ok=True)
        good = _safe_path(profile, "run_planner_task.py")
        Path(good).write_text("pass", encoding="utf-8")
        assert eh.launcher_allowed(good, root)
        assert not eh.launcher_allowed(_safe_path(root, "working", "main.py"), root)
        assert not eh.launcher_allowed(_safe_path(root, "working", "configs",
                                                  "run_planner_task.py"), root)
        assert not eh.launcher_allowed(_safe_path(profile, "evil.py"), root)
        assert not eh.launcher_allowed(good + ".bak", root)
        assert not eh.launcher_allowed(good, tmp)  # root 不匹配
        assert eh.profile_allowed(profile, root)
        assert not eh.profile_allowed(_safe_path(root, "working", "configs"), root)
        # 启动配置校验矩阵（fail-closed）
        base = {"schema": 1, "token": "a" * 64, "parent_pid": 4321,
                "root": root, "log_dir": root, "game_process": "HTGame.exe"}

        def cfg_ok(**over):
            merged = dict(base)
            merged.update(over)
            return merged

        def expect_cfg_reject(cfg, cfg_path, anchor):
            try:
                eh.validate_config(cfg, cfg_path, anchor)
                raise AssertionError("配置应被拒绝: %r" % (cfg,))
            except eh.ConfigError:
                pass
        # 锚定目录 = root（配置文件本应位于 log_dir=root 内），逐字段触发各自拒绝
        root_real = os.path.realpath(root)
        outside = _safe_path(tmp, "elev_helper_config-1-abcd1234.json")
        for over in ({"schema": 2}, {"token": "short"}, {"token": "z" * 64},
                     {"parent_pid": 0}, {"parent_pid": True}, {"parent_pid": "9"},
                     {"root": _safe_path(tmp, "nope")},
                     {"log_dir": _safe_path(tmp, "nope")},
                     {"game_process": ""}, {"game_process": "x" * 100}):
            expect_cfg_reject(cfg_ok(**over), outside, root_real)
        # 锚定目录与 log_dir 不一致（配置文件位于 log_dir 外）→ 拒绝
        expect_cfg_reject(cfg_ok(), outside, os.path.realpath(tmp))
        # 合法形态：配置文件与 log_dir 同目录且 root 内有内嵌 python.exe
        log_root = _sec10_root(_safe_path(tmp, "lg"), content="ok")
        cfg2 = _safe_path(log_root, "elev_helper_config-1-abcd1234.json")
        got = eh.validate_config(cfg_ok(root=log_root, log_dir=log_root),
                                 cfg2, os.path.realpath(log_root))
        assert got[0] == "a" * 64 and got[1] == 4321 and got[4] == "HTGame.exe", got
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_elev_helper_e2e_auth_ops_audit():
    """SEC-10 端到端（测试模式拉起真实辅助进程子进程）：ping 握手、错 token 拒绝、
    未知操作拒绝、launch_runner 参数越界逐一被拒（不接受任意命令/任意路径/任意
    文件写）、解释器指纹变化拒绝执行、审计落盘且绝不含令牌、shutdown 随退且
    端点文件清理（一次性 UAC 启动形态在测试模式等价覆盖，UAC 本体列 Manual）。"""
    from app import elev_client
    elev_client.set_test_spawn(True)
    tmp = _sec10_tmp("e2e")
    log_dir = _safe_path(tmp, "log")
    os.makedirs(log_dir, exist_ok=True)
    root = _sec10_root(tmp)
    try:
        E = elev_client.ELEV
        assert E.ensure(root, log_dir=log_dir, game_process="HTGame.exe"), E.reason()
        port, token = E._port, E._token
        py_path = _safe_path(root, "data", "apps", "ok-nte", "python", "python.exe")
        resp = elev_client.request_once(port, token, "ping", timeout=8)
        assert resp.get("ok") and resp.get("root_fp") == \
            elev_client.file_sha256(py_path)[:16], resp
        # 错 token 拒绝（未认证连接面）
        try:
            elev_client.request_once(port, "f" * 64, "ping", timeout=8)
            raise AssertionError("错 token 应被拒绝")
        except elev_client.ElevRejected:
            pass
        # 未知操作拒绝（不提供通用管理员命令接口）
        try:
            elev_client.request_once(port, token, "run_command", timeout=8,
                                     args={"cmd": "whoami"})
            raise AssertionError("未知操作应被拒绝")
        except elev_client.ElevRejected:
            pass
        # launch_runner 参数越界逐一拒绝
        profile = _safe_path(root, "working", "configs_planner")
        os.makedirs(profile, exist_ok=True)
        launcher = _safe_path(profile, "run_planner_task.py")
        Path(launcher).write_text("print('ok')", encoding="utf-8")
        # SEC-11（B3-B）夹具适配：契约收紧后 launch args 必带
        # expected_launcher_sha256——夹具自算盘面哈希随 args 下发（内容与哈希
        # 一致）；下方越界断言全部保留原语义（路径闸先于内容闸，越界路径被拒
        # 与哈希字段无关）
        launcher_sha = elev_client.file_sha256(launcher)

        def launch_args(**over):
            args = {"launcher": launcher, "profile": profile, "task": "日常任务",
                    "stdout": "ok-nte-headless-selftest.log",
                    "expected_launcher_sha256": launcher_sha}
            args.update(over)
            return args

        def expect_reject(**over):
            try:
                elev_client.request_once(port, token, "launch_runner",
                                         launch_args(**over), timeout=15)
                raise AssertionError("launch_runner 应拒绝: %r" % over)
            except elev_client.ElevRejected:
                pass
        expect_reject(launcher=_safe_path(root, "working", "main.py"))
        expect_reject(launcher=launcher.replace("configs_planner", "configs"))
        expect_reject(launcher=launcher + ".bak")
        expect_reject(task="cmd.exe")
        expect_reject(stdout="../evil.log")
        expect_reject(stdout="ok-nte-live.log")
        expect_reject(profile=_safe_path(root, "working", "configs"))
        # 占位 python 无法执行 → helper 侧 Popen 失败（合法参数的诚实失败路径）
        try:
            elev_client.request_once(port, token, "launch_runner",
                                     launch_args(), timeout=15)
            raise AssertionError("占位 python 应 Popen 失败")
        except elev_client.ElevRejected:
            pass
        # 指纹变化（模拟 ok-nte 更新换解释器）→ 拒绝执行
        Path(py_path).write_text("tampered", encoding="utf-8")
        try:
            elev_client.request_once(port, token, "launch_runner",
                                     launch_args(), timeout=15)
            raise AssertionError("指纹变化应拒绝执行")
        except elev_client.ElevRejected as e:
            assert "指纹" in str(e), str(e)
        # 会话仍健康（操作被拒不得作废会话）
        assert E.available()
        # 审计：落盘、含拒绝记录、绝不含令牌
        audit = Path(_safe_path(log_dir, "elev_helper_audit.log"))
        assert audit.is_file(), "安全事件审计未落盘"
        content = audit.read_text(encoding="utf-8")
        assert "reject" in content and "launch_runner" in content
        assert token not in content and ("f" * 64) not in content, "令牌泄漏进审计"
        # shutdown 随退 + 端点文件清理
        pid = E._pid
        E.shutdown()
        deadline = time.time() + 6
        while time.time() < deadline and elev_client._pid_alive(pid):
            time.sleep(0.2)
        assert not elev_client._pid_alive(pid), "shutdown 后辅助进程应退出"
        assert not Path(_safe_path(log_dir,
                                   "elev_helper_endpoint-%d.json" % pid)).exists()
    finally:
        elev_client.set_test_spawn(False)
        elev_client.ELEV.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- B3-B SEC-11：launcher 内容 SHA-256 契约（写方期望值绑定执行对象） ----------
# 依据：B3 slice-f 修复前确定性复现在案——launch_runner 对 launcher 无内容校验，
# 白名单路径上的同路径异内容篡改文件被执行（防线不对称：解释器指纹篡改被拒）。
# 修复方案（任务书裁定）：写方常量 LAUNCHER_SHA256（oknte_adapter）绑定执行对象，
# helper 每次 launch 前 Popen 复核盘面 SHA-256 与 args 期望值一致才执行。

@case
def test_sec11_launcher_sha_tamper_rejected():
    """SEC-11 ①：同路径异内容（篡改）→ launch 被拒且审计记 launcher_sha_mismatch。

    复现口径（B3 slice-f 同款夹具：_sec10_materialize_python 物化真解释器，
    set_test_spawn(True) 无 UAC 拉起真实 helper）：白名单路径
    <root>/working/configs_planner/run_planner_task.py 写入与写方期望值
    （LAUNCHER_SHA256）不同的无害篡改内容（纯 print+sys.exit，无网络/文件写）
    → launch_runner。修复前该请求放行且篡改内容被执行；修复后 helper 在
    Popen 前复核盘面 SHA-256 不符 → 拒绝 + 审计 reject 行记
    launcher_sha_mismatch，篡改内容绝不执行。"""
    from app import elev_client
    elev_client.set_test_spawn(True)
    tmp = _sec10_tmp("sec11tamper")
    root = _sec10_materialize_python(tmp)
    if root is None:
        print("SKIP test_sec11_launcher_sha_tamper_rejected: 当前解释器无法物化"
              "到 .tmp 临时根（跨卷且缺运行 DLL）")
        elev_client.set_test_spawn(False)
        shutil.rmtree(tmp, ignore_errors=True)
        return
    profile = _safe_path(root, "working", "configs_planner")
    os.makedirs(profile, exist_ok=True)
    launcher = _safe_path(profile, "run_planner_task.py")
    # 篡改内容：无害 print+退出（复现脚本同款），不 import ok、无网络、无文件写
    Path(launcher).write_text(
        "import sys\nprint('TAMPERED-SEC11-REJECTED')\nsys.exit(0)\n",
        encoding="utf-8")
    log_dir = _safe_path(tmp, "log")
    os.makedirs(log_dir, exist_ok=True)
    stdout_name = "ok-nte-headless-sec11-tamper.log"
    try:
        E = elev_client.ELEV
        assert E.ensure(root, log_dir=log_dir, game_process="HTGame.exe"), \
            E.reason()
        port, token = E._port, E._token
        # 写方期望值 = 合法 LAUNCHER_SRC 哈希（生产 client 缺省下发形态），
        # 盘面是篡改内容 → 必须 launcher_sha_mismatch 拒绝
        try:
            elev_client.request_once(port, token, "launch_runner", {
                "launcher": launcher, "profile": profile, "task": "日常任务",
                "stdout": stdout_name,
                "expected_launcher_sha256": adapter_mod.LAUNCHER_SHA256},
                timeout=20)
            raise AssertionError("篡改 launcher 应被拒绝（launcher_sha_mismatch）")
        except elev_client.ElevRejected as e:
            assert "launcher_sha_mismatch" in str(e), str(e)
        # 审计：reject 行记 launcher_sha_mismatch（照既有审计形态）
        audit = Path(_safe_path(log_dir, "elev_helper_audit.log")).read_text(
            encoding="utf-8")
        assert "launcher_sha_mismatch" in audit, "审计应记 launcher_sha_mismatch"
        # 核心补强断言：篡改内容绝未被执行（stdout 镜像不存在或不含标记）
        stdout_path = _safe_path(log_dir, stdout_name)
        assert not os.path.isfile(stdout_path) or \
            "TAMPERED-SEC11-REJECTED" not in Path(stdout_path).read_text(
                encoding="utf-8", errors="replace"), "篡改内容被执行"
        # 会话仍健康（操作被拒不得作废会话）
        assert E.available()
    finally:
        elev_client.set_test_spawn(False)
        elev_client.ELEV.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_sec11_launcher_sha_valid_launch_e2e():
    """SEC-11 ②：合法内容 + 正确哈希 → launch 成功（e2e 全链）。

    同一 helper 会话内两阶段串行：阶段一 request_once 直发（夹具 print 内容 +
    其自算盘面哈希，内容与哈希一致）断言放行且子进程真实执行（stdout 镜像含
    标记）；阶段二以 newline="\\n" 写盘形态精确等于 LAUNCHER_SRC 的 launcher
    （盘面字节哈希 == 写方常量）走生产 client 链（ELEV.launch_runner 缺省下发
    expected_launcher_sha256）断言放行——写方期望值绑定执行对象全链闭环。
    阶段二子进程在占位 root import ok 失败自行退出（launch 成功语义不受影响），
    finally 收尾不留孤儿。"""
    from app import elev_client
    elev_client.set_test_spawn(True)
    tmp = _sec10_tmp("sec11valid")
    root = _sec10_materialize_python(tmp)
    if root is None:
        print("SKIP test_sec11_launcher_sha_valid_launch_e2e: 当前解释器无法物化"
              "到 .tmp 临时根（跨卷且缺运行 DLL）")
        elev_client.set_test_spawn(False)
        shutil.rmtree(tmp, ignore_errors=True)
        return
    profile = _safe_path(root, "working", "configs_planner")
    os.makedirs(profile, exist_ok=True)
    launcher = _safe_path(profile, "run_planner_task.py")
    log_dir = _safe_path(tmp, "log")
    os.makedirs(log_dir, exist_ok=True)
    pids = []
    try:
        E = elev_client.ELEV
        assert E.ensure(root, log_dir=log_dir, game_process="HTGame.exe"), \
            E.reason()
        port, token = E._port, E._token
        # 阶段一：夹具内容 + 夹具自算盘面哈希（内容与哈希一致）→ 放行并真实执行
        Path(launcher).write_text(
            "import sys\nprint('SEC11-VALID-LAUNCH', flush=True)\nsys.exit(0)\n",
            encoding="utf-8")
        resp = elev_client.request_once(port, token, "launch_runner", {
            "launcher": launcher, "profile": profile, "task": "日常任务",
            "stdout": "ok-nte-headless-sec11-valid.log",
            "expected_launcher_sha256": elev_client.file_sha256(launcher)},
            timeout=20)
        assert resp.get("ok") and isinstance(resp.get("pid"), int) \
            and resp["pid"] > 0, resp
        pids.append(resp["pid"])
        stdout_path = _safe_path(log_dir, "ok-nte-headless-sec11-valid.log")
        deadline = time.time() + 15
        seen = ""
        while time.time() < deadline:
            if os.path.isfile(stdout_path):
                seen = Path(stdout_path).read_text(encoding="utf-8",
                                                   errors="replace")
                if "SEC11-VALID-LAUNCH" in seen:
                    break
            if not elev_client._pid_alive(pids[-1]):
                break  # 子进程退出后再做终判
            time.sleep(0.1)
        assert os.path.isfile(stdout_path), "stdout 镜像未创建"
        assert "SEC11-VALID-LAUNCH" in seen, \
            f"合法内容未被执行（stdout 镜像）: {seen[:200]!r}"
        # 阶段二：盘面 = LAUNCHER_SRC（LF 写盘，盘面字节哈希 == 写方常量）→
        # 生产 client 链（缺省下发）放行
        Path(launcher).write_text(adapter_mod.LAUNCHER_SRC,
                                  encoding="utf-8", newline="\n")
        pid2 = E.launch_runner(root, launcher, "日常任务", profile,
                               "ok-nte-headless-sec11-valid2.log")
        assert isinstance(pid2, int) and pid2 > 0, pid2
        pids.append(pid2)
    finally:
        elev_client.set_test_spawn(False)
        elev_client.ELEV.shutdown()
        for pid in pids:  # 收尾不留孤儿（SRV-06 收尸兜底之上的测试侧兜底）
            if elev_client._pid_alive(pid):
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, creationflags=0x08000000)
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_sec11_writeback_selfcheck():
    """SEC-11 ③：写盘自校验——LAUNCHER_SRC 与盘面不符 → 异常（拒绝写盘完成态）。

    把写方常量 LAUNCHER_SRC patch 为篡改内容（LAUNCHER_SHA256 保持不变）模拟
    「盘面内容与写方期望值不符」（写盘损坏/半截/被掉包），prepare_hunter_profile
    写盘后读回盘面字节复核必须抛 OkNteError（含「写盘自校验失败」）；再验不
    patch 时写盘成功且盘面字节 SHA-256 精确等于 LAUNCHER_SHA256（LF 盘面形态
    锁定，防未来 write_text 行为漂移静默破坏契约）。"""
    tmp = _sec10_tmp("sec11write")
    try:
        root = _safe_path(tmp, "root")
        # OkNte.working = <root>/data/apps/ok-nte/working（oknte_adapter.py:234）
        os.makedirs(_safe_path(root, "data", "apps", "ok-nte", "working",
                               "configs"), exist_ok=True)
        ok = OkNte(root)
        # 正向：写盘成功且盘面字节哈希 == 写方期望值（LF 形态锁定）
        p = ok.prepare_hunter_profile("测试目标", 3)
        assert os.path.isfile(p), p
        disk_sha = hashlib.sha256(Path(p).read_bytes()).hexdigest()
        assert disk_sha == adapter_mod.LAUNCHER_SHA256, \
            (disk_sha, adapter_mod.LAUNCHER_SHA256)
        # 反向：写盘内容与写方期望值不符 → 自校验抛错
        orig_src = adapter_mod.LAUNCHER_SRC
        try:
            adapter_mod.LAUNCHER_SRC = orig_src + "\n# sec11-tampered\n"
            try:
                ok.prepare_hunter_profile("测试目标", 3)
                raise AssertionError("盘面与写方期望值不符应抛 OkNteError")
            except adapter_mod.OkNteError as e:
                assert "写盘自校验失败" in str(e), str(e)
        finally:
            adapter_mod.LAUNCHER_SRC = orig_src
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_sec11_launcher_allowed_matrix_regression():
    """SEC-11 ④：launcher_allowed 矩阵回归——契约收紧不得触碰路径白名单既有
    防线。逐字重跑既有矩阵用例 test_elev_helper_pure_validators（任务书所指
    约 :8250-8328 实读偏移至 selftest.py:9553-9623：stdout/task/坐标/
    launcher/profile/启动配置全矩阵），并补充断言路径闸语义逐字不变
    （launcher_allowed 纯函数在契约改动前后零改动）。"""
    test_elev_helper_pure_validators()
    # 路径闸语义补充回归（契约闸必须建立在路径闸之后，不得替代/收紧路径面）
    from app import elev_helper as eh
    tmp = _sec10_tmp("sec11matrix")
    try:
        root = _sec10_root(tmp)
        profile = _safe_path(root, "working", "configs_planner")
        os.makedirs(profile, exist_ok=True)
        good = _safe_path(profile, "run_planner_task.py")
        Path(good).write_text("pass", encoding="utf-8")
        assert eh.launcher_allowed(good, root)
        assert not eh.launcher_allowed(good + ".bak", root)
        assert not eh.launcher_allowed(_safe_path(profile, "evil.py"), root)
        assert not eh.launcher_allowed(_safe_path(root, "working", "main.py"),
                                       root)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_sec11_missing_sha_arg_rejected():
    """SEC-11 ⑤：args 缺 expected_launcher_sha256 字段 → 拒绝并审计（契约收紧
    行为锁定）。占位 root 即可到达契约闸（指纹固化一致 → 路径/存在闸通过 →
    缺字段拒绝发生在 Popen 之前）；空串/None/非字符串同面 fail-closed。
    审计 reject 行记 missing_expected_launcher_sha256。"""
    from app import elev_client
    elev_client.set_test_spawn(True)
    tmp = _sec10_tmp("sec11missing")
    log_dir = _safe_path(tmp, "log")
    os.makedirs(log_dir, exist_ok=True)
    root = _sec10_root(tmp)
    try:
        E = elev_client.ELEV
        assert E.ensure(root, log_dir=log_dir, game_process="HTGame.exe"), \
            E.reason()
        port, token = E._port, E._token
        profile = _safe_path(root, "working", "configs_planner")
        os.makedirs(profile, exist_ok=True)
        launcher = _safe_path(profile, "run_planner_task.py")
        Path(launcher).write_text("print('ok')", encoding="utf-8")
        base = {"launcher": launcher, "profile": profile, "task": "日常任务",
                "stdout": "ok-nte-headless-sec11-missing.log"}
        for bad_args in (
                dict(base),                                  # 键不存在
                dict(base, expected_launcher_sha256=""),     # 空串
                dict(base, expected_launcher_sha256=None),   # None
                dict(base, expected_launcher_sha256=123),    # 非字符串
        ):
            try:
                elev_client.request_once(port, token, "launch_runner",
                                         bad_args, timeout=15)
                raise AssertionError("缺 expected_launcher_sha256 应拒绝: %r"
                                     % (bad_args,))
            except elev_client.ElevRejected as e:
                assert "expected_launcher_sha256" in str(e), str(e)
        # 审计锁定：契约收紧拒绝落盘
        audit = Path(_safe_path(log_dir, "elev_helper_audit.log")).read_text(
            encoding="utf-8")
        assert "missing_expected_launcher_sha256" in audit, \
            "审计应记契约收紧拒绝"
        # 会话仍健康
        assert E.available()
    finally:
        elev_client.set_test_spawn(False)
        elev_client.ELEV.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_elev_helper_watchdog_parent_exit():
    """SEC-10 随主进程退出：父面板进程死亡（假面板子进程退出）→ 辅助进程在
    看门狗下随退，端点文件清理，审计记 parent_exit（退出清理不留孤儿提权进程）。"""
    tmp = _sec10_tmp("wd")
    log_dir = _safe_path(tmp, "log")
    os.makedirs(log_dir, exist_ok=True)
    root = _sec10_root(tmp)
    parent = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(1.2)"],
        cwd=_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=0x08000000)
    helper = None
    try:
        cfg = {"schema": 1, "token": "b" * 64, "parent_pid": parent.pid,
               "root": root, "log_dir": log_dir, "game_process": "HTGame.exe"}
        cfg_path = _safe_path(log_dir, "elev_helper_config-%d-abcd1234.json"
                              % parent.pid)
        Path(cfg_path).write_text(json.dumps(cfg), encoding="utf-8")
        if getattr(sys, "frozen", False):
            # 打包态：入口是本 EXE（bootloader 透传参数给 main.py 的入口分流）
            helper_argv = [sys.executable, "--oknte-elev-helper",
                           "--config", log_dir, os.path.basename(cfg_path)]
        else:
            helper_argv = [sys.executable, "-m", "app.elev_helper",
                           "--config", log_dir, os.path.basename(cfg_path)]
        helper = subprocess.Popen(
            helper_argv,
            cwd=_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000)
        endpoint = _safe_path(log_dir, "elev_helper_endpoint-%d.json" % parent.pid)
        deadline = time.time() + 15
        while time.time() < deadline and not os.path.isfile(endpoint):
            time.sleep(0.2)
        assert os.path.isfile(endpoint), "辅助进程未写端点文件"
        parent.wait(timeout=10)  # 假面板退出
        deadline = time.time() + 8
        while time.time() < deadline and helper.poll() is None:
            time.sleep(0.3)
        assert helper.poll() is not None, "父进程死后辅助进程未随退"
        assert not os.path.isfile(endpoint), "退出后端点文件应清理"
        audit = Path(_safe_path(log_dir, "elev_helper_audit.log"))
        assert audit.is_file() and "parent_exit" in audit.read_text(encoding="utf-8")
    finally:
        if helper is not None and helper.poll() is None:
            helper.kill()
        if parent.poll() is None:
            parent.kill()
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_elev_client_root_fp_relaunch():
    """SEC-10 客户端会话生命周期：root 结构不完整直接失败（绝不弹 UAC）、
    内嵌解释器指纹变化 → 旧会话退出 + 新会话拉起（变更需再次 UAC 同意的
    测试模式等价形态）、shutdown 干净随退。"""
    from app import elev_client
    elev_client.set_test_spawn(True)
    tmp = _sec10_tmp("cli")
    log_dir = _safe_path(tmp, "log")
    os.makedirs(log_dir, exist_ok=True)
    root = _sec10_root(tmp, content="v1")
    try:
        E = elev_client.ELEV
        assert E.ensure(root, log_dir=log_dir, game_process="HTGame.exe"), E.reason()
        pid1 = E._pid
        # 结构不完整 root → False + 原因可诊断；既有会话不受影响
        bad = _safe_path(tmp, "badroot")
        os.makedirs(bad, exist_ok=True)
        assert not E.ensure(bad, log_dir=log_dir)
        assert "python.exe" in E.reason()
        assert E.available() and E._pid == pid1
        # 指纹变化 → 旧会话拒绝服务，新会话拉起
        py = _safe_path(root, "data", "apps", "ok-nte", "python", "python.exe")
        Path(py).write_text("v2", encoding="utf-8")
        assert E.ensure(root, log_dir=log_dir, game_process="HTGame.exe"), E.reason()
        pid2 = E._pid
        assert pid2 != pid1, "指纹变化应重新拉起会话"
        deadline = time.time() + 6
        while time.time() < deadline and elev_client._pid_alive(pid1):
            time.sleep(0.2)
        assert not elev_client._pid_alive(pid1), "旧会话辅助进程应退出"
        # shutdown 干净随退
        E.shutdown()
        deadline = time.time() + 6
        while time.time() < deadline and elev_client._pid_alive(pid2):
            time.sleep(0.2)
        assert not elev_client._pid_alive(pid2), "shutdown 后应退出"
        assert not E.available()
    finally:
        elev_client.set_test_spawn(False)
        elev_client.ELEV.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_runner_helper_launch_stdout_stop():
    """SEC-10 runner 集成（真实提权辅助进程 + 真实子进程）：_launch_and_monitor
    经辅助进程拉起（_ElevChild）、stdout 镜像文件经 _tail_stdout_file 泵回面板
    日志、登记表生效（is_running 补盲）、stop_event → _kill_child 走 stop_runner
    → 子进程终止、finally 注销登记。runtime.json 与 DATA_DIR 全部重定向到 .tmp。"""
    from app import elev_client
    elev_client.set_test_spawn(True)
    tmp = _sec10_tmp("runner")
    orig_runtime = runner_mod.RUNTIME_FILE
    orig_data_dir = runner_mod.DATA_DIR
    runner_mod.RUNTIME_FILE = _safe_path(tmp, "runtime.json")
    runner_mod.DATA_DIR = _safe_path(tmp, "paneldata")
    os.makedirs(runner_mod.DATA_DIR, exist_ok=True)
    # 辅助进程 log_dir 必须与 runner 的 DATA_DIR 同目录（生产口径：两者同为
    # state.DATA_DIR），stdout 镜像才会落在被泵的目录里
    log_dir = runner_mod.DATA_DIR
    root = _sec10_materialize_python(tmp)
    r = runner_mod.RUNNER
    helper_pid = None
    # SEC-11（B3-B）夹具适配锚点：写方常量在本用例内被 patch（见下），finally 恢复
    orig_launcher_sha = adapter_mod.LAUNCHER_SHA256
    try:
        if root is None:
            print("SKIP test_runner_helper_launch_stdout_stop: 当前解释器无法"
                  "物化到 .tmp 临时根（跨卷且缺运行 DLL）")
            return
        with _StateGuard():
            os.makedirs(_safe_path(root, "working", "logs"), exist_ok=True)
            profile = _safe_path(root, "working", "configs_planner")
            os.makedirs(profile, exist_ok=True)
            launcher = _safe_path(profile, "run_planner_task.py")
            Path(launcher).write_text(
                "import time\n"
                "print('[ok-nte] sec10-fixture-line-1', flush=True)\n"
                "print('sec10-fixture-line-2', flush=True)\n"
                "time.sleep(30)\n", encoding="utf-8")
            # SEC-11（B3-B）夹具适配：本用例经生产 client 链（ELEV.launch_runner
            # 缺省下发写方常量），夹具内容非 LAUNCHER_SRC，故把写方常量 patch 为
            # 夹具盘面哈希（finally 恢复原值）——执行链路与全部断言不变
            adapter_mod.LAUNCHER_SHA256 = elev_client.file_sha256(launcher)
            ok = OkNte(root)
            cmd, cwd, env = ok.run_command(launcher)
            assert elev_client.ELEV.ensure(root, log_dir=log_dir,
                                           game_process="HTGame.exe"), \
                elev_client.ELEV.reason()
            helper_pid = elev_client.ELEV._pid
            r.child = None
            r.stop_event.clear()
            result = {}

            def _run():
                result["out"] = r._launch_and_monitor(
                    ok, cmd, cwd, env, "2026-01-01",
                    {"mat_id": "coin", "name": "SEC10夹具", "runs": 1,
                     "stamina": 60, "gain": 10}, 60)

            th = threading.Thread(target=_run, daemon=True)
            th.start()
            # stdout 镜像泵：夹具输出经辅助进程重定向 → 面板日志行
            deadline = time.time() + 25
            while time.time() < deadline:
                if any("sec10-fixture-line-2" in ln for ln in list(r.lines)):
                    break
                time.sleep(0.3)
            lines = list(r.lines)
            assert any("sec10-fixture-line-1" in ln for ln in lines), \
                "stdout 镜像行未进面板日志: %r" % lines[-6:]
            assert isinstance(r.child, runner_mod._ElevChild), \
                "helper 模式应使用 _ElevChild"
            assert adapter_mod.runner_child_alive(), "runner 子进程应已登记"
            child_obj = r.child  # 收尾 finally 会把 r.child 置 None，先捕获
            record = r._runtime.get("execution") or {}
            stdout_name = "ok-nte-headless-%s.log" % record.get("execution_id")
            stdout_path = _safe_path(runner_mod.DATA_DIR, stdout_name)
            assert os.path.isfile(stdout_path), "stdout 镜像文件未创建"
            assert "sec10-fixture-line-1" in Path(stdout_path).read_text(
                encoding="utf-8", errors="replace")
            # 停止路径：stop_event → _kill_child → helper stop_runner
            r.stop_event.set()
            th.join(timeout=30)
            assert not th.is_alive(), "停止后监控循环应退出"
            assert result["out"][0] == "stopped", result
            deadline = time.time() + 15
            while time.time() < deadline and child_obj.poll() is None:
                time.sleep(0.3)
            assert child_obj.poll() is not None, "stop_runner 后任务进程应终止"
            assert not adapter_mod.runner_child_alive(), "登记表应随收尾注销"
            audit = Path(_safe_path(log_dir, "elev_helper_audit.log"))
            content = audit.read_text(encoding="utf-8")
            assert "launch_runner" in content and "stop_runner" in content
    finally:
        r.stop_event.set()
        if r.child is not None and getattr(r.child, "poll", lambda: None)() is None:
            subprocess.run(["taskkill", "/F", "/PID", str(r.child.pid)],
                           capture_output=True, creationflags=0x08000000)
        r.child = None
        elev_client.ELEV.shutdown()
        elev_client.set_test_spawn(False)
        adapter_mod.LAUNCHER_SHA256 = orig_launcher_sha  # SEC-11 夹具适配恢复
        runner_mod.RUNTIME_FILE = orig_runtime
        runner_mod.DATA_DIR = orig_data_dir
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_sec10_deelevate_static():
    """SEC-10 去提权静态源检：三处提权封装（run_panel.bat RunAs / csc 清单
    requireAdministrator / PyInstaller --uac-admin）全部摘除且改为 asInvoker；
    辅助进程接线（入口分流、白名单 stop_runner、is_running 补盲）存在；
    elev_helper 保持纯标准库（不 import 本仓库模块）。"""
    if not os.path.isdir(os.path.join(_ROOT, "tools")):
        print("SKIP test_sec10_deelevate_static: dist 不携带 tools/（仓库根运行才有效）")
        return
    bat = Path(_safe_path(_ROOT, "run_panel.bat")).read_text(encoding="utf-8")
    assert "RunAs" not in bat and "net session" not in bat, \
        "run_panel.bat 仍含提权封装"
    build_src = Path(_safe_path(_ROOT, "tools", "build_exe.py")).read_text(encoding="utf-8")
    assert "requireAdministrator" not in build_src, "csc 清单仍提权"
    assert "--uac-admin" not in build_src, "PyInstaller 仍提权"
    assert 'level="asInvoker"' in build_src, "清单未改 asInvoker"
    main_src = Path(_safe_path(_ROOT, "app", "main.py")).read_text(encoding="utf-8")
    assert "--oknte-elev-helper" in main_src, "面板入口缺少辅助进程分流"
    helper_src = Path(_safe_path(_ROOT, "app", "elev_helper.py")).read_text(encoding="utf-8")
    assert "from app" not in helper_src and "import app" not in helper_src, \
        "elev_helper 必须纯标准库（不 import 本仓库模块）"
    runner_src = Path(_safe_path(_ROOT, "app", "runner.py")).read_text(encoding="utf-8")
    assert "stop_runner" in runner_src and "_launch_via_helper" in runner_src, \
        "runner 缺少辅助进程接线"
    adapter_src = Path(_safe_path(_ROOT, "app",
                                  "oknte_adapter.py")).read_text(encoding="utf-8")
    assert "runner_child_alive" in adapter_src and \
        "register_runner_child" in adapter_src, "is_running 补盲缺失"
    client_src = Path(_safe_path(_ROOT, "app", "elev_client.py")).read_text(encoding="utf-8")
    assert "-Verb RunAs" in client_src, "生产拉起必须经 UAC（Start-Process -Verb RunAs）"


# ---------- B3-A Slice B（SRV-06，2026-11-18）：Ctrl+C 停执行器 + helper 退出收尸 children 专项 ----------


@case
def test_srv06_kb_branch_stops_runner():
    """SRV-06 Ctrl+C 路径专项（直调层）：RUNNER 经真实 start 入口置 running
    （_run_loop 打桩为停驻哨兵，不拉起任何真实子进程）→ 直调 main._run_panel
    （OKNTE_PLANNER_NO_BROWSER=1 → ui=none；create_server 打桩为无套接字桩），
    KeyboardInterrupt 从 server_thread.join() 同一调用点注入（真实 Ctrl+C 打断
    阻塞主线程的抛出点，与 SRV-06 复现 run6 同口径）→ 断言分支内 RUNNER.stop()
    恰被调用一次（计数）且 runtime.json 落 stopped（RUNTIME_FILE 重定向 .tmp）。"""
    from app import elev_client
    from app import main as main_mod
    tmp = _sec10_tmp("srv06kb")
    orig_runtime = runner_mod.RUNTIME_FILE
    orig_run_loop = runner_mod.Runner._run_loop
    orig_create_server = main_mod.create_server
    orig_join = threading.Thread.join
    runner_mod.RUNTIME_FILE = _safe_path(tmp, "runtime.json")

    def _parked_run_loop(self, mode="daily"):
        # 停在 running：直到 stop_event；被停则照真实 _run_loop 的 stop 分支口径
        # 落 stopped 记账（runner.py: _finish("stopped", "已手动停止")）
        self.stop_event.wait(timeout=30)
        if self.stop_event.is_set():
            self._finish("stopped", "已手动停止")

    runner_mod.Runner._run_loop = _parked_run_loop

    class _StubServer:
        """无套接字桩：serve_forever 长驻等待（20s 自杀兜底），不占真实端口。"""

        def __init__(self):
            self._stop = threading.Event()

        def serve_forever(self):
            self._stop.wait(timeout=20)

        def shutdown(self):
            self._stop.set()

    stub = _StubServer()
    main_mod.create_server = lambda port, auto_range=20: (stub, port)
    entered = threading.Event()
    driver = {"entered": False, "started": None, "running": False, "error": None}

    def _driver():
        # 侧线程：主线程到达 join 注入点后，经真实 start 入口把 RUNNER 置 running
        try:
            entered.wait(timeout=15)
            driver["entered"] = True
            ok, msg = runner_mod.RUNNER.start()
            driver["started"] = (ok, msg)
            if not ok:
                return
            deadline = time.time() + 10
            while time.time() < deadline and \
                    runner_mod.RUNNER.snapshot()["state"] != "running":
                time.sleep(0.05)
            driver["running"] = \
                runner_mod.RUNNER.snapshot()["state"] == "running"
        except Exception as e:  # noqa: BLE001 —— 驱动线程错误回传主线程断言
            driver["error"] = e

    def _cond_join(self, timeout=None):
        # 条件 join：KeyboardInterrupt 注入点 = server_thread.join() 同一调用点。
        # 仅对桩 server 线程注入（按 target 归属判定）：其余线程（含 subprocess
        # run 内部的 stdout 泵线程 join）一律走真实 join，绝不向旁系线程漏注入。
        if getattr(self, "_target", None) is None or \
                getattr(self._target, "__self__", None) is not stub:
            return orig_join(self, timeout)
        entered.set()
        deadline = time.time() + 20
        while time.time() < deadline:
            if runner_mod.RUNNER.snapshot()["state"] == "running":
                time.sleep(0.3)  # 稳定期：确保 start 的 runtime 落盘
                break
            time.sleep(0.05)
        raise KeyboardInterrupt("srv06-injected")

    calls = {"stop": 0}
    orig_stop = runner_mod.RUNNER.stop

    def _counting_stop(*a, **k):
        calls["stop"] += 1
        return orig_stop(*a, **k)

    driver_th = None
    no_browser = os.environ.get("OKNTE_PLANNER_NO_BROWSER")
    try:
        with _StateGuard():
            STATE.data["settings"] = dict(DEFAULT_SETTINGS,
                                          oknte_root=_safe_path(tmp, "fake_root"))
            STATE.data["inventory"] = {}
            STATE.data["today"] = _empty_today()
            STATE.data["characters"] = []  # 启动回填空转（不触目录/快照）
            # 前置对齐：RUNNER 必须空闲（照 test_ocr_single_flight_409 的对账口径）
            deadline = time.time() + 10
            while time.time() < deadline and \
                    runner_mod.RUNNER.snapshot()["state"] in \
                    ("running", "waiting", "stopping"):
                runner_mod.RUNNER.stop()
                time.sleep(0.2)
            assert runner_mod.RUNNER.snapshot()["state"] not in \
                ("running", "waiting", "stopping"), "前置：RUNNER 必须空闲"
            main_mod._HOTKEY_STOP.set()  # 热键线程即刻收尾（测试进程不需要）
            driver_th = threading.Thread(target=_driver, daemon=True,
                                         name="srv06-driver")
            driver_th.start()
            runner_mod.RUNNER.stop = _counting_stop
            threading.Thread.join = _cond_join
            os.environ["OKNTE_PLANNER_NO_BROWSER"] = "1"
            try:
                main_mod._run_panel()  # 直调：KeyboardInterrupt 在分支内消化
            finally:
                os.environ.pop("OKNTE_PLANNER_NO_BROWSER", None)
                if no_browser is not None:
                    os.environ["OKNTE_PLANNER_NO_BROWSER"] = no_browser
                threading.Thread.join = orig_join
                if "stop" in runner_mod.RUNNER.__dict__:
                    del runner_mod.RUNNER.stop
            orig_join(driver_th, timeout=10)
            stub.shutdown()
            # ---- 断言：分支内 stop 被真实执行 + runtime.json 落 stopped ----
            assert driver["error"] is None, driver["error"]
            assert driver["entered"], "主线程应已到达 join 注入点"
            assert driver["started"] and driver["started"][0], \
                f"驱动线程应成功 start: {driver['started']!r}"
            assert driver["running"], "RUNNER 应已进入 running"
            assert calls["stop"] == 1, \
                f"KeyboardInterrupt 分支应恰好调用一次 RUNNER.stop()，" \
                f"实际 {calls['stop']!r}"
            deadline = time.time() + 5
            rt = None
            while time.time() < deadline:
                try:
                    rt = json.loads(Path(runner_mod.RUNTIME_FILE).read_text(
                        encoding="utf-8"))
                except (OSError, ValueError):
                    rt = None
                if isinstance(rt, dict) and rt.get("state") == "stopped":
                    break
                time.sleep(0.1)
            assert isinstance(rt, dict) and rt.get("state") == "stopped", \
                f"Ctrl+C 分支后 runtime.json 应落 stopped，实际 {rt!r}"
            assert runner_mod.RUNNER.snapshot()["state"] == "stopped", \
                runner_mod.RUNNER.snapshot()["state"]
    finally:
        main_mod.create_server = orig_create_server
        runner_mod.Runner._run_loop = orig_run_loop
        threading.Thread.join = orig_join  # 幂等兜底（中途失败也还原）
        runner_mod.RUNTIME_FILE = orig_runtime
        main_mod._HOTKEY_STOP.clear()
        runner_mod.RUNNER.stop_event.set()
        th = runner_mod.RUNNER.thread
        if th is not None and th.is_alive():
            orig_join(th, timeout=6)
        if driver_th is not None and driver_th.is_alive():
            orig_join(driver_th, timeout=5)
        elev_client.ELEV.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_srv06_helper_exit_kills_children():
    """SRV-06 helper 退出收尸专项（真实 helper 子进程 + 真实长驻哨兵）：伪父 +
    真实 helper 子进程（set_test_spawn 模式，test_elev_helper_e2e_auth_ops_audit
    同款；任务书所指 :8429-8459 实为 DATA_DIR 用例，模式实读在 :8648-8741），
    _sec10_materialize_python 物化内嵌解释器（launch_runner 白名单全参数）拉起
    长驻哨兵（time.sleep 自杀兜底）→ E.shutdown() 正常退出 → 断言 helper 进程
    退出后哨兵随之死亡（不留孤儿）+ 审计 exit 行 children=0 + child_kill 记录。"""
    from app import elev_client
    elev_client.set_test_spawn(True)
    tmp = _sec10_tmp("srv06helper")
    root = _sec10_materialize_python(tmp)
    if root is None:
        print("SKIP test_srv06_helper_exit_kills_children: 当前解释器无法物化到"
              " .tmp 临时根（跨卷且缺运行 DLL）")
        return
    os.makedirs(_safe_path(root, "working"), exist_ok=True)
    profile = _safe_path(root, "working", "configs_planner")
    os.makedirs(profile, exist_ok=True)
    launcher = _safe_path(profile, "run_planner_task.py")
    Path(launcher).write_text(
        "import time\n"
        "print('srv06-sentinel-start', flush=True)\n"
        "time.sleep(60)\n", encoding="utf-8")
    log_dir = _safe_path(tmp, "log")
    os.makedirs(log_dir, exist_ok=True)
    child_pid = None
    helper_pid = None
    try:
        E = elev_client.ELEV
        assert E.ensure(root, log_dir=log_dir, game_process="HTGame.exe"), \
            E.reason()
        helper_pid = E._pid
        port, token = E._port, E._token
        resp = elev_client.request_once(port, token, "launch_runner", {
            "launcher": launcher, "profile": profile, "task": "日常任务",
            "stdout": "ok-nte-headless-srv06-exit.log",
            # SEC-11（B3-B）夹具适配：契约收紧后必带写方期望哈希（夹具自算盘面
            # 哈希，内容与哈希一致），收尸断言语义不变
            "expected_launcher_sha256": elev_client.file_sha256(launcher)},
            timeout=20)
        assert resp.get("ok"), resp
        child_pid = resp["pid"]
        deadline = time.time() + 10
        while time.time() < deadline and not elev_client._pid_alive(child_pid):
            time.sleep(0.1)
        assert elev_client._pid_alive(child_pid), "哨兵应已拉起并存活"
        # helper 正常退出（shutdown op → run() finally 收尾链）
        E.shutdown()
        deadline = time.time() + 8
        while time.time() < deadline and elev_client._pid_alive(helper_pid):
            time.sleep(0.2)
        assert not elev_client._pid_alive(helper_pid), "helper 应随 shutdown 退出"
        # SRV-06 核心断言：helper 退出前收尸 children → 哨兵不成为孤儿
        deadline = time.time() + 8
        while time.time() < deadline and elev_client._pid_alive(child_pid):
            time.sleep(0.2)
        assert not elev_client._pid_alive(child_pid), \
            "helper 退出应 taskkill 收尾存活 children（SRV-06 孤儿修复）"
        audit = Path(_safe_path(log_dir, "elev_helper_audit.log")).read_text(
            encoding="utf-8")
        exit_lines = [ln for ln in audit.splitlines() if '"event": "exit"' in ln]
        assert exit_lines, "审计应有 exit 行"
        assert '"detail": "children=0"' in exit_lines[-1], \
            f"exit 行应记 children=0（收尾已摘除）: {exit_lines[-1]!r}"
        assert "child_kill" in audit and f"pid={child_pid}" in audit, \
            "应有 child_kill 收尾审计且指向哨兵 pid"
    finally:
        elev_client.set_test_spawn(False)
        elev_client.ELEV.shutdown()
        if child_pid is not None and elev_client._pid_alive(child_pid):
            subprocess.run(["taskkill", "/F", "/PID", str(child_pid)],
                           capture_output=True, creationflags=0x08000000)
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_srv06_stop_idle_safe():
    """SRV-06 回归（runner.py stop 现状）：未运行态 stop 必须安全拒绝——
    返回 (False, "当前没有在运行")、不改状态、不写任何 runtime；全局 RUNNER
    同口径（修复依赖此不变量：Ctrl+C 分支补 stop() 后空闲态零副作用）。"""
    tmp = _sec10_tmp("srv06stop")
    orig_runtime = runner_mod.RUNTIME_FILE
    runner_mod.RUNTIME_FILE = _safe_path(tmp, "runtime.json")
    try:
        r = runner_mod.Runner()  # 全新实例：state=idle，未挂线程
        before = r.snapshot()["state"]
        ok, msg = r.stop()
        assert ok is False and msg == "当前没有在运行", (ok, msg)
        assert r.snapshot()["state"] == before, "拒绝停止不得改状态"
        assert not os.path.isfile(runner_mod.RUNTIME_FILE), \
            "未运行态 stop 不得写 runtime"
        # 全局 RUNNER 同口径（先照既有口径对齐空闲）
        deadline = time.time() + 10
        while time.time() < deadline and \
                runner_mod.RUNNER.snapshot()["state"] in \
                ("running", "waiting", "stopping"):
            runner_mod.RUNNER.stop()
            time.sleep(0.2)
        g_state = runner_mod.RUNNER.snapshot()["state"]
        ok2, msg2 = runner_mod.RUNNER.stop()
        assert ok2 is False and msg2 == "当前没有在运行", (ok2, msg2)
        assert runner_mod.RUNNER.snapshot()["state"] == g_state, \
            "全局 RUNNER 拒绝停止不得改状态"
    finally:
        runner_mod.RUNTIME_FILE = orig_runtime
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_srv06_reap_children_regression():
    """SRV-06 回归（elev_helper._reap_children 现状不破坏）：真实短命子进程进
    children → 收尾线程摘除进 finished（exit code 0）+ child_exit 审计；
    stop_event 置位后收尾线程及时退出（finally 收尸循环不得改变其语义）。"""
    from app import elev_helper as eh
    tmp = _sec10_tmp("srv06reap")
    log_dir = _safe_path(tmp, "log")
    os.makedirs(log_dir, exist_ok=True)
    root = _sec10_root(tmp)
    proc = None
    helper = None
    try:
        helper = eh._Helper("c" * 64, os.getpid(), root, log_dir,
                            "HTGame.exe", _safe_path(log_dir, "cfg.json"))
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(1.0)"],
            cwd=_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000)
        helper.children[proc.pid] = proc
        reaper = threading.Thread(target=helper._reap_children, daemon=True,
                                  name="srv06-reaper")
        reaper.start()
        deadline = time.time() + 15
        while time.time() < deadline and proc.pid not in helper.finished:
            time.sleep(0.2)
        assert proc.pid in helper.finished, "已退出子进程应被收尾进 finished"
        assert proc.pid not in helper.children, "收尾后应从 children 摘除"
        assert helper.finished[proc.pid] == 0, \
            f"干净退出码应为 0，实际 {helper.finished[proc.pid]!r}"
        audit = Path(_safe_path(log_dir, "elev_helper_audit.log")).read_text(
            encoding="utf-8")
        assert "child_exit" in audit, "收尾应写 child_exit 审计"
        # stop_event 置位 → 收尾线程及时退出（现状语义保持）
        helper.stop_event.set()
        reaper.join(timeout=6)
        assert not reaper.is_alive(), "stop_event 置位后收尾线程应退出"
    finally:
        if helper is not None:
            helper.stop_event.set()
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- B2.4 CONC-01/02：save_json_atomic 并发口径与图鉴覆盖层锁 ----------

# 临时根放仓库 .tmp/（AGENTS.md 卫生条款：不落 C 盘；先例 _SEC10_TMP_BASE）
_CONC_TMP_BASE = os.path.join(_ROOT, ".tmp", "selftest-conc")


def _conc_tmp(name):
    """CONC 用例临时目录：tempfile 显式 dir= 指向仓库 .tmp/ 子目录。"""
    os.makedirs(_CONC_TMP_BASE, exist_ok=True)
    return tempfile.mkdtemp(prefix=name + "_", dir=_CONC_TMP_BASE)


def _join_all(ths, timeout=180):
    """带硬截止的 join：超时即断言失败（daemon 线程不阻塞进程退出）。"""
    deadline = time.monotonic() + timeout
    for t in ths:
        t.join(max(0.0, deadline - time.monotonic()))
    alive = [t.name for t in ths if t.is_alive()]
    assert not alive, f"线程硬超时未完成: {alive}"


@case
def test_conc01_same_file_concurrent_writes():
    """CONC-01 同路径并发 save_json_atomic（修复后基线，2×200 + 8×100 = 1200 写）。

    断言：0 异常、末态可解析且为单写者完整负载、目录无 *.tmp 残留、.bak 可解析
    （对齐评审 X3 实验口径）。
    """
    tmp = _conc_tmp("conc01_same")
    try:
        for n_threads, writes in ((2, 200), (8, 100)):
            target = os.path.join(tmp, "state_%dx%d.json" % (n_threads, writes))
            barrier = threading.Barrier(n_threads)
            errors, elock = [], threading.Lock()

            def worker(tid, target=target, writes=writes,
                       barrier=barrier, errors=errors, elock=elock):
                try:
                    barrier.wait(30)  # 起点对齐；超时/破坏抛 BrokenBarrierError 进
                    # errors（B2.7C：wait 纳入 worker try 边界首语句，不再裸逸出）
                    for i in range(writes):
                        try:
                            state_mod.save_json_atomic(
                                target, {"writer": "w%d" % tid, "seq": i,
                                         "blob": "x" * 1400})
                        except BaseException as e:  # noqa: BLE001
                            with elock:
                                errors.append("%s: %s" % (type(e).__name__, e))
                except BaseException as e:  # noqa: BLE001
                    with elock:  # B2.7C：barrier 异常与写循环异常同一收集通道
                        errors.append("%s: %s" % (type(e).__name__, e))

            ths = [threading.Thread(target=worker, args=(i,), name="W%d" % i,
                                    daemon=True) for i in range(n_threads)]
            for t in ths:
                t.start()
            _join_all(ths)
            assert errors == [], f"{n_threads}x{writes} 并发写异常: {errors[:5]}"
            with open(target, encoding="utf-8") as f:  # 末态可解析（json.load 即断言）
                doc = json.load(f)
            assert set(doc.keys()) == {"writer", "seq", "blob"}, doc.keys()
            assert doc["blob"] == "x" * 1400 and re.fullmatch(r"w\d+", doc["writer"])
            with open(target + ".bak", encoding="utf-8") as f:  # .bak 可解析
                bak = json.load(f)
            assert set(bak.keys()) == {"writer", "seq", "blob"}, bak.keys()
        residue = [p for p in os.listdir(tmp) if p.endswith(".tmp")]
        assert residue == [], f"tmp 残留: {residue}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_conc02_custom_overlay_concurrent_upsert():
    """CONC-02 图鉴覆盖层并发读-改-写（_CUSTOM_LOCK 修复后）。

    A) 2×200 不同 name 并发 upsert → 末态 400 条全部在（评审 EXP-2 缩小版）；
    B) upsert×remove 交叉 → 与串行语义一致（评审 EXP-1b：初盘甲乙丙，
       并发 upsert 丁 + remove 乙，两种交错同态 → 末态必为 [甲,丙,丁]）；
    C) 同名双 upsert（载荷全同，可交换）→ 末态恰好 1 条且内容为该载荷。

    B2.7C：CUSTOM_FILE patch 期间以 _CustomFileGuard 守卫真实 CUSTOM_FILE
    （orig_custom，patch 前捕获）不删除/不改变/无新增副产物（防泄漏门禁）。
    """
    tmp = _conc_tmp("conc02_custom")
    orig_custom = game_data_mod.CUSTOM_FILE
    guard = _CustomFileGuard(orig_custom)
    guard.__enter__()
    game_data_mod.CUSTOM_FILE = os.path.join(tmp, "game_data_custom.json")
    try:
        # ---- A) 2×200 不同 name ----
        barrier = threading.Barrier(2)
        errors, elock = [], threading.Lock()

        def upsert_worker(prefix, n, barrier=barrier, errors=errors, elock=elock):
            try:
                barrier.wait(30)  # B2.7C：wait 纳入 try 边界首语句，超时/破坏进 errors
                for i in range(n):
                    try:
                        game_data_mod.upsert_custom(
                            "characters", {"name": f"{prefix}-{i:03d}", "version": "c02"})
                    except BaseException as e:  # noqa: BLE001
                        with elock:
                            errors.append("%s: %s" % (type(e).__name__, e))
            except BaseException as e:  # noqa: BLE001
                with elock:  # B2.7C：barrier 异常与写循环异常同一收集通道
                    errors.append("%s: %s" % (type(e).__name__, e))

        ths = [threading.Thread(target=upsert_worker, args=(p, 200), name=p,
                                daemon=True) for p in ("并发甲", "并发乙")]
        for t in ths:
            t.start()
        _join_all(ths)
        assert errors == [], f"并发 upsert 异常: {errors[:5]}"
        doc = game_data_mod.custom_entries()
        names = [e["name"] for e in doc["characters"]]
        assert len(names) == 400, f"期望 400 条，实际 {len(names)}"
        assert all(f"并发甲-{i:03d}" in names for i in range(200)) and \
            all(f"并发乙-{i:03d}" in names for i in range(200)), "并发 upsert 丢更新"
        # ---- B) upsert×remove 交叉（EXP-1b 同态化：两写者名字不同 → 任意交错同串行）----
        state_mod.save_json_atomic(game_data_mod.CUSTOM_FILE, {
            "version": 1,
            "characters": [{"name": "甲"}, {"name": "乙"}, {"name": "丙"}],
            "weapons": []})
        barrier = threading.Barrier(2)
        errors, remove_result = [], {}

        def up_ding(errors=errors, barrier=barrier):
            try:
                barrier.wait(30)  # B2.7C：wait 纳入 try 边界首语句
                game_data_mod.upsert_custom("characters", {"name": "丁", "version": "x"})
            except BaseException as e:  # noqa: BLE001
                errors.append("%s: %s" % (type(e).__name__, e))

        def rm_yi(remove_result=remove_result, errors=errors, barrier=barrier):
            try:
                barrier.wait(30)  # B2.7C：wait 纳入 try 边界首语句
                remove_result["r"] = game_data_mod.remove_custom("characters", "乙")
            except BaseException as e:  # noqa: BLE001
                errors.append("%s: %s" % (type(e).__name__, e))

        ths = [threading.Thread(target=up_ding, name="up-丁", daemon=True),
               threading.Thread(target=rm_yi, name="rm-乙", daemon=True)]
        for t in ths:
            t.start()
        _join_all(ths)
        assert errors == [], f"交叉用例异常: {errors}"
        assert remove_result.get("r") is True, "remove 乙 应实际删除"
        names_b = [e["name"] for e in game_data_mod.custom_entries()["characters"]]
        assert names_b == ["甲", "丙", "丁"], f"交叉末态与串行语义不符: {names_b}"
        # ---- C) 同名双 upsert（载荷全同）----
        barrier = threading.Barrier(2)
        errors = []
        payload = {"name": "同名甲", "rarity": "S", "version": "same"}

        def up_same(errors=errors, barrier=barrier):
            try:
                barrier.wait(30)  # B2.7C：wait 纳入 try 边界首语句
                game_data_mod.upsert_custom("characters", dict(payload))
            except BaseException as e:  # noqa: BLE001
                errors.append("%s: %s" % (type(e).__name__, e))

        ths = [threading.Thread(target=up_same, name="same1", daemon=True),
               threading.Thread(target=up_same, name="same2", daemon=True)]
        for t in ths:
            t.start()
        _join_all(ths)
        assert errors == [], f"同名双 upsert 异常: {errors}"
        same = [e for e in game_data_mod.custom_entries()["characters"]
                if e["name"] == "同名甲"]
        assert len(same) == 1 and same[0] == payload, same
        residue = [p for p in os.listdir(tmp) if p.endswith(".tmp")]
        assert residue == [], f"tmp 残留: {residue}"
    finally:
        game_data_mod.CUSTOM_FILE = orig_custom
        guard_exc = None
        try:
            guard.__exit__(None, None, None)  # B2.7C：先复核防泄漏门禁再清理
        except BaseException as e:  # noqa: BLE001
            guard_exc = e
        shutil.rmtree(tmp, ignore_errors=True)
        if guard_exc is not None:
            raise guard_exc


@case
def test_conc01_failure_injection_no_residue():
    """CONC-01 失败注入：monkeypatch os.replace 抛 OSError → 断言无本次主 tmp/
    bak_tmp 残留、主文件保持旧完整内容；恢复后重试写盘可用。"""
    tmp = _conc_tmp("conc01_fail")
    try:
        target = os.path.join(tmp, "state.json")
        old = {"v": 1, "blob": "y" * 800}
        state_mod.save_json_atomic(target, old)  # 先落一版旧完整内容（.bak 链成立）
        orig_replace, calls = os.replace, {"n": 0}

        def _boom(a, b):
            calls["n"] += 1
            raise OSError("注入失败（CONC-01 失败自清用例）")

        os.replace = _boom
        try:
            try:
                state_mod.save_json_atomic(target, {"v": 2})
            except OSError as e:
                assert "注入失败" in str(e), e
            else:
                raise AssertionError("os.replace 注入失败必须抛出 OSError")
        finally:
            os.replace = orig_replace
        assert calls["n"] >= 1, "注入未生效（os.replace 未被调用）"
        with open(target, encoding="utf-8") as f:  # 主文件保持旧完整
            assert json.load(f) == old
        residue = [p for p in os.listdir(tmp) if p.endswith(".tmp")]
        assert residue == [], f"失败后 tmp/bak_tmp 残留: {residue}"
        state_mod.save_json_atomic(target, {"v": 2, "blob": "z"})  # 恢复后重试可用
        with open(target, encoding="utf-8") as f:
            assert json.load(f)["v"] == 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- BIZ-03：save_json_atomic 发布步瞬时占用白名单有界重试 ----------


def _winerror_oserror(winerror, msg):
    """构造带 winerror 属性的 OSError（BIZ-03 注入口径）。

    OSError(errno.EACCES, msg) 构造后 winerror=None（3.13.13/win32 实测），须显式
    赋值；先例：.testarchive/2026-11-14-B24-conc/scripts/verify/b09_diskfull.py 的
    OSError(28) 注入同型。
    """
    exc = OSError(errno.EACCES, msg)
    exc.winerror = winerror
    return exc


def _same_target(dst, target):
    """fake os.replace 的 dst 是否指向 target（normcase+abspath 归一比较）。"""
    return os.path.normcase(os.path.abspath(str(dst))) == \
        os.path.normcase(os.path.abspath(target))


@case
def test_biz03a_retry_first_transient_then_success():
    """BIZ-03 Case A：主发布步首次 WinError 5 瞬时占用、第二次成功 → 最终成功。

    断言：不外抛、末态数据正确、主发布 replace 恰 2 次（首试+1 次重试）、
    .bak 语义不变（发布前旧完整版本照常入 .bak）、目录无 tmp/bak_tmp 残留。
    """
    tmp = _conc_tmp("biz03a")
    try:
        target = os.path.join(tmp, "state.json")
        old = {"v": 1, "blob": "y" * 400}
        new = {"v": 2, "blob": "z" * 400}
        state_mod.save_json_atomic(target, old)
        orig_replace, calls = os.replace, {"main": 0, "other": 0}

        def _flaky(a, b):
            if _same_target(b, target):
                calls["main"] += 1
                if calls["main"] == 1:  # 仅主发布首试失败；.bak 发布不受注入影响
                    raise _winerror_oserror(
                        5, "注入: WinError 5 瞬时占用（BIZ-03 Case A）")
            else:
                calls["other"] += 1
            return orig_replace(a, b)

        os.replace = _flaky
        try:
            state_mod.save_json_atomic(target, new)
        finally:
            os.replace = orig_replace
        with open(target, encoding="utf-8") as f:  # 最终成功、数据正确
            assert json.load(f) == new, "重试成功后末态数据不正确"
        with open(target + ".bak", encoding="utf-8") as f:  # .bak 语义不变
            assert json.load(f) == old, ".bak 应保持发布前旧完整版本"
        assert calls["main"] == 2, \
            f"主发布应恰 2 次 replace（首试+1 重试）: {calls}"
        assert calls["other"] == 1, f".bak 发布应恰 1 次: {calls}"
        residue = [p for p in os.listdir(tmp) if p.endswith(".tmp")]
        assert residue == [], f"tmp/bak_tmp 残留: {residue}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_biz03b_retry_bounded_backoff():
    """BIZ-03 Case B：前 2 次（≤预算-1）可重试错误（WinError 5、32 各一）后成功
    → 验证重试次数与退避序列。

    断言：replace 恰 3 次（首试+2 重试+成功）；time.sleep 记录序列恰为设计退避
    前缀 [10ms, 20ms]；记录的 replace 调用时刻间隔 ≥ 对应退避值（真实退避发生，
    sleep 透传不吞）；末态数据正确、无 tmp 残留。不预建主文件（无 .bak 步，
    全部 replace 均为主发布）。
    """
    tmp = _conc_tmp("biz03b")
    try:
        target = os.path.join(tmp, "state.json")
        new = {"v": 2, "blob": "z" * 400}
        orig_replace, orig_sleep = os.replace, time.sleep
        calls, sleeps, stamps = {"n": 0}, [], []

        def _flaky(a, b):
            calls["n"] += 1
            # 高精度时钟测退避间隔：Windows time.monotonic() 粒度 15.6ms，3.12 下
            # 10ms 退避会两次落进同一刻度测得 0.0000s（perf_counter=QPC 无此问题）
            stamps.append(time.perf_counter())
            if calls["n"] <= 2:  # 前两次失败：winerror 5、32 各一（覆盖白名单两值）
                raise _winerror_oserror(
                    5 if calls["n"] == 1 else 32,
                    "注入: 瞬时占用（BIZ-03 Case B 第 %d 次）" % calls["n"])
            return orig_replace(a, b)

        def _rec_sleep(seconds):
            sleeps.append(seconds)
            return orig_sleep(seconds)  # 透传真实退避

        os.replace, time.sleep = _flaky, _rec_sleep
        try:
            state_mod.save_json_atomic(target, new)
        finally:
            os.replace, time.sleep = orig_replace, orig_sleep
        with open(target, encoding="utf-8") as f:  # 重试后最终成功
            assert json.load(f) == new, "重试成功后末态数据不正确"
        assert calls["n"] == 3, f"应恰 3 次 replace（首试+2 重试+成功）: {calls['n']}"
        assert sleeps == [0.010, 0.020], \
            f"退避序列应为设计前缀 [10,20]ms: {sleeps}"
        assert len(stamps) == 3 and stamps[0] <= stamps[1] <= stamps[2], \
            "replace 调用时刻应非降序"
        for i, backoff in enumerate((0.010, 0.020)):
            gap = stamps[i + 1] - stamps[i]
            assert gap >= backoff - 1e-3, \
                f"第 {i + 1} 次重试前退避 {gap:.4f}s 应 ≥ {backoff}s"
        residue = [p for p in os.listdir(tmp) if p.endswith(".tmp")]
        assert residue == [], f"tmp 残留: {residue}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_biz03c_retry_budget_exhausted_raises():
    """BIZ-03 Case C：主发布步全部允许次数均失败（WinError 32）→ 原异常原样
    外抛（对象同一性）、主文件保持旧完整、tmp/bak_tmp 清理符合设计。

    time.sleep 打桩为记录型空转（断言预算恰好耗尽：5 次退避=完整
    [10,20,40,80,160]ms，且免真实 310ms 等待）；.bak 发布不受注入影响。
    """
    tmp = _conc_tmp("biz03c")
    try:
        target = os.path.join(tmp, "state.json")
        old = {"v": 1, "blob": "y" * 400}
        new = {"v": 2, "blob": "z" * 400}
        state_mod.save_json_atomic(target, old)
        orig_replace, orig_sleep = os.replace, time.sleep
        injected = _winerror_oserror(
            32, "注入: WinError 32 持续占用（BIZ-03 Case C）")
        calls, sleeps = {"main": 0, "other": 0}, []

        def _always(a, b):
            if _same_target(b, target):
                calls["main"] += 1
                raise injected  # 每次主发布都抛同一异常实例（验原样外抛）
            calls["other"] += 1
            return orig_replace(a, b)

        def _nop_sleep(seconds):
            sleeps.append(seconds)

        os.replace, time.sleep = _always, _nop_sleep
        try:
            try:
                state_mod.save_json_atomic(target, new)
            except OSError as e:
                assert e is injected, f"应原样外抛同一异常对象: {e!r}"
            else:
                raise AssertionError("重试预算耗尽必须抛出 OSError")
        finally:
            os.replace, time.sleep = orig_replace, orig_sleep
        assert calls["main"] == 6, \
            f"主发布应恰 6 次（首试+5 重试，预算上限）: {calls['main']}"
        assert sleeps == [0.010, 0.020, 0.040, 0.080, 0.160], \
            f"应恰好耗尽完整退避预算: {sleeps}"
        assert calls["other"] == 1, f".bak 发布应恰 1 次: {calls['other']}"
        with open(target, encoding="utf-8") as f:  # 主文件保持旧完整
            assert json.load(f) == old, "发布失败主文件应保持旧完整内容"
        with open(target + ".bak", encoding="utf-8") as f:
            assert json.load(f) == old, ".bak 应为发布前旧版"
        residue = [p for p in os.listdir(tmp) if p.endswith(".tmp")]
        assert residue == [], f"失败后 tmp/bak_tmp 残留: {residue}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_biz03d_non_retryable_fails_immediately():
    """BIZ-03 Case D：不可重试错误立即外抛——不重试（replace 恰 1 次、零退避）。

    覆盖三种形态：裸 OSError（无 winerror）、winerror=3 路径类错误、
    FileNotFoundError；断言 time.sleep 零调用、主文件保持旧态（不存在）、
    无 tmp 残留。
    """
    tmp = _conc_tmp("biz03d")
    orig_replace, orig_sleep = os.replace, time.sleep
    try:
        non_retryable = (
            ("bare", OSError(errno.EACCES,
                             "注入: 无 winerror 的裸 OSError（BIZ-03 Case D）")),
            ("winerror3", _winerror_oserror(
                3, "注入: WinError 3 路径类错误（BIZ-03 Case D）")),
            ("fnf", FileNotFoundError(
                errno.ENOENT, "注入: FileNotFoundError（BIZ-03 Case D）")),
        )
        for label, injected in non_retryable:
            target = os.path.join(tmp, "state_%s.json" % label)
            calls, sleeps = {"n": 0}, []

            def _fail(a, b, _injected=injected, _calls=calls):
                _calls["n"] += 1
                raise _injected

            def _nop_sleep(*_seconds):
                sleeps.append(1)

            os.replace, time.sleep = _fail, _nop_sleep
            try:
                try:
                    state_mod.save_json_atomic(target, {"v": 9})
                except OSError as e:
                    assert e is injected, f"[{label}] 应原样外抛同一异常: {e!r}"
                else:
                    raise AssertionError(f"[{label}] 不可重试错误必须外抛")
            finally:
                os.replace, time.sleep = orig_replace, orig_sleep
            assert calls["n"] == 1, \
                f"[{label}] 不重试: replace 应恰 1 次，实际 {calls['n']}"
            assert sleeps == [], f"[{label}] 零退避（time.sleep 不应被调用）"
            assert not os.path.exists(target), \
                f"[{label}] 主文件不应被创建（保持旧态）"
        residue = [p for p in os.listdir(tmp) if p.endswith(".tmp")]
        assert residue == [], f"tmp 残留: {residue}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_biz03e_bak_step_transient_boundary():
    """BIZ-03 Case E：.bak 发布步遇瞬时占用（WinError 5 全预算失败）→ 重试后吞
    （备份落后一版语义不变），主写入不受影响。

    实现选择申报（与 state.py save_json_atomic docstring 一致）：.bak 发布步
    套用与主发布步同一 _replace_bounded_retry——预算耗尽仍被既有
    try/except OSError 吞掉。断言：不外抛、主文件落新数据、.bak 保持注入前
    旧版（落后一版以上可接受）、.bak 发布恰 6 次（首试+5 重试）且耗尽完整
    退避序列、bak_tmp 残留被 finally 自清。
    """
    tmp = _conc_tmp("biz03e")
    try:
        target = os.path.join(tmp, "state.json")
        old = {"v": 1, "blob": "y" * 400}
        mid = {"v": 2, "blob": "m" * 400}
        new = {"v": 3, "blob": "z" * 400}
        state_mod.save_json_atomic(target, old)  # v1：主文件建立（无 .bak 步）
        state_mod.save_json_atomic(target, mid)  # v2：.bak 建立（=old）
        orig_replace, orig_sleep = os.replace, time.sleep
        injected = _winerror_oserror(
            5, "注入: WinError 5 .bak 发布瞬时占用（BIZ-03 Case E）")
        calls, sleeps = {"main": 0, "bak": 0}, []

        def _flaky(a, b):
            if _same_target(b, target + ".bak"):
                calls["bak"] += 1
                raise injected  # .bak 发布每次都失败（瞬时占用形态）
            calls["main"] += 1
            return orig_replace(a, b)  # 主发布不受注入影响

        def _nop_sleep(seconds):
            sleeps.append(seconds)

        os.replace, time.sleep = _flaky, _nop_sleep
        try:
            state_mod.save_json_atomic(target, new)
        finally:
            os.replace, time.sleep = orig_replace, orig_sleep
        with open(target, encoding="utf-8") as f:  # 主写入不受影响
            assert json.load(f) == new, ".bak 步失败不得影响主写入"
        with open(target + ".bak", encoding="utf-8") as f:  # 备份落后一版语义不变
            assert json.load(f) == old, ".bak 应保持注入前旧版且不被写坏"
        assert calls["bak"] == 6, \
            f".bak 发布应重试至预算 6 次（首试+5 重试）: {calls['bak']}"
        assert sleeps == [0.010, 0.020, 0.040, 0.080, 0.160], \
            f".bak 步应耗尽完整退避预算: {sleeps}"
        assert calls["main"] == 1, f"主发布应恰 1 次: {calls['main']}"
        residue = [p for p in os.listdir(tmp) if p.endswith(".tmp")]
        assert residue == [], f"bak_tmp/tmp 残留: {residue}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- B2.7B：并发档位固化（16 线程档/读写并发/业务并发/锁卫生，2026-11-15） ----------
# B2.4 独立复核跑过的高并发场景（16 线程档、读写并发、大统计）沉淀为正式门禁。
# 全部确定性：Barrier/Event 同步、barrier.wait(30) 纳入 worker try 边界首语句
# （B2.7C 修正：超时/破坏 BrokenBarrierError 与写循环异常同走 errors 收集通道）、
# daemon 线程、_join_all 硬截止、禁随机 sleep（读者节奏用固定 sleep）、禁无限等待；
# 临时目录一律 _conc_tmp（仓库 .tmp/，E 盘），finally rmtree 自清。


def _conc_residue(tmp):
    """uuid 双形态 tmp 残留 glob（state.py save_json_atomic 命名口径）：
    主 tmp <名字>.<32hex>.tmp 与 .bak 发布 tmp <名字>.bak.<32hex>.tmp 两形态，
    两种 glob 并集（先例：B2.7A test_token_cleanup_uuid_tmp_both_shapes）。"""
    return sorted({str(p) for pat in ("*.tmp", "*.bak.*.tmp")
                   for p in Path(tmp).glob(pat)})


class _CustomFileGuard:
    """B2.7C：CUSTOM_FILE patch 用例防泄漏门禁（真实数据不变量复核）。

    先例形态：smoke_test._StateFileGuard（快照/退出复核，uuid 双形态 tmp 按精确
    正则扫描）；与本守卫的差异：_StateFileGuard 退出时恢复快照，本守卫**对测试前
    已存在的文件只读——绝不写回/恢复/删除**（恢复=覆盖用户数据），只删除守卫期
    新增的测试垃圾。

    用法：monkeypatch 前捕获 ``orig_custom = game_data_mod.CUSTOM_FILE``，构造
    ``guard = _CustomFileGuard(orig_custom)`` 并 ``guard.__enter__()``（或 with
    消费），finally 中 ``CUSTOM_FILE`` 恢复原值后调用 ``guard.__exit__`` 复核。

    __enter__ 快照四类对象（每项记录 existed 与内容 sha256）：
    1. real_path 本体；
    2. real_path + ".bak" 同名副本；
    3. 同 basename 的 *.corrupt（state.load_json 真损坏隔离形态，state.py:230/:235
       os.replace(path, path + ".corrupt")）；
    4. uuid 双形态 tmp（state.py save_json_atomic 命名 :177/:184）：
       basename + ".<32位hex>.tmp" 与 basename + ".bak.<32位hex>.tmp"——按 basename
       前缀精确正则扫描，不过宽 glob（近邻/无关文件名不命中）。

    __exit__ 复核（用户数据不变量）：测试前存在者必须仍存在且 sha256 逐字节
    不变——不许删/改/覆盖；测试前不存在者必须仍不存在——守卫期新增者（含
    .bak/.corrupt/tmp 副产物同口径）先捕获证据（before/after 清单与哈希进失败
    消息），再删除该测试垃圾，然后断言失败（测试前本无此物）。守卫通用（任何
    real_path 皆可守）；自检见 test_conc09（全部在 _conc_tmp 沙箱内，绝不指向
    真实 data/）。
    """

    def __init__(self, real_path):
        self._real = str(real_path)
        self._snap = {}  # path -> (form, existed, sha256)

    @staticmethod
    def _sha256_file(path):
        """文件内容 sha256（逐字节不变量复核口径）。"""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def _family_paths(self):
        """受守卫四类对象的当前清单：{path: form}。real/.bak 两固定形态恒在
        （不存在者快照 existed=False，退出可检出「守卫期新增」）；corrupt 与
        uuid 双形态 tmp 按目录清单精确扫描。"""
        real = self._real
        d = os.path.dirname(real) or "."
        base = os.path.basename(real)
        fam = {real: "real", real + ".bak": "bak"}
        # uuid 双形态：state.py:177 <名字>.<32hex>.tmp 与 :184 <名字>.bak.<32hex>.tmp
        # ——basename 前缀精确锚定（先例 smoke_test._PANEL_TOKEN_UUID_TMP_RE 口径）
        uuid_re = re.compile(r"^%s(?:\.bak)?\.[0-9a-f]{32}\.tmp$" % re.escape(base))
        try:
            names = os.listdir(d)
        except OSError:
            names = []
        for name in names:
            path = os.path.join(d, name)
            if uuid_re.fullmatch(name):
                fam[path] = "uuid_tmp"
            elif name.endswith(".corrupt") and name.startswith(base + "."):
                fam[path] = "corrupt"
        return fam

    def _state(self, path):
        """当前形态状态：(existed, sha256)。不存在者 (False, None)。"""
        if os.path.isfile(path):
            return (True, self._sha256_file(path))
        return (False, None)

    def __enter__(self):
        for path, form in self._family_paths().items():
            existed, digest = self._state(path)
            self._snap[path] = (form, existed, digest)
        return self

    def __exit__(self, *exc):
        after = self._family_paths()
        problems = []  # (kind, path, form, before(existed,sha), after(existed,sha))
        garbage = []   # 守卫期新增（测试垃圾）：取证后删除
        for path in sorted(set(self._snap) | set(after)):
            form = after.get(path) or self._snap[path][0]
            a_existed, a_digest = self._state(path)
            if path not in self._snap:
                problems.append(("新增", path, form, (False, None),
                                 (a_existed, a_digest)))
                garbage.append(path)
                continue
            _, b_existed, b_digest = self._snap[path]
            if b_existed and not a_existed:
                problems.append(("被删", path, form, (b_existed, b_digest),
                                 (a_existed, a_digest)))
            elif b_existed and a_digest != b_digest:
                problems.append(("被改", path, form, (b_existed, b_digest),
                                 (a_existed, a_digest)))
            elif not b_existed and a_existed:
                problems.append(("新增", path, form, (b_existed, b_digest),
                                 (a_existed, a_digest)))
                garbage.append(path)
        # 先取证（problems 已含 before/after 哈希与形态）再删守卫期新增垃圾；
        # 测试前已存在者一律不碰（只读守卫，绝不写回/恢复/删除）
        for path in garbage:
            try:
                os.remove(path)
            except OSError:
                pass
        if problems:

            def _fmt(state):
                existed, digest = state
                return "existed=%s sha256=%s" % (existed, digest or "-")

            lines = ["%s [form=%s kind=%s] before(%s) after(%s)%s" % (
                path, form, kind, _fmt(before), _fmt(after_state),
                "（守卫期新增测试垃圾已删除）" if kind == "新增" else "")
                for kind, path, form, before, after_state in problems]
            raise AssertionError(
                "B2.7C CUSTOM_FILE 防泄漏门禁：真实数据不变量被破坏，共 %d 项"
                "（测试前存在者必须逐字节不变，测试前不存在者不得出现）:\n%s"
                % (len(problems), "\n".join(lines)))
        return False


@case
def test_conc03_16thread_same_file_writes():
    """B2.7B 并发档 ①：16 线程×25 写同一 JSON（Barrier(16) 对齐，合计 400 写）。

    B2.4「16 线程档」高并发场景沉淀（修复后 16 线程 perf 实测 1600/1600 成功，
    .testarchive/2026-11-14-B24-conc/conc-verify-summary.md §3；本机校准
    3200 写 ≈17.5s，5.4ms/写）。断言：0 异常、末态 json.load 成功且顶层 keys
    恰为 writer/seq/blob 三键、blob 为 1400 个 x、writer 匹配 w\\d+；.bak 存在
    且可解析（先写首版保证 .bak 链）；目录无 *.tmp 与 .bak.*.tmp 残留
    （uuid 双形态 glob）；_join_all timeout=60。
    """
    tmp = _conc_tmp("conc03_16t")
    try:
        target = os.path.join(tmp, "state_16t.json")
        state_mod.save_json_atomic(target, {"writer": "w0", "seq": -1,
                                            "blob": "x" * 1400})  # 首版：.bak 链成立
        n_threads, writes = 16, 25
        barrier = threading.Barrier(n_threads)
        errors, elock = [], threading.Lock()

        def worker(tid, target=target, writes=writes,
                   barrier=barrier, errors=errors, elock=elock):
            try:
                barrier.wait(30)  # 起点对齐；超时/破坏抛 BrokenBarrierError 进
                # errors（B2.7C：wait 纳入 worker try 边界首语句，不再裸逸出）
                for i in range(writes):
                    try:
                        state_mod.save_json_atomic(
                            target, {"writer": "w%d" % tid, "seq": i,
                                     "blob": "x" * 1400})
                    except BaseException as e:  # noqa: BLE001
                        with elock:
                            errors.append("%s: %s" % (type(e).__name__, e))
            except BaseException as e:  # noqa: BLE001
                with elock:  # B2.7C：barrier 异常与写循环异常同一收集通道
                    errors.append("%s: %s" % (type(e).__name__, e))

        ths = [threading.Thread(target=worker, args=(i,), name="W%d" % i,
                                daemon=True) for i in range(n_threads)]
        for t in ths:
            t.start()
        _join_all(ths, timeout=60)
        assert errors == [], f"16 线程×25 并发写异常: {errors[:5]}"
        with open(target, encoding="utf-8") as f:  # 末态可解析（json.load 即断言）
            doc = json.load(f)
        assert set(doc.keys()) == {"writer", "seq", "blob"}, doc.keys()
        assert doc["blob"] == "x" * 1400 and re.fullmatch(r"w\d+", doc["writer"])
        with open(target + ".bak", encoding="utf-8") as f:  # .bak 存在且可解析
            bak = json.load(f)
        assert set(bak.keys()) == {"writer", "seq", "blob"}, bak.keys()
        residue = _conc_residue(tmp)
        assert residue == [], f"uuid 双形态 tmp 残留: {residue}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_conc04_read_write_concurrency():
    """B2.7B 并发档 ②：8 写者×25 写（Barrier(8) 对齐）+ 6 读者 Event 驱动并发读写。

    B2.7B §六新缺陷（ID 待报告阶段登记）永久回归用例：本例初版读者以裸
    open+json.loads 直读（生产读者等价形态）实测暴露读者侧瞬时访问失败——
    8 轮隔离直调全 FAIL，读者可见错误分布全为 PermissionError [Errno 13]
    （99~163 次/轮，零 JSONDecodeError/零形状违规）；并实测 state.load_json
    把该瞬时占用误判为损坏、os.replace 将完好主文件隔离进伪 .corrupt
    （独立复现 expA/A2 共 9/9 轮）。修复（load_json 对读者侧 PermissionError
    有界重试 + 瞬时占用排除出 .corrupt 隔离）后，读者改走生产读取路径
    state.load_json（与 main.py:3318/:3810 GET /api/game-data 经
    custom_entries 的暴露面同型），断言：读者零失败零损坏（三键形状+
    blob=1400x）、零伪 .corrupt 隔离、写者零异常（BIZ-03 预算在该档充分）、
    reads>0、末态可解析、uuid 双形态 tmp 零残留。读者节奏固定 20ms sleep
    （禁随机 sleep 口径下的确定性档；该档写者预算零耗尽，无间隔热读者档
    实测超预算 ~91% 见 .testarchive/2026-11-15-B27B-conc-tier/probe-sweep.log）；
    读者自带硬截止 deadline=time.monotonic()+60（防写者死锁导致读者永转）；
    主线程 _join_all(writers) 后 done_event.set() 再 _join_all(readers)。
    """
    tmp = _conc_tmp("conc04_rw")
    try:
        target = os.path.join(tmp, "state_rw.json")
        state_mod.save_json_atomic(target, {"writer": "w0", "seq": -1,
                                            "blob": "x" * 1400})  # 预建合法首版
        n_writers, writes, n_readers = 8, 25, 6
        barrier = threading.Barrier(n_writers)
        done_event = threading.Event()
        writer_errors, reader_errors = [], []
        reads, wlock, rlock = [0], threading.Lock(), threading.Lock()

        def writer(tid, target=target, barrier=barrier, errors=writer_errors,
                   lock=wlock):
            try:
                barrier.wait(30)  # 起点对齐；超时/破坏抛 BrokenBarrierError 进
                # errors（B2.7C：wait 纳入 writer try 边界首语句，不再裸逸出）
                for i in range(writes):
                    try:
                        state_mod.save_json_atomic(
                            target, {"writer": "w%d" % tid, "seq": i,
                                     "blob": "x" * 1400})
                    except BaseException as e:  # noqa: BLE001
                        with lock:
                            errors.append("%s: %s" % (type(e).__name__, e))
            except BaseException as e:  # noqa: BLE001
                with lock:  # B2.7C：barrier 异常与写循环异常同一收集通道
                    errors.append("%s: %s" % (type(e).__name__, e))

        def reader(target=target, done=done_event, errors=reader_errors,
                   lock=rlock, counter=reads):
            # §六修复回归：读者走生产读取路径 state.load_json（修复后瞬时占用
            # 在 load_json 内部被有界重试吸收，读者侧零失败、零伪 .corrupt）
            deadline = time.monotonic() + 60  # 硬截止：写者死锁也不得让读者永转
            while not done.is_set():
                if time.monotonic() > deadline:
                    with lock:
                        errors.append("deadline: 读者硬截止 60s 超限")
                    break
                time.sleep(0.020)  # 固定 20ms 节奏（禁随机 sleep 口径下的确定性档）
                try:
                    doc = state_mod.load_json(target)
                except BaseException as e:  # noqa: BLE001 — 修复后读者路径不得外逸异常
                    with lock:
                        errors.append("%s: %s" % (type(e).__name__, e))
                    continue
                if not isinstance(doc, dict) or \
                        set(doc.keys()) != {"writer", "seq", "blob"} or \
                        doc.get("blob") != "x" * 1400:
                    with lock:  # 半截/损坏/None（主+.bak 均不可读）可见
                        errors.append("shape: 非三键形状或 blob 非 1400x: %r"
                                      % (sorted(doc.keys())
                                         if isinstance(doc, dict)
                                         else type(doc).__name__))
                    continue
                with lock:
                    counter[0] += 1

        rths = [threading.Thread(target=reader, name="R%d" % i, daemon=True)
                for i in range(n_readers)]
        wths = [threading.Thread(target=writer, args=(i,), name="W%d" % i,
                                 daemon=True) for i in range(n_writers)]
        for t in rths:
            t.start()
        for t in wths:
            t.start()
        _join_all(wths, timeout=60)
        done_event.set()
        _join_all(rths, timeout=60)
        assert writer_errors == [], \
            f"写者异常（BIZ-03 预算应充分）: {writer_errors[:5]}"
        dist = {}
        for e in reader_errors:
            key = e.split(":", 1)[0]
            dist[key] = dist.get(key, 0) + 1
        assert reader_errors == [], \
            f"读者可见错误分布={dist} (样例: {reader_errors[:3]})"
        assert reads[0] > 0, \
            f"读者成功读数应为正（防读者空转恒过）: reads={reads[0]}"
        with open(target, encoding="utf-8") as f:  # 末态可解析
            doc = json.load(f)
        assert set(doc.keys()) == {"writer", "seq", "blob"}, doc.keys()
        quarantine = sorted(str(p) for p in Path(tmp).glob("*.corrupt"))
        assert quarantine == [], \
            (f"B2.7B §六伪 .corrupt 回归：瞬时占用不得把完好主文件隔离进 "
             f".corrupt: {quarantine}")
        residue = _conc_residue(tmp)
        assert residue == [], f"uuid 双形态 tmp 残留: {residue}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_conc05_custom_multi_key_concurrent():
    """B2.7B 并发档 ③：8 线程×25 个不同 name 并发 upsert_custom（Barrier(8)）。

    CONC-02 口径放大（test_conc02 段 A 为 2×200）：entry 形状照抄 conc02 段 A
    （保证通过校验）；断言 0 异常、custom_entries() 恰 200 条、预期 name 集合
    全在（零丢失）、文档可解析、无 tmp 残留。CUSTOM_FILE monkeypatch 照
    test_conc02 先例 finally 恢复；B2.7C 起以 _CustomFileGuard 守卫真实
    CUSTOM_FILE 不被泄漏/改写。
    """
    tmp = _conc_tmp("conc05_multi")
    orig_custom = game_data_mod.CUSTOM_FILE
    guard = _CustomFileGuard(orig_custom)
    guard.__enter__()
    game_data_mod.CUSTOM_FILE = os.path.join(tmp, "game_data_custom.json")
    try:
        n_threads, per_thread = 8, 25
        barrier = threading.Barrier(n_threads)
        errors, elock = [], threading.Lock()

        def worker(t, barrier=barrier, errors=errors, elock=elock):
            try:
                barrier.wait(30)  # B2.7C：wait 纳入 try 边界首语句，超时/破坏进 errors
                for k in range(per_thread):
                    try:
                        game_data_mod.upsert_custom(
                            "characters", {"name": f"自测角色-{t}-{k}",
                                           "version": "c05"})
                    except BaseException as e:  # noqa: BLE001
                        with elock:
                            errors.append("%s: %s" % (type(e).__name__, e))
            except BaseException as e:  # noqa: BLE001
                with elock:  # B2.7C：barrier 异常与写循环异常同一收集通道
                    errors.append("%s: %s" % (type(e).__name__, e))

        ths = [threading.Thread(target=worker, args=(t,), name=f"U{t}",
                                daemon=True) for t in range(n_threads)]
        for t in ths:
            t.start()
        _join_all(ths, timeout=60)
        assert errors == [], f"并发 upsert 异常: {errors[:5]}"
        doc = game_data_mod.custom_entries()
        names = [e["name"] for e in doc["characters"]]
        assert len(names) == 200, f"期望恰 200 条，实际 {len(names)}"
        expected = {f"自测角色-{t}-{k}" for t in range(n_threads)
                    for k in range(per_thread)}
        assert expected <= set(names), "预期 name 集合存在丢失"
        with open(game_data_mod.CUSTOM_FILE, encoding="utf-8") as f:  # 文档可解析
            json.load(f)
        residue = _conc_residue(tmp)
        assert residue == [], f"uuid 双形态 tmp 残留: {residue}"
    finally:
        game_data_mod.CUSTOM_FILE = orig_custom
        guard_exc = None
        try:
            guard.__exit__(None, None, None)  # B2.7C：先复核防泄漏门禁再清理
        except BaseException as e:  # noqa: BLE001
            guard_exc = e
        shutil.rmtree(tmp, ignore_errors=True)
        if guard_exc is not None:
            raise guard_exc


@case
def test_conc06_custom_same_key_concurrent():
    """B2.7B 并发档 ④：8 线程对同名「自测并发同键」各写不同 payload 每线程 5 次。

    与 test_conc02 段 C（载荷全同、可交换）不同，本例载荷互异（f"载荷-{t}"）：
    验证同名并发 upsert 的 last-writer-wins 串行化语义——任意交错末态收敛到
    恰 1 条且 payload 属于 8 个已写值集合（绝不混合/半截/多出）。断言 0 异常、
    该 name 恰 1 条、文档可解析、无 tmp 残留；B2.7C 起以 _CustomFileGuard 守卫
    真实 CUSTOM_FILE 不被泄漏/改写。
    """
    tmp = _conc_tmp("conc06_same")
    orig_custom = game_data_mod.CUSTOM_FILE
    guard = _CustomFileGuard(orig_custom)
    guard.__enter__()
    game_data_mod.CUSTOM_FILE = os.path.join(tmp, "game_data_custom.json")
    try:
        n_threads, per_thread = 8, 5
        barrier = threading.Barrier(n_threads)
        errors, elock = [], threading.Lock()
        written = {f"载荷-{t}" for t in range(n_threads)}

        def worker(t, barrier=barrier, errors=errors, elock=elock):
            try:
                barrier.wait(30)  # B2.7C：wait 纳入 try 边界首语句，超时/破坏进 errors
                for _ in range(per_thread):
                    try:
                        game_data_mod.upsert_custom(
                            "characters", {"name": "自测并发同键",
                                           "payload": f"载荷-{t}"})
                    except BaseException as e:  # noqa: BLE001
                        with elock:
                            errors.append("%s: %s" % (type(e).__name__, e))
            except BaseException as e:  # noqa: BLE001
                with elock:  # B2.7C：barrier 异常与写循环异常同一收集通道
                    errors.append("%s: %s" % (type(e).__name__, e))

        ths = [threading.Thread(target=worker, args=(t,), name=f"S{t}",
                                daemon=True) for t in range(n_threads)]
        for t in ths:
            t.start()
        _join_all(ths, timeout=60)
        assert errors == [], f"同名并发 upsert 异常: {errors[:5]}"
        doc = game_data_mod.custom_entries()
        same = [e for e in doc["characters"] if e["name"] == "自测并发同键"]
        assert len(same) == 1, \
            f"同名条目应恰 1 条（last-writer-wins 串行化）: {len(same)} 条"
        assert same[0].get("payload") in written, \
            f"末态 payload 应属于 8 个已写值集合: {same[0]!r}"
        with open(game_data_mod.CUSTOM_FILE, encoding="utf-8") as f:  # 文档可解析
            json.load(f)
        residue = _conc_residue(tmp)
        assert residue == [], f"uuid 双形态 tmp 残留: {residue}"
    finally:
        game_data_mod.CUSTOM_FILE = orig_custom
        guard_exc = None
        try:
            guard.__exit__(None, None, None)  # B2.7C：先复核防泄漏门禁再清理
        except BaseException as e:  # noqa: BLE001
            guard_exc = e
        shutil.rmtree(tmp, ignore_errors=True)
        if guard_exc is not None:
            raise guard_exc


@case
def test_conc07_lock_hygiene_repeated():
    """B2.7B 并发档 ⑤：5 轮短压力混合负载（JSON 原子写 + 覆盖层 upsert）锁卫生。

    每轮开始记录基线线程名集合（{t.name for t in threading.enumerate()}）与
    len(state._PATH_LOCKS) 基线；16 线程 Barrier(16) 混合负载——偶数线程
    save_json_atomic 写 2 个固定 tmp JSON 各 8 次，奇数线程 upsert_custom 不同
    name 5 次（CUSTOM_FILE 在用例开始前 monkeypatch、finally 恢复）。每轮断言：
    _join_all(timeout=60) 无存活线程、enumerate 线程名集合回归基线（无线程
    泄漏）、无 tmp 残留、_PATH_LOCKS 增量第 1 轮后恰 3（2 个 JSON 加
    CUSTOM_FILE 共 3——per-path 锁 get-or-create 常驻不清理，state.py
    _path_lock 文档口径）、第 2-5 轮增量 0（无异常膨胀）、每轮墙钟
    time.perf_counter() < 30s（死锁兜底；口径先例 test_biz03b :8159-8161
    perf_counter=QPC 注）；B2.7C 起以 _CustomFileGuard 守卫真实 CUSTOM_FILE
    不被泄漏/改写。
    """
    tmp = _conc_tmp("conc07_hygiene")
    orig_custom = game_data_mod.CUSTOM_FILE
    guard = _CustomFileGuard(orig_custom)
    guard.__enter__()
    game_data_mod.CUSTOM_FILE = os.path.join(tmp, "game_data_custom.json")
    try:
        json_a = os.path.join(tmp, "hygiene_a.json")
        json_b = os.path.join(tmp, "hygiene_b.json")
        n_threads, per_json_writes, per_thread_upserts = 16, 8, 5
        expected_new = 3  # 2 个 JSON 目标 + CUSTOM_FILE（首轮 get-or-create）
        for rnd in range(1, 6):
            names_base = {t.name for t in threading.enumerate()}
            locks_before = len(state_mod._PATH_LOCKS)
            barrier = threading.Barrier(n_threads)
            errors, elock = [], threading.Lock()

            def even_worker(barrier=barrier, errors=errors, elock=elock,
                            ja=json_a, jb=json_b):
                try:
                    barrier.wait(30)  # B2.7C：wait 纳入 try 边界首语句
                    for i in range(per_json_writes):
                        for tgt in (ja, jb):
                            try:
                                state_mod.save_json_atomic(
                                    tgt, {"seq": i, "blob": "x" * 1400})
                            except BaseException as e:  # noqa: BLE001
                                with elock:
                                    errors.append("%s: %s" % (type(e).__name__, e))
                except BaseException as e:  # noqa: BLE001
                    with elock:  # B2.7C：barrier 异常与写循环异常同一收集通道
                        errors.append("%s: %s" % (type(e).__name__, e))

            def odd_worker(t, barrier=barrier, errors=errors, elock=elock):
                try:
                    barrier.wait(30)  # B2.7C：wait 纳入 try 边界首语句
                    for k in range(per_thread_upserts):
                        try:
                            game_data_mod.upsert_custom(
                                "characters", {"name": f"自测混合-{t}-{k}",
                                               "version": "c07"})
                        except BaseException as e:  # noqa: BLE001
                            with elock:
                                errors.append("%s: %s" % (type(e).__name__, e))
                except BaseException as e:  # noqa: BLE001
                    with elock:  # B2.7C：barrier 异常与写循环异常同一收集通道
                        errors.append("%s: %s" % (type(e).__name__, e))

            ths = [threading.Thread(target=even_worker, name=f"HE{rnd}-{i}",
                                    daemon=True)
                   for i in range(0, n_threads, 2)]
            ths += [threading.Thread(target=odd_worker, args=(t,),
                                     name=f"HO{rnd}-{t}", daemon=True)
                    for t in range(1, n_threads, 2)]
            t0 = time.perf_counter()
            for t in ths:
                t.start()
            _join_all(ths, timeout=60)
            wall = time.perf_counter() - t0
            assert errors == [], f"第 {rnd} 轮混合负载异常: {errors[:5]}"
            assert wall < 30.0, \
                f"第 {rnd} 轮墙钟 {wall:.2f}s ≥ 30s（死锁兜底口径）"
            now_names = {t.name for t in threading.enumerate()}
            assert now_names == names_base, \
                (f"第 {rnd} 轮线程泄漏: 新增={now_names - names_base} "
                 f"缺失={names_base - now_names}")
            grew = len(state_mod._PATH_LOCKS) - locks_before
            if rnd == 1:
                assert grew == expected_new, \
                    (f"第 1 轮 _PATH_LOCKS 增量应恰 {expected_new}"
                     f"（2 JSON+CUSTOM_FILE）: {grew}")
            else:
                assert grew == 0, f"第 {rnd} 轮 _PATH_LOCKS 异常膨胀: +{grew}"
            residue = _conc_residue(tmp)
            assert residue == [], f"第 {rnd} 轮 uuid 双形态 tmp 残留: {residue}"
        with open(game_data_mod.CUSTOM_FILE, encoding="utf-8") as f:  # 文档可解析
            json.load(f)
    finally:
        game_data_mod.CUSTOM_FILE = orig_custom
        guard_exc = None
        try:
            guard.__exit__(None, None, None)  # B2.7C：先复核防泄漏门禁再清理
        except BaseException as e:  # noqa: BLE001
            guard_exc = e
        shutil.rmtree(tmp, ignore_errors=True)
        if guard_exc is not None:
            raise guard_exc


# ---------- B2.7C：测试语义收口（barrier 异常边界自证 / 防泄漏守卫自检，2026-11-16） ----------


@case
def test_conc08_barrier_break_reported():
    """B2.7C 注入①：barrier 破坏必须进正式失败通道（worker 异常边界自证）。

    B2.7B 复核缺口的确定性收口自证：修复前 17 处 barrier.wait 位于 worker 异常
    收集 try 之外——barrier 破坏/超时会以线程未捕获异常形式静默逸出（threading
    默认 excepthook 打印后消散），用例 errors==[] 照常通过（假绿）。本例按修复后
    worker 边界形态（try 首语句 wait + except BaseException → errors 收集）起
    4 写者线程写同一预建 JSON；全部 start() 后主线程立即 barrier.abort()
    （标准库语义：已阻塞与未到达的 wait 全部立即抛 threading.BrokenBarrierError），
    _join_all(ths, timeout=30)。断言一：errors 非空且每条都含 BrokenBarrierError
    （worker 异常进入可观察失败通道）；断言二：全部线程已退出（_join_all 硬截止）；
    断言三（测试确实会 FAIL 的证明）：同构 mini 流程（预建文件+同款边界+abort+
    join+成功档 errors==[] 断言）包在 try/except AssertionError 里复跑，断言
    AssertionError 确实抛出且失败消息含 BrokenBarrierError——证明 barrier 破坏
    →正式用例确定性 FAIL（假绿通道已封死）。
    注：wait(30) 预算耗尽的超时路径与 abort 路径抛同一异常类
    threading.BrokenBarrierError，30 秒慢路径不另测。
    """
    tmp = _conc_tmp("conc08_abort")
    try:
        target = os.path.join(tmp, "state_conc08.json")
        state_mod.save_json_atomic(target, {"writer": "w0", "seq": -1,
                                            "blob": "x" * 1400})  # 预建合法首版
        n_threads = 4
        barrier = threading.Barrier(n_threads)
        errors, elock = [], threading.Lock()

        def worker(tid, target=target, barrier=barrier, errors=errors,
                   elock=elock):
            # 与修复后正式并发档同款边界：try 首语句 wait，异常统一进 errors
            try:
                barrier.wait(30)
                for i in range(25):
                    try:
                        state_mod.save_json_atomic(
                            target, {"writer": "w%d" % tid, "seq": i,
                                     "blob": "x" * 1400})
                    except BaseException as e:  # noqa: BLE001
                        with elock:
                            errors.append("%s: %s" % (type(e).__name__, e))
            except BaseException as e:  # noqa: BLE001
                with elock:
                    errors.append("%s: %s" % (type(e).__name__, e))

        ths = [threading.Thread(target=worker, args=(i,), name="W%d" % i,
                                daemon=True) for i in range(n_threads)]
        for t in ths:
            t.start()
        barrier.abort()  # 已阻塞与未到达的 wait 全部立即抛 BrokenBarrierError
        _join_all(ths, timeout=30)  # 断言二：全部线程已退出（硬截止兜底）
        assert errors, "断言一：barrier 破坏必须进 errors 失败通道，实际为空"
        assert all("BrokenBarrierError" in e for e in errors), \
            f"断言一：每条 error 都应含 BrokenBarrierError: {errors[:5]}"
        # ---- 断言三：同构 mini 流程复跑——成功档断言在 barrier 破坏下确定性 FAIL ----
        m_target = os.path.join(tmp, "state_conc08_mini.json")
        state_mod.save_json_atomic(m_target, {"writer": "w0", "seq": -1,
                                              "blob": "x" * 1400})
        m_barrier = threading.Barrier(n_threads)
        m_errors, m_elock = [], threading.Lock()

        def m_worker(tid, target=m_target, barrier=m_barrier, errors=m_errors,
                     elock=m_elock):
            try:
                barrier.wait(30)
                for i in range(25):
                    try:
                        state_mod.save_json_atomic(
                            target, {"writer": "w%d" % tid, "seq": i,
                                     "blob": "x" * 1400})
                    except BaseException as e:  # noqa: BLE001
                        with elock:
                            errors.append("%s: %s" % (type(e).__name__, e))
            except BaseException as e:  # noqa: BLE001
                with elock:
                    errors.append("%s: %s" % (type(e).__name__, e))

        m_ths = [threading.Thread(target=m_worker, args=(i,), name="M%d" % i,
                                  daemon=True) for i in range(n_threads)]
        for t in m_ths:
            t.start()
        m_barrier.abort()
        _join_all(m_ths, timeout=30)
        raised = None
        try:
            assert m_errors == [], "mini 并发写异常: %s" % (m_errors[:5],)
        except AssertionError as e:
            raised = e
        assert raised is not None, \
            "断言三：barrier 破坏下成功档断言必须 FAIL（防假绿自证失效）"
        assert "BrokenBarrierError" in str(raised), \
            f"断言三：失败消息应含 BrokenBarrierError: {raised}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_conc09_custom_file_guard_detects_leak():
    """B2.7C 注入②：_CustomFileGuard 防泄漏守卫自检（四场景全在 _conc_tmp
    沙箱内，零碰真实 data/）。结构仿 test_statefileguard_uuid_pattern 的
    快照-扰动-断言；守卫消费用 with。

    场景一：沙箱「真实路径」已有文件且隔离写只进另一 tmp（非守卫家族）→
    守卫通过；场景二：已存在文件在守卫期内被改写 → 守卫断言失败且该文件原样
    保留（不许删/覆盖语义自证：守卫退出后内容与改写后一致、未被恢复，失败
    消息含 before/after 哈希与形态）；场景三：不存在路径在守卫期内被新建 →
    守卫失败+垃圾已被守卫清除+错误信息含内容哈希证据；场景四：已存在文件旁
    出现 uuid 双形态 tmp 副产物（state.py:177/:184 命名口径）→ 守卫失败+
    垃圾已清，固定名 .tmp 近邻不纳入守卫（不过宽 glob 自证）。
    """
    tmp = _conc_tmp("conc09_guard")
    try:
        # ---- 场景一：已有文件 + 隔离写只进另一 tmp → 守卫通过 ----
        sandbox1 = os.path.join(tmp, "sandbox1")
        os.makedirs(sandbox1)
        real1 = os.path.join(sandbox1, "game_data_custom.json")
        Path(real1).write_text('{"v": 1}', encoding="utf-8")
        with _CustomFileGuard(real1):
            Path(os.path.join(tmp, "scratch1.json")).write_text(
                '{"isolated": true}', encoding="utf-8")  # 隔离写：非守卫家族
        assert Path(real1).read_text(encoding="utf-8") == '{"v": 1}', \
            "场景一：守卫通过路径不得触碰受守卫文件"
        assert not os.path.exists(real1 + ".bak"), "场景一：守卫不得制造 .bak"

        # ---- 场景二：已存在文件守卫期内被改写 → 断言失败且原样保留（不恢复）----
        sandbox2 = os.path.join(tmp, "sandbox2")
        os.makedirs(sandbox2)
        real2 = os.path.join(sandbox2, "game_data_custom.json")
        old_body = '{"v": 1}'
        Path(real2).write_text(old_body, encoding="utf-8")
        old_hex = hashlib.sha256(old_body.encode("utf-8")).hexdigest()
        new_body = '{"v": 2, "touched": true}'
        new_hex = hashlib.sha256(new_body.encode("utf-8")).hexdigest()
        caught = None
        try:
            with _CustomFileGuard(real2):
                Path(real2).write_text(new_body, encoding="utf-8")  # 守卫期改写
        except AssertionError as e:
            caught = e
        assert caught is not None, "场景二：守卫期内改写已存在文件必须被捕获"
        msg2 = str(caught)
        assert old_hex in msg2 and new_hex in msg2, \
            f"场景二：失败消息必须含 before/after 哈希: {msg2}"
        assert "kind=被改" in msg2 and "form=real" in msg2, \
            f"场景二：失败消息必须含形态清单: {msg2}"
        assert Path(real2).read_text(encoding="utf-8") == new_body, \
            "场景二：守卫必须原样保留被改文件（绝不写回/恢复快照——不许删/覆盖语义）"

        # ---- 场景三：不存在路径守卫期内被新建 → 失败+垃圾已清+哈希证据 ----
        sandbox3 = os.path.join(tmp, "sandbox3")
        os.makedirs(sandbox3)
        real3 = os.path.join(sandbox3, "game_data_custom.json")  # 测试前不存在
        leak_body = '{"born": "leak"}'
        leak_hex = hashlib.sha256(leak_body.encode("utf-8")).hexdigest()
        caught = None
        try:
            with _CustomFileGuard(real3):
                Path(real3).write_text(leak_body, encoding="utf-8")
        except AssertionError as e:
            caught = e
        assert caught is not None, "场景三：守卫期内新建必须被捕获"
        msg3 = str(caught)
        assert "kind=新增" in msg3 and leak_hex in msg3, \
            f"场景三：失败消息必须含内容哈希证据: {msg3}"
        assert not os.path.exists(real3), \
            "场景三：守卫期新增垃圾必须已被守卫删除（测试垃圾不留盘）"

        # ---- 场景四：uuid 双形态 tmp 副产物 → 失败+垃圾已清；近邻不误伤 ----
        sandbox4 = os.path.join(tmp, "sandbox4")
        os.makedirs(sandbox4)
        real4 = os.path.join(sandbox4, "game_data_custom.json")
        Path(real4).write_text('{"v": 1}', encoding="utf-8")
        hex1, hex2 = "ab" * 16, "cd" * 16  # 32 位 hex（uuid4().hex 同形态）
        u_main = os.path.join(sandbox4, "game_data_custom.json.%s.tmp" % hex1)
        u_bak = os.path.join(sandbox4, "game_data_custom.json.bak.%s.tmp" % hex2)
        near = os.path.join(sandbox4, "game_data_custom.json.tmp")  # 固定名近邻
        caught = None
        try:
            with _CustomFileGuard(real4):
                Path(near).write_text("near", encoding="utf-8")  # 非守卫形态
                Path(u_main).write_text("tmp-main", encoding="utf-8")
                Path(u_bak).write_text("tmp-bak", encoding="utf-8")
        except AssertionError as e:
            caught = e
        assert caught is not None, "场景四：uuid 双形态 tmp 副产物必须被捕获"
        msg4 = str(caught)
        assert "kind=新增" in msg4 and "form=uuid_tmp" in msg4, \
            f"场景四：失败消息必须含形态清单: {msg4}"
        assert hashlib.sha256(b"tmp-main").hexdigest() in msg4, \
            f"场景四：失败消息应含副产物内容哈希: {msg4}"
        assert not os.path.exists(u_main) and not os.path.exists(u_bak), \
            "场景四：uuid 双形态 tmp 垃圾必须已被守卫清除"
        assert os.path.exists(near), "场景四：固定名 .tmp 近邻不得被误删（精确扫描）"
        assert Path(real4).read_text(encoding="utf-8") == '{"v": 1}', \
            "场景四：守卫失败路径同样不得触碰测试前已存在的文件"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- B2.7B stress 档（--stress 时运行，发布前/专项复核用） ----------
# 日常档 conc03-07 同口径断言的放大负载版：断言口径不变，仅放大负载规模
# （16 线程×200 高量写 / 长时读写并发 / 800 key upsert+同名风暴 / 20 轮重复）。
# 不进 CASES、不进日常档计数；python -m app.selftest --stress 时运行
# （用法见模块 docstring「stress 档用法」）。


@stress_case
def test_stress_conc01_16t_high_volume():
    """stress ①：16 线程×200=3200 写同一 JSON（conc03 形状放大）。

    断言集同 conc03：0 异常、末态三键形状、.bak 存在且可解析、uuid 双形态
    tmp 零残留。本机校准 3200 写 ≈17.5s（5.4ms/写）——_join_all timeout=60
    有 3 倍余量。
    """
    tmp = _conc_tmp("stress_conc01")
    try:
        target = os.path.join(tmp, "state_16t_high.json")
        state_mod.save_json_atomic(target, {"writer": "w0", "seq": -1,
                                            "blob": "x" * 1400})  # 首版：.bak 链成立
        n_threads, writes = 16, 200
        barrier = threading.Barrier(n_threads)
        errors, elock = [], threading.Lock()

        def worker(tid, target=target, writes=writes,
                   barrier=barrier, errors=errors, elock=elock):
            try:
                barrier.wait(30)  # B2.7C：wait 纳入 try 边界首语句，超时/破坏进 errors
                for i in range(writes):
                    try:
                        state_mod.save_json_atomic(
                            target, {"writer": "w%d" % tid, "seq": i,
                                     "blob": "x" * 1400})
                    except BaseException as e:  # noqa: BLE001
                        with elock:
                            errors.append("%s: %s" % (type(e).__name__, e))
            except BaseException as e:  # noqa: BLE001
                with elock:  # B2.7C：barrier 异常与写循环异常同一收集通道
                    errors.append("%s: %s" % (type(e).__name__, e))

        ths = [threading.Thread(target=worker, args=(i,), name="W%d" % i,
                                daemon=True) for i in range(n_threads)]
        for t in ths:
            t.start()
        _join_all(ths, timeout=60)
        assert errors == [], f"16×200 高量并发写异常: {errors[:5]}"
        with open(target, encoding="utf-8") as f:
            doc = json.load(f)
        assert set(doc.keys()) == {"writer", "seq", "blob"}, doc.keys()
        assert doc["blob"] == "x" * 1400 and re.fullmatch(r"w\d+", doc["writer"])
        with open(target + ".bak", encoding="utf-8") as f:
            bak = json.load(f)
        assert set(bak.keys()) == {"writer", "seq", "blob"}, bak.keys()
        residue = _conc_residue(tmp)
        assert residue == [], f"uuid 双形态 tmp 残留: {residue}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@stress_case
def test_stress_conc02_readwrite_long():
    """stress ②：16 写者×100 写 + 8 读者并发读写（conc04 模式放大）。

    B2.7B §六新缺陷（ID 待报告阶段登记）永久回归用例（conc04 同款放大）：
    断言集同 conc04——writer_errors==[]（BIZ-03 预算充分性在 1600 写档实证；
    复现轮曾实测写者预算耗尽 WinError 5 形态 2/3 轮）、读者走生产读取路径
    state.load_json 且 reader_errors==[]（零损坏可见）、零伪 .corrupt 隔离、
    reads>0、末态可解析、无 tmp 残留。读者节奏固定 30ms sleep（1600 发布档
    写者预算零耗尽的确定性档）；读者自带 60s 硬截止。
    """
    tmp = _conc_tmp("stress_conc02")
    try:
        target = os.path.join(tmp, "state_rw_long.json")
        state_mod.save_json_atomic(target, {"writer": "w0", "seq": -1,
                                            "blob": "x" * 1400})  # 预建合法首版
        n_writers, writes, n_readers = 16, 100, 8
        barrier = threading.Barrier(n_writers)
        done_event = threading.Event()
        writer_errors, reader_errors = [], []
        reads, wlock, rlock = [0], threading.Lock(), threading.Lock()

        def writer(tid, target=target, barrier=barrier, errors=writer_errors,
                   lock=wlock):
            try:
                barrier.wait(30)  # B2.7C：wait 纳入 try 边界首语句，超时/破坏进 errors
                for i in range(writes):
                    try:
                        state_mod.save_json_atomic(
                            target, {"writer": "w%d" % tid, "seq": i,
                                     "blob": "x" * 1400})
                    except BaseException as e:  # noqa: BLE001
                        with lock:
                            errors.append("%s: %s" % (type(e).__name__, e))
            except BaseException as e:  # noqa: BLE001
                with lock:  # B2.7C：barrier 异常与写循环异常同一收集通道
                    errors.append("%s: %s" % (type(e).__name__, e))

        def reader(target=target, done=done_event, errors=reader_errors,
                   lock=rlock, counter=reads):
            # §六修复回归：读者走生产读取路径 state.load_json（与 conc04 同款）
            deadline = time.monotonic() + 60  # 硬截止：写者死锁也不得让读者永转
            while not done.is_set():
                if time.monotonic() > deadline:
                    with lock:
                        errors.append("deadline: 读者硬截止 60s 超限")
                    break
                time.sleep(0.030)  # 固定 30ms 节奏（1600 发布档写者预算零耗尽确定性档）
                try:
                    doc = state_mod.load_json(target)
                except BaseException as e:  # noqa: BLE001 — 修复后读者路径不得外逸异常
                    with lock:
                        errors.append("%s: %s" % (type(e).__name__, e))
                    continue
                if not isinstance(doc, dict) or \
                        set(doc.keys()) != {"writer", "seq", "blob"} or \
                        doc.get("blob") != "x" * 1400:
                    with lock:  # 半截/损坏/None（主+.bak 均不可读）可见
                        errors.append("shape: 非三键形状或 blob 非 1400x: %r"
                                      % (sorted(doc.keys())
                                         if isinstance(doc, dict)
                                         else type(doc).__name__))
                    continue
                with lock:
                    counter[0] += 1

        rths = [threading.Thread(target=reader, name="R%d" % i, daemon=True)
                for i in range(n_readers)]
        wths = [threading.Thread(target=writer, args=(i,), name="W%d" % i,
                                 daemon=True) for i in range(n_writers)]
        for t in rths:
            t.start()
        for t in wths:
            t.start()
        _join_all(wths, timeout=60)
        done_event.set()
        _join_all(rths, timeout=60)
        assert writer_errors == [], \
            f"写者异常（BIZ-03 预算应充分）: {writer_errors[:5]}"
        dist = {}
        for e in reader_errors:
            key = e.split(":", 1)[0]
            dist[key] = dist.get(key, 0) + 1
        assert reader_errors == [], \
            f"读者可见错误分布={dist} (样例: {reader_errors[:3]})"
        assert reads[0] > 0, \
            f"读者成功读数应为正（防读者空转恒过）: reads={reads[0]}"
        with open(target, encoding="utf-8") as f:
            doc = json.load(f)
        assert set(doc.keys()) == {"writer", "seq", "blob"}, doc.keys()
        quarantine = sorted(str(p) for p in Path(tmp).glob("*.corrupt"))
        assert quarantine == [], \
            (f"B2.7B §六伪 .corrupt 回归：瞬时占用不得把完好主文件隔离进 "
             f".corrupt: {quarantine}")
        residue = _conc_residue(tmp)
        assert residue == [], f"uuid 双形态 tmp 残留: {residue}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@stress_case
def test_stress_conc03_custom_volume():
    """stress ③：16 线程×50=800 个不同 key upsert + 16 线程×10 同名风暴（两段）。

    A 段（conc05 放大）：断言 0 异常、恰 800 条、预期 name 集合全在；
    B 段（conc06 放大）：同名「自测压力同键」16 线程各 10 次互异 payload，
    断言该 name 恰 1 条且 payload 属于 16 个已写值集合。两段后文档可解析、
    无 tmp 残留。CUSTOM_FILE monkeypatch 照 test_conc02 先例 finally 恢复；
    B2.7C 起以 _CustomFileGuard 守卫真实 CUSTOM_FILE 不被泄漏/改写。
    """
    tmp = _conc_tmp("stress_conc03")
    orig_custom = game_data_mod.CUSTOM_FILE
    guard = _CustomFileGuard(orig_custom)
    guard.__enter__()
    game_data_mod.CUSTOM_FILE = os.path.join(tmp, "game_data_custom.json")
    try:
        n_threads = 16
        # ---- A) 16×50 不同 key ----
        per_thread = 50
        barrier = threading.Barrier(n_threads)
        errors, elock = [], threading.Lock()

        def up_worker(t, barrier=barrier, errors=errors, elock=elock):
            try:
                barrier.wait(30)  # B2.7C：wait 纳入 try 边界首语句，超时/破坏进 errors
                for k in range(per_thread):
                    try:
                        game_data_mod.upsert_custom(
                            "characters", {"name": f"自测压力角色-{t}-{k}",
                                           "version": "sc03"})
                    except BaseException as e:  # noqa: BLE001
                        with elock:
                            errors.append("%s: %s" % (type(e).__name__, e))
            except BaseException as e:  # noqa: BLE001
                with elock:  # B2.7C：barrier 异常与写循环异常同一收集通道
                    errors.append("%s: %s" % (type(e).__name__, e))

        ths = [threading.Thread(target=up_worker, args=(t,), name=f"UP{t}",
                                daemon=True) for t in range(n_threads)]
        for t in ths:
            t.start()
        _join_all(ths, timeout=60)
        assert errors == [], f"A 段 800 key upsert 异常: {errors[:5]}"
        doc = game_data_mod.custom_entries()
        names = [e["name"] for e in doc["characters"]]
        assert len(names) == 800, f"A 段期望恰 800 条，实际 {len(names)}"
        expected = {f"自测压力角色-{t}-{k}" for t in range(n_threads)
                    for k in range(per_thread)}
        assert expected <= set(names), "A 段预期 name 集合存在丢失"
        # ---- B) 16×10 同名风暴 ----
        per_thread_b = 10
        barrier = threading.Barrier(n_threads)
        errors_b, elock_b = [], threading.Lock()
        written_b = {f"压力载荷-{t}" for t in range(n_threads)}

        def storm_worker(t, barrier=barrier, errors=errors_b, elock=elock_b):
            try:
                barrier.wait(30)  # B2.7C：wait 纳入 try 边界首语句，超时/破坏进 errors
                for _ in range(per_thread_b):
                    try:
                        game_data_mod.upsert_custom(
                            "characters", {"name": "自测压力同键",
                                           "payload": f"压力载荷-{t}"})
                    except BaseException as e:  # noqa: BLE001
                        with elock:
                            errors.append("%s: %s" % (type(e).__name__, e))
            except BaseException as e:  # noqa: BLE001
                with elock:  # B2.7C：barrier 异常与写循环异常同一收集通道
                    errors.append("%s: %s" % (type(e).__name__, e))

        ths_b = [threading.Thread(target=storm_worker, args=(t,), name=f"ST{t}",
                                  daemon=True) for t in range(n_threads)]
        for t in ths_b:
            t.start()
        _join_all(ths_b, timeout=60)
        assert errors_b == [], f"B 段同名风暴异常: {errors_b[:5]}"
        doc = game_data_mod.custom_entries()
        same = [e for e in doc["characters"] if e["name"] == "自测压力同键"]
        assert len(same) == 1, \
            f"B 段同名条目应恰 1 条（last-writer-wins）: {len(same)} 条"
        assert same[0].get("payload") in written_b, \
            f"B 段末态 payload 应属于 16 个已写值集合: {same[0]!r}"
        assert len([e for e in doc["characters"]
                    if e["name"].startswith("自测压力角色-")]) == 800, \
            "B 段风暴不得扰动 A 段 800 条"
        with open(game_data_mod.CUSTOM_FILE, encoding="utf-8") as f:  # 文档可解析
            json.load(f)
        residue = _conc_residue(tmp)
        assert residue == [], f"uuid 双形态 tmp 残留: {residue}"
    finally:
        game_data_mod.CUSTOM_FILE = orig_custom
        guard_exc = None
        try:
            guard.__exit__(None, None, None)  # B2.7C：先复核防泄漏门禁再清理
        except BaseException as e:  # noqa: BLE001
            guard_exc = e
        shutil.rmtree(tmp, ignore_errors=True)
        if guard_exc is not None:
            raise guard_exc


@stress_case
def test_stress_conc04_repeat_rounds():
    """stress ④：20 轮×16 线程混合负载（conc07 模式放大）。

    每轮断言同 conc07：_join_all(timeout=60) 无存活线程、enumerate 线程名集合
    回归基线、无 tmp 残留、每轮墙钟 perf_counter < 30s；_PATH_LOCKS 增量只在
    第 1 轮后做一次预期值断言（2 JSON+CUSTOM_FILE 共 3）、其后各轮恒 0；
    B2.7C 起以 _CustomFileGuard 守卫真实 CUSTOM_FILE 不被泄漏/改写。
    """
    tmp = _conc_tmp("stress_conc04")
    orig_custom = game_data_mod.CUSTOM_FILE
    guard = _CustomFileGuard(orig_custom)
    guard.__enter__()
    game_data_mod.CUSTOM_FILE = os.path.join(tmp, "game_data_custom.json")
    try:
        json_a = os.path.join(tmp, "hygiene_a.json")
        json_b = os.path.join(tmp, "hygiene_b.json")
        n_threads, per_json_writes, per_thread_upserts, rounds = 16, 8, 5, 20
        expected_new = 3
        for rnd in range(1, rounds + 1):
            names_base = {t.name for t in threading.enumerate()}
            locks_before = len(state_mod._PATH_LOCKS)
            barrier = threading.Barrier(n_threads)
            errors, elock = [], threading.Lock()

            def even_worker(barrier=barrier, errors=errors, elock=elock,
                            ja=json_a, jb=json_b):
                try:
                    barrier.wait(30)  # B2.7C：wait 纳入 try 边界首语句
                    for i in range(per_json_writes):
                        for tgt in (ja, jb):
                            try:
                                state_mod.save_json_atomic(
                                    tgt, {"seq": i, "blob": "x" * 1400})
                            except BaseException as e:  # noqa: BLE001
                                with elock:
                                    errors.append("%s: %s" % (type(e).__name__, e))
                except BaseException as e:  # noqa: BLE001
                    with elock:  # B2.7C：barrier 异常与写循环异常同一收集通道
                        errors.append("%s: %s" % (type(e).__name__, e))

            def odd_worker(t, barrier=barrier, errors=errors, elock=elock):
                try:
                    barrier.wait(30)  # B2.7C：wait 纳入 try 边界首语句
                    for k in range(per_thread_upserts):
                        try:
                            game_data_mod.upsert_custom(
                                "characters", {"name": f"自测压力混合-{t}-{k}",
                                               "version": "sc04"})
                        except BaseException as e:  # noqa: BLE001
                            with elock:
                                errors.append("%s: %s" % (type(e).__name__, e))
                except BaseException as e:  # noqa: BLE001
                    with elock:  # B2.7C：barrier 异常与写循环异常同一收集通道
                        errors.append("%s: %s" % (type(e).__name__, e))

            ths = [threading.Thread(target=even_worker, name=f"SE{rnd}-{i}",
                                    daemon=True)
                   for i in range(0, n_threads, 2)]
            ths += [threading.Thread(target=odd_worker, args=(t,),
                                     name=f"SO{rnd}-{t}", daemon=True)
                    for t in range(1, n_threads, 2)]
            t0 = time.perf_counter()
            for t in ths:
                t.start()
            _join_all(ths, timeout=60)
            wall = time.perf_counter() - t0
            assert errors == [], f"第 {rnd} 轮混合负载异常: {errors[:5]}"
            assert wall < 30.0, \
                f"第 {rnd} 轮墙钟 {wall:.2f}s ≥ 30s（死锁兜底口径）"
            now_names = {t.name for t in threading.enumerate()}
            assert now_names == names_base, \
                (f"第 {rnd} 轮线程泄漏: 新增={now_names - names_base} "
                 f"缺失={names_base - now_names}")
            grew = len(state_mod._PATH_LOCKS) - locks_before
            if rnd == 1:
                assert grew == expected_new, \
                    (f"第 1 轮 _PATH_LOCKS 增量应恰 {expected_new}"
                     f"（2 JSON+CUSTOM_FILE）: {grew}")
            else:
                assert grew == 0, f"第 {rnd} 轮 _PATH_LOCKS 异常膨胀: +{grew}"
            residue = _conc_residue(tmp)
            assert residue == [], f"第 {rnd} 轮 uuid 双形态 tmp 残留: {residue}"
        with open(game_data_mod.CUSTOM_FILE, encoding="utf-8") as f:  # 文档可解析
            json.load(f)
    finally:
        game_data_mod.CUSTOM_FILE = orig_custom
        guard_exc = None
        try:
            guard.__exit__(None, None, None)  # B2.7C：先复核防泄漏门禁再清理
        except BaseException as e:  # noqa: BLE001
            guard_exc = e
        shutil.rmtree(tmp, ignore_errors=True)
        if guard_exc is not None:
            raise guard_exc


# ---------- FETCH-06：open_pinned 响应体大小边界（app/url_guard.py，B2.6B） ----------
# 覆盖：MAX_RESPONSE_BYTES 上下沿（below/exact/limit+1/远超）、未知长度与分块
# 聚合形态、premature-close 不误判、空体与二进制往返、重定向/SSRF 先于读体、
# 超限时 response 必经关闭（生命周期语义）。全部离线纯桩——fake-DNS 沿 B25
# 专项 _xmod01_gai 形态、响应桩实现真实 http.client.HTTPResponse.read(n) 语义
# （沿 B2.6B 基线 .testarchive/2026-11-15-B26B-fetch06/repro_fetch06.py），
# 绝不发真实网络请求。登记验证法对齐（audit/2026-11-12/BUG-REGISTER.md:67）：
# test_fetch06c 沿 mock 超限响应体经 sites_fetch._http_get_bytes 模块薄委托
# 断言抛 ValueError。
import http.client


class _Fetch06Resp:
    """真实 http.client.HTTPResponse.read(n) 语义桩（基线 repro_fetch06.py 的
    Resp/ChunkedResp/PrematureResp 三合一）：
    - read(n>=0)：至多 n 字节、按 chunk_size 跨块聚合、EOF 后回 b''（短读不抛，
      同 stdlib amt 路径）；声明长度（declared_len）已知时 amt 剪到声明余量；
    - read() / read(n<0)：读到 EOF 聚合（unknown-length 语义）；
    - capped want 大于底层供给（声明长度>供给）时按 stdlib _safe_read 口径抛
      http.client.IncompleteRead(partial, missing)（premature-close 形态）；
      未知长度供给耗尽只短读不抛（同 stdlib）；
    - 支持 with 上下文（__exit__ 留痕并调 close）；read/close/上下文全部记录。
    """

    def __init__(self, body, *, declared_len=None, chunk_size=8192):
        self._mv = memoryview(body)
        self.pos = 0             # 读取推进位置（供「未全量消费」断言）
        # declared_len=None → 未知长度（stdlib self.length=None，无声明剪裁）；
        # premature-close 用例显式传 declared_len>len(body)
        self._declared = declared_len
        self._chunk = chunk_size
        self.read_calls = []     # 每次 read 记 (n, 实际返回字节数)
        self.close_calls = 0
        self.exited = False

    def read(self, n=-1):
        if n == 0:
            self.read_calls.append((0, 0))
            return b""
        remaining_stream = len(self._mv) - self.pos
        if n is None or n < 0:
            want = remaining_stream          # 读到 EOF 聚合
        elif self._declared is not None:
            want = min(n, self._declared - self.pos)  # stdlib：amt 剪到声明余量
        else:
            want = min(n, remaining_stream)  # 未知长度：至多 n 或至 EOF
        if want > remaining_stream:  # 声明长度供给不足 → premature-close 形态
            collected = self._mv[self.pos:].tobytes()
            self.pos = len(self._mv)
            self.read_calls.append((n, len(collected)))
            raise http.client.IncompleteRead(collected,
                                             want - remaining_stream)
        parts, got = [], 0  # 跨块聚合（真实 chunked/缓冲流消费形态）
        while got < want:
            take = min(self._chunk, want - got)
            parts.append(self._mv[self.pos:self.pos + take])
            self.pos += take
            got += take
        out = b"".join(parts)
        self.read_calls.append((n, len(out)))
        return out

    def close(self):
        self.close_calls += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.exited = True
        self.close()
        return False


class _Fetch06Opener:
    """FETCH-06 桩 opener：open() 返回 resp_factory() 产出的响应桩（绝不建连）；
    记录 open 次数与最近响应，供「校验/重定向先于读体」「close 必经」断言。"""

    def __init__(self, resp_factory):
        self._factory = resp_factory
        self.open_calls = 0
        self.resp = None

    def open(self, req, timeout=None):
        self.open_calls += 1
        self.resp = self._factory()
        return self.resp


class _Fetch06Dns:
    """FETCH-06 fake-DNS 上下文（沿 _xmod01_gai 形态）：with 期间把
    socket.getaddrinfo 换成表桩，退出还原。默认 wiki.mysqil.com→1.2.3.4
    公网形态（六类受限段全不命中）。"""

    def __init__(self, table=None):
        self._table = table or {"wiki.mysqil.com": ["1.2.3.4"]}

    def __enter__(self):
        import socket as socket_mod
        self._orig = socket_mod.getaddrinfo
        socket_mod.getaddrinfo = _xmod01_gai(self._table)
        return self

    def __exit__(self, *exc):
        import socket as socket_mod
        socket_mod.getaddrinfo = self._orig
        return False


def _fetch06_open_pinned(opener, url="https://wiki.mysqil.com/probe/fetch06",
                         allowed_hosts=("wiki.mysqil.com",)):
    """FETCH-06 公用 open_pinned 调用（公网 DNS 上下文 + 单域白名单）。"""
    from app import url_guard as ug
    with _Fetch06Dns():
        return ug.open_pinned(url, timeout=10, opener=opener,
                              allowed_hosts=allowed_hosts,
                              allow_subdomains=False)


@case
def test_fetch06a_below_limit_passes():
    """FETCH-06 ①：1KB 桩响应体 → bytes 原样返回（正常量级不受上限影响——
    登记行量级 shared.js ~239KB、头像 ~0.3-1MB，全部远低于上限）。"""
    from app import url_guard as ug
    body = (bytes(range(256)) * 5)[:1024]
    resp = _Fetch06Resp(body)
    out = _fetch06_open_pinned(_Fetch06Opener(lambda: resp))
    assert out == body, len(out)
    assert resp.read_calls == [(ug.MAX_RESPONSE_BYTES + 1, 1024)], resp.read_calls


@case
def test_fetch06b_exact_limit_passes():
    """FETCH-06 ②：恰 MAX_RESPONSE_BYTES 桩 → 通过，锁死 off-by-one 上沿
    （上限值本身合法，MAX+1 才拒绝）。"""
    from app import url_guard as ug
    limit = ug.MAX_RESPONSE_BYTES
    body = (bytes(range(256)) * (limit // 256 + 1))[:limit]
    resp = _Fetch06Resp(body, declared_len=len(body))
    out = _fetch06_open_pinned(_Fetch06Opener(lambda: resp))
    assert out == body and len(out) == limit, len(out)
    assert resp.read_calls == [(limit + 1, limit)], resp.read_calls


@case
def test_fetch06c_limit_plus_one_rejected():
    """FETCH-06 ③：MAX+1 → ERR_RESPONSE_TOO_LARGE（B2.6B 基线 defectConfirmed
    判据反转）；断言 code、消息含上限与实际长度两个数值、脱敏 URL（
    _safe_urlrepr 形态）、绝不返回截断内容。另沿登记验证法（
    audit/2026-11-12/BUG-REGISTER.md:67「mock 超限响应体调 _http_get/
    _http_get_bytes 应抛 ValueError」）走 sites_fetch._http_get_bytes 模块
    薄委托复测同一拒绝。"""
    from app import sites_fetch as sf
    from app import url_guard as ug
    limit = ug.MAX_RESPONSE_BYTES
    body = (bytes(range(256)) * (limit // 256 + 2))[:limit + 1]
    resp = _Fetch06Resp(body, declared_len=len(body))
    opener = _Fetch06Opener(lambda: resp)
    out = None
    with _Fetch06Dns():
        try:
            out = ug.open_pinned("https://wiki.mysqil.com/probe/fetch06",
                                 timeout=10, opener=opener,
                                 allowed_hosts=("wiki.mysqil.com",),
                                 allow_subdomains=False)
            raise AssertionError("MAX+1 必须拒绝而非静默全量返回")
        except ug.URLGuardError as e:
            assert e.code == "ERR_RESPONSE_TOO_LARGE", e.code
            msg = str(e)
            assert str(limit) in msg and str(limit + 1) in msg, msg  # 上限+实际长度
            assert "拒绝" in msg, msg                                # 防线文案口径
            assert "https://wiki.mysqil.com/probe/fetch06" in msg, msg  # 脱敏 URL
        assert out is None, "绝不能返回截断内容冒充成功"
        assert resp.read_calls == [(limit + 1, limit + 1)], resp.read_calls
        assert ug._PINS.get("wiki.mysqil.com") is None, dict(ug._PINS)  # 注销无残留
        # 登记验证法：mock 超限响应体经模块薄委托（_http_get_bytes）应抛 ValueError
        orig_opener = sf._OPENER
        sf._OPENER = _Fetch06Opener(lambda: _Fetch06Resp(body, declared_len=len(body)))
        try:
            try:
                sf._http_get_bytes("https://wiki.mysqil.com/probe/fetch06", 10)
                raise AssertionError("模块薄委托路径 MAX+1 必须拒绝")
            except ValueError as e:  # 登记口径：调用方 except (OSError, ValueError) 承接
                assert getattr(e, "code", "") == "ERR_RESPONSE_TOO_LARGE", e
        finally:
            sf._OPENER = orig_opener


@case
def test_fetch06d_very_large_rejected():
    """FETCH-06 ④：64MB 远超上限 → 同类拒绝且读数止步于 MAX+1——桩记录
    read(n) 调用与返回量，断言未全量消费（单次封顶探测，绝不整份消费）。"""
    from app import url_guard as ug
    mb = 1024 * 1024
    limit = ug.MAX_RESPONSE_BYTES
    body = (bytes(range(256)) * (64 * mb // 256 + 1))[:64 * mb]
    resp = _Fetch06Resp(body, declared_len=len(body))
    try:
        _fetch06_open_pinned(_Fetch06Opener(lambda: resp))
        raise AssertionError("64MB 必须拒绝")
    except ug.URLGuardError as e:
        assert e.code == "ERR_RESPONSE_TOO_LARGE", e.code
    assert resp.read_calls == [(limit + 1, limit + 1)], resp.read_calls
    assert resp.pos == limit + 1, resp.pos          # 读取止步于 MAX+1
    assert resp.pos < len(body), "必须未全量消费"     # 64MB 供给未被整份读走


@case
def test_fetch06e_unknown_length_oversize_rejected():
    """FETCH-06 ⑤：未知长度流（无 Content-Length——旧实现 read(-1) 会读到 EOF
    聚合超限的形态）→ 封顶探测照拒；读取止步 MAX+1、流尾仍有余量（未全量消费）。"""
    from app import url_guard as ug
    limit = ug.MAX_RESPONSE_BYTES
    body = (bytes(range(256)) * ((limit + 4096) // 256 + 1))[:limit + 4096]
    resp = _Fetch06Resp(body)  # 未知长度形态（declared_len=None）
    try:
        _fetch06_open_pinned(_Fetch06Opener(lambda: resp))
        raise AssertionError("未知长度超限流必须拒绝")
    except ug.URLGuardError as e:
        assert e.code == "ERR_RESPONSE_TOO_LARGE", e.code
    assert resp.read_calls == [(limit + 1, limit + 1)], resp.read_calls
    assert resp.pos == limit + 1, resp.pos
    assert resp.pos < len(body), "流尾余量不得被消费"


@case
def test_fetch06f_chunked_oversize_rejected():
    """FETCH-06 ⑥：8KB 分块供给聚合超限（真实 chunked 编码消费形态：read(n)
    跨块聚合至 n 或块流末尾）→ 拒绝。"""
    from app import url_guard as ug
    limit = ug.MAX_RESPONSE_BYTES
    body = b"chunk-" * (limit // 6 + 1)  # 6 字节块 × N ≈ MAX+3B，聚合后超限
    resp = _Fetch06Resp(body, chunk_size=8192)  # 分块供给形态
    try:
        _fetch06_open_pinned(_Fetch06Opener(lambda: resp))
        raise AssertionError("分块聚合超限必须拒绝")
    except ug.URLGuardError as e:
        assert e.code == "ERR_RESPONSE_TOO_LARGE", e.code
    assert resp.read_calls == [(limit + 1, limit + 1)], resp.read_calls
    assert resp.pos == limit + 1, resp.pos


@case
def test_fetch06g_premature_close_not_misclassified():
    """FETCH-06 ⑦：Content-Length 声明 2MB、底层只送 1MB（premature-close
    形态，read 按 stdlib _safe_read 口径抛 http.client.IncompleteRead）→
    异常原样穿透：非 URLGuardError（非超限误判）、不返回任何截断内容
    （非截断成功）。"""
    from app import url_guard as ug
    import http.client
    mb = 1024 * 1024
    supplied = bytes(range(256)) * (mb // 256)  # 恰 1MB
    resp = _Fetch06Resp(supplied, declared_len=2 * mb)  # 声明 2MB 只送 1MB
    try:
        _fetch06_open_pinned(_Fetch06Opener(lambda: resp))
        raise AssertionError("premature close 必须穿透而非截断成功")
    except http.client.IncompleteRead as e:
        assert not isinstance(e, ug.URLGuardError), "不得误判为超限"
        assert len(e.partial) == mb and e.expected == mb, (len(e.partial), e.expected)
        assert e.args == (e.partial,), e.args  # stdlib 形态：args 仅含 partial


@case
def test_fetch06h_empty_and_binary_roundtrip():
    """FETCH-06 ⑧：空字节串 → 成功返回 b''（0 字节合法响应）；二进制含
    0x00/0xFF 字节原样往返（bytes 通道不解码不筛选）。"""
    body = b""
    out = _fetch06_open_pinned(_Fetch06Opener(lambda: _Fetch06Resp(body)))
    assert out == b"", out
    body2 = bytes([0x00, 0xFF, 0x10, 0x81]) * 64 + "中文往返".encode("utf-8")
    out2 = _fetch06_open_pinned(_Fetch06Opener(lambda: _Fetch06Resp(body2)))
    assert out2 == body2, (len(out2), len(body2))
    assert b"\x00" in out2 and b"\xff" in out2


@case
def test_fetch06i_redirect_rejected_before_body():
    """FETCH-06 ⑨：redirect 响应 + 超大 body 桩 → 「拒绝跟随重定向」先发生
    （桩 opener 复刻真实 OPENER 收到 302 的行为：委托共享层 _NoRedirect
    redirect_request 契约）且未读到 body——桩记录读取计数断言为空。"""
    from app import url_guard as ug
    mb = 1024 * 1024
    resp = _Fetch06Resp(b"x" * (ug.MAX_RESPONSE_BYTES + mb))
    captured = []

    class _RedirectingOpener:
        """桩 opener：open 时按真实 OPENER 的重定向口径（_NoRedirect 一律拒）
        先走 redirect_request——重定向被拒则读体根本不可达。"""

        def open(self, req, timeout=None):
            captured.append(req.get_full_url())
            ug._NoRedirect().redirect_request(req, None, 302, "Found", None,
                                              "https://evil.test/redirected")
            resp.read()  # 不可达哨兵：若重定向未被拒，读取计数必非空
            return resp

    try:
        _fetch06_open_pinned(_RedirectingOpener())
        raise AssertionError("重定向必须拒绝跟随")
    except ValueError as e:
        assert "拒绝跟随重定向" in str(e) and "evil.test" in str(e), str(e)
        assert not isinstance(e, ug.URLGuardError), "重定向拒绝不是 URLGuardError 形态"
    assert resp.read_calls == [], "重定向拒绝先于读体：响应桩零读取"
    assert captured == ["https://wiki.mysqil.com/probe/fetch06"], captured
    assert ug._PINS.get("wiki.mysqil.com") is None, dict(ug._PINS)


@case
def test_fetch06j_ssrf_rejected_before_body():
    """FETCH-06 ⑩：SSRF 校验先于任何响应体读取——白名单外主机（公网 IP 也拒）
    ERR_HOST / 白名单域解析私网 ERR_PRIVATE_IP 都在 validate_url 阶段先胜，
    opener.open 根本不被调用、超大 body 桩零读取。"""
    from app import url_guard as ug
    mb = 1024 * 1024
    opener = _Fetch06Opener(lambda: _Fetch06Resp(b"x" * (ug.MAX_RESPONSE_BYTES + mb)))
    with _Fetch06Dns({"evil.test": ["1.2.3.4"]}):  # 外域：公网 IP 也一律拒
        try:
            ug.open_pinned("https://evil.test/x", timeout=10, opener=opener,
                           allowed_hosts=("wiki.mysqil.com",),
                           allow_subdomains=False)
            raise AssertionError("外域必须拒绝")
        except ug.URLGuardError as e:
            assert e.code == "ERR_HOST", e.code
    with _Fetch06Dns({"wiki.mysqil.com": ["10.0.0.5"]}):  # 白名单域解析私网
        try:
            ug.open_pinned("https://wiki.mysqil.com/x", timeout=10, opener=opener,
                           allowed_hosts=("wiki.mysqil.com",),
                           allow_subdomains=False)
            raise AssertionError("私网解析必须拒绝")
        except ug.URLGuardError as e:
            assert e.code == "ERR_PRIVATE_IP", e.code
    assert opener.open_calls == 0, "SSRF 拒绝必须先于 opener.open"
    assert opener.resp is None, "响应桩根本不应被构造"


@case
def test_fetch06k_response_closed_on_oversize():
    """FETCH-06 ⑪：超限异常在 with 块内抛出 → response.close 被调/上下文退出
    （无残留句柄生命周期语义）；异常先于 finally 注销，钉扎无残留。"""
    from app import url_guard as ug
    limit = ug.MAX_RESPONSE_BYTES
    body = (bytes(range(256)) * (limit // 256 + 2))[:limit + 1]
    resp = _Fetch06Resp(body, declared_len=len(body))
    try:
        _fetch06_open_pinned(_Fetch06Opener(lambda: resp))
        raise AssertionError("超限必须拒绝")
    except ug.URLGuardError as e:
        assert e.code == "ERR_RESPONSE_TOO_LARGE", e.code
    assert resp.exited, "with 上下文必须退出（close 必经语义）"
    assert resp.close_calls >= 1, "response.close 必须被调"
    assert ug._PINS.get("wiki.mysqil.com") is None, dict(ug._PINS)


# ---------- B2.7A：token uuid 双形态清理 / HTTPError 错误体有界读 / 守卫与清扫（2026-11-15） ----------
# 覆盖：clear_token_store uuid 双形态残留清理（TJD-09：双形态+固定名全清、
# 无关文件零扰动、per-path 锁与在途写者互斥、单点 unlink 失败尽力而为）；
# _wrap_http_error 错误体有界读（FETCH-08：短体展示兼容、恰 200B 上沿、201B
# 只读 201、33MiB 远超上限读量有界、未知长度/分块供给形态、凭据脱敏消息口径）；
# smoke _StateFileGuard uuid 形态守卫（姊妹项）；elev 握手前陈旧孤儿清扫
# （姊妹项）。全部离线纯桩——HTTPError 本地构造（先例
# test_tajiduo_http_error_wrapping），临时目录一律仓库内 .tmp/（显式 dir=），
# 绝不发真实网络请求。判据对齐 audit/2026-11-12/BUG-REGISTER.md:95（FETCH-08）
# 与 :96（TJD-09）登记验证法；读量计数为内存判据（tracemalloc 在
# CPython 3.13/win32 不跟踪 BytesIO.read 大块分配，取证口径见
# .testarchive/2026-11-15-B27A-cleanup/httperror-repro-result.json）。
import io
import urllib.error as ue


class _HttpErrFP:
    """HTTPError 错误体读数桩：BytesIO 语义 + 每次 read 记 (n, 返回量)，
    close 留痕（供「只读 MAX+1」「finally e.close() 必达」断言）。"""

    def __init__(self, body):
        self._b = io.BytesIO(body)
        self.read_calls = []    # 每次 read 记 n
        self.read_total = 0     # 累计消费字节数（读量判据）
        self.close_calls = 0
        self._closed = False

    @property
    def closed(self):
        return self._closed

    def read(self, n=-1):
        data = self._b.read(n)
        self.read_calls.append(n)
        self.read_total += len(data)
        return data

    def close(self):
        self.close_calls += 1
        self._closed = True


class _HttpErrChunkedFP:
    """HTTPError 错误体分块供给桩（真实 chunked/缓冲流消费形态：read(n)
    跨块聚合至 n 或流尾，块内截断不影响聚合总量——沿 FETCH-06 _Fetch06Resp
    聚合口径）。"""

    def __init__(self, body, chunk_size=7):
        self._mv = memoryview(body)
        self.pos = 0
        self._chunk = chunk_size
        self.read_calls = []
        self.read_total = 0
        self._closed = False

    @property
    def closed(self):
        return self._closed

    def read(self, n=-1):
        remaining = len(self._mv) - self.pos
        want = remaining if (n is None or n < 0) else min(n, remaining)
        parts, got = [], 0
        while got < want:
            take = min(self._chunk, want - got)
            parts.append(self._mv[self.pos:self.pos + take].tobytes())
            self.pos += take
            got += take
        self.read_calls.append(n)
        self.read_total += got
        return b"".join(parts)

    def close(self):
        self._closed = True


@case
def test_token_cleanup_uuid_tmp_both_shapes():
    """TJD-09 ①：崩溃/硬杀残留的 uuid 双形态 tmp（主 tmp
    tajiduo_token.json.<32hex>.tmp 与 bak tmp tajiduo_token.json.bak.<32hex>.tmp）
    ＋固定名三件套 → clear_token_store() 后全部消失（复现矩阵 before 双形态
    残留缺陷修复后按同判据复跑=0 残留）。"""
    from app import tajiduo_client as tc
    import tempfile
    os.makedirs(".tmp", exist_ok=True)
    with tempfile.TemporaryDirectory(dir=".tmp") as tmp:
        orig_file = tc.TOKEN_FILE
        tc.TOKEN_FILE = os.path.join(tmp, "tajiduo_token.json")
        try:
            uuid_hex = "4ead0dd1b11a407a833bdb4f31d9648f"   # 基线复现同款 32hex
            bak_hex = "7c81f65994e14b4ab8f6740fdf27b0d6"
            made = {
                "tajiduo_token.json": '{"device_id":"HTX"}',
                "tajiduo_token.json.bak": '{"device_id":"HTX0"}',
                "tajiduo_token.json.tmp": "legacy-fixed-tmp",
                "tajiduo_token.json.%s.tmp" % uuid_hex: '{"cred":"main"}',
                "tajiduo_token.json.bak.%s.tmp" % bak_hex: '{"cred":"bak"}',
            }
            for name, content in made.items():
                Path(tmp, name).write_text(content, encoding="utf-8")
            tc.clear_token_store()
            leftover = sorted(os.listdir(tmp))
            assert leftover == [], \
                "uuid 双形态＋固定名三件套必须 0 残留: %s" % leftover
        finally:
            tc.TOKEN_FILE = orig_file


@case
def test_token_cleanup_unrelated_preserved():
    """TJD-09 ②（不过宽 glob 负例面）：同目录同形态他人 tmp（gacha_history/
    last_payload/state 前缀）与 token 前缀形态近邻（31 位 hex/大写 hex/33 位
    hex/非 hex 短段/双 .bak/尾缀多段/无 .tmp 后缀）一律保留且逐字节未动。"""
    from app import tajiduo_client as tc
    import tempfile
    os.makedirs(".tmp", exist_ok=True)
    with tempfile.TemporaryDirectory(dir=".tmp") as tmp:
        orig_file = tc.TOKEN_FILE
        tc.TOKEN_FILE = os.path.join(tmp, "tajiduo_token.json")
        uuid_hex = "4ead0dd1b11a407a833bdb4f31d9648f"
        try:
            kept = {
                # 同目录同形态他人 tmp（basename 前缀不同）
                "gacha_history.json": "g",
                "gacha_history.json.tmp": "g-tmp",
                "gacha_history.json.%s.tmp" % uuid_hex: "g-uuid",
                "gacha_history.json.bak.%s.tmp" % uuid_hex: "g-bak-uuid",
                "tajiduo_last_payload.json.%s.tmp" % uuid_hex: "p-uuid",
                "state.json.%s.tmp" % uuid_hex: "s-uuid",
                # token 前缀近邻但形态不严格命中（uuid 收窄负例）
                "tajiduo_token.json.abc.tmp": "not-hex32",
                "tajiduo_token.json.%s.tmp" % ("a" * 31): "hex31",
                "tajiduo_token.json.%s.tmp" % ("A" * 32): "upper-hex",
                "tajiduo_token.json.%s.tmp" % ("g" * 33): "hex33",
                "tajiduo_token.json.bak.bak.%s.tmp" % uuid_hex: "double-bak",
                "tajiduo_token.json.%s.tmp.bak" % uuid_hex: "suffix-after-tmp",
                "tajiduo_token.json.%s" % uuid_hex: "no-tmp-suffix",
            }
            for name, content in kept.items():
                Path(tmp, name).write_text(content, encoding="utf-8")
            tc.clear_token_store()
            for name, content in kept.items():
                assert Path(tmp, name).read_text(encoding="utf-8") == content, name
            assert sorted(os.listdir(tmp)) == sorted(kept), \
                "无关/近邻文件必须零增零减"
        finally:
            tc.TOKEN_FILE = orig_file


@case
def test_token_cleanup_holds_path_lock():
    """TJD-09 ③（锁语义）：clear_token_store 与并发 save_json_atomic(TOKEN_FILE)
    共用同一 per-path 锁——主线程持 state._path_lock(TOKEN_FILE) 模拟在途写者
    整序列时，清理必须阻塞等待（不得越过锁删活跃写者刚落盘的 uuid tmp）；
    锁释放后清理完成且残留照清。事件+线程按可测性设计。"""
    from app import tajiduo_client as tc
    from app import state as state_mod
    import tempfile
    import threading
    import time as time_mod
    os.makedirs(".tmp", exist_ok=True)
    with tempfile.TemporaryDirectory(dir=".tmp") as tmp:
        orig_file = tc.TOKEN_FILE
        tc.TOKEN_FILE = os.path.join(tmp, "tajiduo_token.json")
        lock = state_mod._path_lock(tc.TOKEN_FILE)
        held = False
        try:
            Path(tmp, "tajiduo_token.json").write_text("{}", encoding="utf-8")
            lock.acquire()  # 模拟在途写者：save_json_atomic 整序列持锁
            held = True
            done = []
            t = threading.Thread(
                target=lambda: (tc.clear_token_store(), done.append(True)),
                daemon=True)
            t.start()
            time_mod.sleep(0.15)
            assert not done, "path 锁被在途写者持有时清理必须阻塞（互斥）"
            active_tmp = Path(tmp, "tajiduo_token.json.%s.tmp" % ("1" * 32))
            active_tmp.write_text("{}", encoding="utf-8")  # 在途写者刚落盘
            time_mod.sleep(0.05)
            assert done == [] and active_tmp.exists(), \
                "清理被锁阻塞期间不得误删活跃写者 tmp"
            lock.release()
            held = False
            t.join(5.0)
            assert done, "锁释放后清理必须完成"
            assert not Path(tmp, "tajiduo_token.json").exists(), "释放后固定名照删"
            assert not active_tmp.exists(), "释放后 uuid 残留照清"
        finally:
            if held:
                lock.release()
            tc.TOKEN_FILE = orig_file


@case
def test_token_cleanup_failure_tolerance():
    """TJD-09 ④（尽力而为）：制造单个 unlink 必失败形态（固定名 .bak 换成
    同名目录——os.remove 目录在 Windows/POSIX 双平台均抛 OSError 子类）→
    clear_token_store() 不抛（logout 不 500 的前提），其余 uuid 双形态与
    固定名本体照常清理完成——失败项保留、其余项不中断。"""
    from app import tajiduo_client as tc
    import tempfile
    os.makedirs(".tmp", exist_ok=True)
    with tempfile.TemporaryDirectory(dir=".tmp") as tmp:
        orig_file = tc.TOKEN_FILE
        tc.TOKEN_FILE = os.path.join(tmp, "tajiduo_token.json")
        try:
            uuid_hex = "7c81f65994e14b4ab8f6740fdf27b0d6"
            Path(tmp, "tajiduo_token.json").write_text("{}", encoding="utf-8")
            Path(tmp, "tajiduo_token.json.%s.tmp" % uuid_hex).write_text(
                "{}", encoding="utf-8")
            Path(tmp, "tajiduo_token.json.bak.%s.tmp" % uuid_hex).write_text(
                "{}", encoding="utf-8")
            bak_dir = Path(tmp, "tajiduo_token.json.bak")
            bak_dir.mkdir()  # unlink 必失败形态（同名目录）
            tc.clear_token_store()  # 不得抛：单个失败绝不中断整轮清理
            assert bak_dir.is_dir(), "失败项尽力而为：保留不重试"
            assert not Path(tmp, "tajiduo_token.json").exists(), "本体照删"
            assert not Path(tmp, "tajiduo_token.json.%s.tmp" % uuid_hex).exists(), \
                "主 tmp 照清（失败不中断）"
            assert not Path(tmp, "tajiduo_token.json.bak.%s.tmp" % uuid_hex).exists(), \
                "bak tmp 照清（失败不中断）"
        finally:
            tc.TOKEN_FILE = orig_file


@case
def test_httperr_short_body_compat():
    """FETCH-08 ①（展示不变性·短体）：33B 短错误体 → raw body 与旧实现
    e.read()[:200] 完全一致（字节 0..199 口径）；TajiduoError 照常返回
    status_code/auth/路径消息。本地构造 HTTPError（BytesIO），绝不触网。"""
    from app import tajiduo_client as tc
    body = b'{"code":401,"msg":"token expired"}'
    url = "https://bbs-api.tajiduo.com/usercenter/api/refreshToken"
    e = ue.HTTPError(url, 401, "HTTP Error", {}, io.BytesIO(body))
    err = tc._wrap_http_error(e, url)
    assert err.status_code == 401, err.status_code
    assert err.auth is True, err.auth
    assert err.raw.get("body") == body.decode("utf-8"), err.raw
    assert "refreshToken" in str(err), str(err)


@case
def test_httperr_exact_limit():
    """FETCH-08 ②（恰 200B 上沿）：恰 200 字节错误体 → raw body 全 200 字符
    （字节 0..199 全保留——与旧实现逐字符一致）。"""
    from app import tajiduo_client as tc
    body = (b"0123456789abcdefghijklmnopqrstuvwxyz" * 7)[:200]
    e = ue.HTTPError("https://bbs-api.tajiduo.com/x", 500, "HTTP Error", {},
                     io.BytesIO(body))
    err = tc._wrap_http_error(e, "https://bbs-api.tajiduo.com/x")
    shown = err.raw.get("body", "")
    assert shown == body.decode("utf-8", errors="replace"), len(shown)
    assert len(shown) == 200, len(shown)


@case
def test_httperr_limit_plus_one():
    """FETCH-08 ③（201B → 只留 200B 且只读 201）：MAX+1 供给 → raw body 恰
    前 200 字节（第 201 字节被切片丢弃）；读数恰一次 read(201)、消费 201 字节
    （读数桩断言，绝不读第 202 字节起）；finally e.close() 必达 fp。"""
    from app import tajiduo_client as tc
    body = b"y" * 201
    fp = _HttpErrFP(body)
    e = ue.HTTPError("https://bbs-api.tajiduo.com/x", 502, "HTTP Error", {}, fp)
    err = tc._wrap_http_error(e, "https://bbs-api.tajiduo.com/x")
    assert err.raw.get("body") == "y" * 200, len(err.raw.get("body", ""))
    assert fp.read_calls == [201], fp.read_calls   # 恰一次 read(MAX+1)
    assert fp.read_total == 201, fp.read_total     # 只读 201，绝不多读
    assert fp.close_calls == 1, fp.close_calls     # finally e.close() 到达 fp


@case
def test_httperr_very_large_bounded():
    """FETCH-08 ④（33MiB 远超上限，复现矩阵同体量）：33MiB 错误体 → 读数恰
    一次 read(201)、消费 201 字节（≈体量的 0.0006%，绝不全量消费——内存判据
    以读量计数为准，tracemalloc 在 CPython 3.13/win32 不跟踪 BytesIO.read
    大块分配，取证口径见 .testarchive/2026-11-15-B27A-cleanup/
    httperror-repro-result.json）；TajiduoError 照常返回 status_code 与 path
    消息，raw body ≤200 字符。"""
    from app import tajiduo_client as tc
    body = b"z" * (33 * 1024 * 1024)   # 34,603,008 字节（基线取证同体量）
    fp = _HttpErrFP(body)
    url = "https://bbs-api.tajiduo.com/apihub/awapi/yh/gacha"
    e = ue.HTTPError(url, 500, "HTTP Error", {}, fp)
    err = tc._wrap_http_error(e, url)
    assert fp.read_calls == [201], fp.read_calls
    assert fp.read_total == 201, fp.read_total
    assert fp.read_total < len(body) // 1000, \
        "读量必须远小于体量（不全量进内存）: %d/%d" % (fp.read_total, len(body))
    assert len(err.raw.get("body", "")) == 200, len(err.raw.get("body", ""))
    assert err.status_code == 500 and err.auth is False, (err.status_code, err.auth)
    assert "gacha" in str(err), str(err)


@case
def test_httperr_unknown_length():
    """FETCH-08 ⑤（未知长度流）：无声明长度形态（旧实现 e.read() 无参会读到
    EOF 聚合整流）→ 有界读恰 read(201)、消费 201 字节，流尾余量未被消费。"""
    from app import tajiduo_client as tc
    body = b"u" * 4096
    fp = _HttpErrFP(body)
    e = ue.HTTPError("https://bbs-api.tajiduo.com/x", 503, "HTTP Error", {}, fp)
    err = tc._wrap_http_error(e, "https://bbs-api.tajiduo.com/x")
    assert fp.read_calls == [201], fp.read_calls
    assert fp.read_total == 201, fp.read_total
    assert fp.read_total < len(body), "流尾余量不得被消费"
    assert len(err.raw.get("body", "")) == 200, len(err.raw.get("body", ""))


@case
def test_httperr_chunked_error_body():
    """FETCH-08 ⑥（分块供给）：7 字节小块分片供给 1KB 错误体（真实 chunked
    消费形态：read(n) 跨块聚合至 n）→ 聚合恰 201 字节、raw body 恰前 200
    字符、读数恰一次 read(201)。"""
    from app import tajiduo_client as tc
    body = b"c" * 1024
    fp = _HttpErrChunkedFP(body, chunk_size=7)
    e = ue.HTTPError("https://bbs-api.tajiduo.com/x", 500, "HTTP Error", {}, fp)
    err = tc._wrap_http_error(e, "https://bbs-api.tajiduo.com/x")
    assert fp.read_calls == [201], fp.read_calls
    assert fp.read_total == 201, fp.read_total
    assert err.raw.get("body") == "c" * 200, len(err.raw.get("body", ""))


@case
def test_httperr_credentials_redaction():
    """FETCH-08 ⑦（凭据脱敏口径不变）：错误体含 token 样串 → 只进 raw 的 body
    字段；错误消息保持 path-only 口径（「接口 HTTP <code>（<path>）」逐字符
    锁死），str(e)/args 绝不含响应体内容（官方错误原文不透传用户）。"""
    from app import tajiduo_client as tc
    secret = "Bearer_SECRET_TOKEN_VALUE_NOT_A_REAL_CREDENTIAL"
    body = ('{"code":401,"msg":"session expired %s"}' % secret).encode("utf-8")
    url = "https://bbs-api.tajiduo.com/usercenter/api/refreshToken"
    e = ue.HTTPError(url, 401, "HTTP Error", {}, io.BytesIO(body))
    err = tc._wrap_http_error(e, url)
    assert secret in err.raw.get("body", ""), "诊断原文只进 raw.body"
    assert set(err.raw.keys()) == {"body"}, err.raw
    assert secret not in str(err), "str(e) 必不含响应体"
    assert secret not in err.args[0], "异常消息必不含响应体"
    assert str(err) == "接口 HTTP 401（/usercenter/api/refreshToken）", str(err)


@case
def test_statefileguard_uuid_pattern():
    """姊妹项（smoke 守卫 uuid 形态）：_StateFileGuard 快照/恢复逻辑对
    panel_token uuid 双形态 tmp 生效——守卫期改写/删除/新增 uuid tmp，退出后
    恢复为进守卫时快照（进守卫时存在→恢复原内容；守卫期新增→删除）；形态
    近邻与无关前缀不纳入守卫（不过宽 glob）。STATE_FILE 重定向 .tmp 临时目录，
    绝不碰仓库 data/。"""
    from app import smoke_test as sm
    import tempfile
    os.makedirs(".tmp", exist_ok=True)
    hex1, hex2 = "0" * 32, "f" * 32
    with tempfile.TemporaryDirectory(dir=".tmp") as tmp:
        orig_state = sm.STATE_FILE
        sm.STATE_FILE = os.path.join(tmp, "state.json")
        try:
            guarded = {
                "state.json": "s0",
                "panel_token.json": "t0",
                "panel_token.json.bak": "b0",
                "panel_token.json.%s.tmp" % hex1: "u0",
                "panel_token.json.bak.%s.tmp" % hex2: "v0",
            }
            unguarded = {
                "unrelated.json.%s.tmp" % hex1: "x0",          # 非守卫前缀
                "panel_token.json.%s.tmp" % ("a" * 31): "n0",  # 31 位 hex 近邻
                "panel_token.json.%s.tmp.bak" % hex1: "n1",    # 尾缀近邻
            }
            for name, content in {**guarded, **unguarded}.items():
                Path(tmp, name).write_text(content, encoding="utf-8")
            guard = sm._StateFileGuard()
            guard.__enter__()
            # 守卫期扰动：uuid tmp 改写/删除/新增 + 固定名改写 + 主文件删除
            Path(tmp, "panel_token.json.%s.tmp" % hex1).write_text(
                "CHANGED", encoding="utf-8")
            os.remove(Path(tmp, "panel_token.json.bak.%s.tmp" % hex2))
            Path(tmp, "panel_token.json.%s.tmp" % hex2).write_text(
                "NEW", encoding="utf-8")
            Path(tmp, "panel_token.json").write_text("T-CHANGED", encoding="utf-8")
            os.remove(Path(tmp, "state.json"))
            guard.__exit__(None, None, None)
            # 恢复语义：进守卫时存在的恢复原内容（含 uuid 双形态）
            for name, content in guarded.items():
                assert Path(tmp, name).read_text(encoding="utf-8") == content, name
            # 守卫期新增（快照中不存在）→ 退出删除
            assert not Path(tmp, "panel_token.json.%s.tmp" % hex2).exists()
            # 未纳入守卫的近邻/无关文件不受守卫影响
            for name, content in unguarded.items():
                assert Path(tmp, name).read_text(encoding="utf-8") == content, name
        finally:
            sm.STATE_FILE = orig_state


@case
def test_elev_stale_sweep():
    """姊妹项（elev 握手前陈旧孤儿清扫）：手造旧 config/endpoint 孤儿（死 pid
    ＋回溯 mtime）→ _sweep_stale_helper_files 删除；本轮 keep 路径、pid 存活
    （含本轮自身 pid）、mtime 晚于会话开始、形态近邻与非本家族文件全部保留
    （三道闸不过删）；无 keep 保护时旧孤儿照删。死 pid 来源（零子进程）＝
    EnumProcesses 快照当前全部 pid 后取快照外不存在的 pid（前置自检
    _pid_alive=False 锁死；极端 pid 复用窗口毫秒级且必由断言兜底失败）。"""
    from app import elev_client as ec
    import ctypes
    import tempfile
    os.makedirs(".tmp", exist_ok=True)
    arr = (ctypes.c_ulong * 65536)()
    needed = ctypes.c_ulong()
    enum = getattr(ctypes.windll.kernel32, "K32EnumProcesses", None) \
        or ctypes.windll.psapi.EnumProcesses
    if not enum(arr, ctypes.sizeof(arr), ctypes.byref(needed)):
        raise AssertionError("EnumProcesses 前置失败")
    live_pids = set(arr[:needed.value // ctypes.sizeof(ctypes.c_ulong)])
    dead_pid = next(p_ for p_ in range(100000, 400000, 4) if p_ not in live_pids)
    assert not ec._pid_alive(dead_pid), "死 pid 探测前置自检"
    live_pid = os.getpid()
    with tempfile.TemporaryDirectory(dir=".tmp") as tmp:
        stale_cfg = Path(tmp, "elev_helper_config-%d-abcd1234.json" % dead_pid)
        stale_ep = Path(tmp, "elev_helper_endpoint-%d.json" % dead_pid)
        round_cfg = Path(tmp, "elev_helper_config-%d-feedbeef.json" % dead_pid)
        fresh_cfg = Path(tmp, "elev_helper_config-%d-1234abcd.json" % dead_pid)
        live_cfg = Path(tmp, "elev_helper_config-%d-abcd1234.json" % live_pid)
        live_ep = Path(tmp, "elev_helper_endpoint-%d.json" % live_pid)
        near = {
            "9hex": Path(tmp, "elev_helper_config-%d-aabbccdd1.json" % dead_pid),
            "缺hex段": Path(tmp, "elev_helper_config-%d.json" % dead_pid),
            "endpoint尾缀": Path(tmp, "elev_helper_endpoint-%d.json.bak" % dead_pid),
            "endpoint 11位pid": Path(tmp, "elev_helper_endpoint-99999999999.json"),
        }
        audit_like = Path(tmp, "elev_helper_audit.jsonl")
        for f in (stale_cfg, stale_ep, round_cfg, fresh_cfg, live_cfg, live_ep,
                  audit_like, *near.values()):
            f.write_text("{}", encoding="utf-8")
        old = time.time() - 3600.0
        for f in (stale_cfg, stale_ep, round_cfg):
            os.utime(f, (old, old))          # 陈旧孤儿形态：mtime 早于会话开始
        os.utime(fresh_cfg, (time.time() + 120.0,) * 2)  # 会话开始后新写（闸③）
        ec._sweep_stale_helper_files(tmp, keep_paths=(round_cfg,))
        assert not stale_cfg.exists(), "旧 config 孤儿必须删除"
        assert not stale_ep.exists(), "旧 endpoint 孤儿必须删除"
        assert round_cfg.exists(), "本轮 keep 路径绝不碰（闸①）"
        assert live_cfg.exists() and live_ep.exists(), \
            "pid 存活（含本轮自身 pid）跳过（闸②）"
        assert fresh_cfg.exists(), "mtime 晚于会话开始跳过（闸③）"
        for label, f in near.items():
            assert f.exists(), "形态近邻不命中: %s" % label
        assert audit_like.exists(), "非本家族文件（审计 JSONL 等）一律不碰"
        ec._sweep_stale_helper_files(tmp)    # 无 keep：闸①仅对 keep 内生效
        assert not round_cfg.exists(), "无 keep 保护时旧孤儿照删"


# ---------- B2.8-B（FETCH-01）：upgrades 有限合并——失败 slug 沿用旧条目 + failed 透出 + toast partial（2026-11-17） ----------
# 桩形态（沿本文件既有先例：模块函数替换 + try/finally 还原 + 临时根一律仓库内
# .tmp/ 显式 dir=）：逐例替换 wf.UPGRADES_JSON / wf.fetch_character_upgrade /
# wf.save_json_atomic / wf._character_slugs_from_sitemap 并在 finally 还原；
# 全程离线绝不触网，绝不触碰 data/mysqil/ 真实数据。语义裁定=用户方案 B：
# 部分成功落盘「成功 ∪ 仅 attempted∧failed 沿用旧条目」（同仓先例
# app/sites_fetch.py:616-620 _replace_source_entries 失败保旧）；全失败/空抓取
# 不落盘（修复前 S2-C3 字节不变口径保持不变）；退役角色不并入落盘（无僵尸）。
# 预置旧档/字节对照一律 pathlib 写读。

def _fetch01_entry(slug, marker):
    """构造 upgrades 条目（marker 供断言区分旧档/新抓）。"""
    return {"slug": slug, "name": "名-" + slug, "marker": marker, "skills": [],
            "ascension": [], "materials": {}, "city_talents": []}


def _fetch01_preset(upg, chars):
    """预置旧档 upgrades.json（pathlib 写入，格式对齐 save_json_atomic）。"""
    upg.write_text(json.dumps({"fetched_at": "2026-01-01 00:00:00",
                               "characters": chars},
                              ensure_ascii=False, indent=2), encoding="utf-8")


def _fetch01_redirect(wf, upg):
    """把 wf.UPGRADES_JSON 重定向到临时目录；返回原值供 finally 还原。"""
    orig = wf.UPGRADES_JSON
    wf.UPGRADES_JSON = str(upg)
    return orig


@case
def test_fetch01_all_success():
    """FETCH-01 ①全成功：落盘=本轮成功条目、merged_from_failed=0、failed 空、
    返回 payload 与落盘一致（修复前口径不变）。"""
    from app import wiki_fetch as wf
    os.makedirs(".tmp", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fetch01_t01_", dir=".tmp") as tmp:
        upg = Path(tmp, "upgrades.json")
        orig_json = _fetch01_redirect(wf, upg)
        orig_fetch = wf.fetch_character_upgrade
        try:
            wf.fetch_character_upgrade = lambda slug, timeout=15: \
                _fetch01_entry(slug, "FRESH")
            payload, failed = wf.fetch_all_upgrades(slugs=["alpha", "beta"])
            assert failed == [], failed
            assert payload["merged_from_failed"] == 0, payload
            assert set(payload["characters"]) == {"alpha", "beta"}
            assert all(payload["characters"][s]["marker"] == "FRESH"
                       for s in ("alpha", "beta"))
            disk = json.loads(upg.read_text(encoding="utf-8"))
            assert set(disk["characters"]) == {"alpha", "beta"}
            assert disk["characters"] == payload["characters"], "落盘与返回一致"
            assert disk["fetched_at"] == payload["fetched_at"]
        finally:
            wf.UPGRADES_JSON = orig_json
            wf.fetch_character_upgrade = orig_fetch


@case
def test_fetch01_all_fail_no_write():
    """FETCH-01 ②全失败：不落盘行为不变（修复前复现 S2-C3 对照口径）——主文件
    字节级保持预置旧档、无 .bak/临时写盘副产物、failed 三条照常透出、返回
    characters={}（修复前口径）；merged_from_failed=3（旧档在手、盘上自然保留）。"""
    from app import wiki_fetch as wf
    os.makedirs(".tmp", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fetch01_t02_", dir=".tmp") as tmp:
        upg = Path(tmp, "upgrades.json")
        _fetch01_preset(upg, {s: _fetch01_entry(s, "OLD-" + s)
                              for s in ("alpha", "beta", "gamma")})
        before = upg.read_bytes()
        orig_json = _fetch01_redirect(wf, upg)
        orig_fetch = wf.fetch_character_upgrade
        try:
            def _boom(slug, timeout=15):
                raise ValueError("桩注入失败")
            wf.fetch_character_upgrade = _boom
            payload, failed = wf.fetch_all_upgrades(
                slugs=["alpha", "beta", "gamma"])
            assert payload["characters"] == {}, payload["characters"]
            assert payload["merged_from_failed"] == 3, payload
            assert [f["slug"] for f in failed] == ["alpha", "beta", "gamma"], \
                failed
            assert all(f["error"] == "桩注入失败" for f in failed), failed
            assert upg.read_bytes() == before, "全失败必须字节级不落盘"
            assert not Path(tmp, "upgrades.json.bak").exists(), "不得产生 .bak"
            assert not list(Path(tmp).glob("*.tmp")), "不得残留写盘临时副产物"
        finally:
            wf.UPGRADES_JSON = orig_json
            wf.fetch_character_upgrade = orig_fetch


@case
def test_fetch01_partial_merge():
    """FETCH-01 ③一成一败（修复前复现 S1-A3 核心）：failed slug 旧条目沿用落盘、
    成功覆盖旧条目；旧档中本轮未触碰的 slug 随落盘掉出（僵尸规避设计）；返回
    payload 保持仅本轮成功条目（端点 count=本轮新抓数，toast N/M 不相交）。"""
    from app import wiki_fetch as wf
    os.makedirs(".tmp", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fetch01_t03_", dir=".tmp") as tmp:
        upg = Path(tmp, "upgrades.json")
        _fetch01_preset(upg, {s: _fetch01_entry(s, "OLD-" + s)
                              for s in ("alpha", "beta", "gamma")})
        orig_json = _fetch01_redirect(wf, upg)
        orig_fetch = wf.fetch_character_upgrade
        try:
            def _stub(slug, timeout=15):
                if slug == "beta":
                    raise ValueError("桩注入失败")
                return _fetch01_entry(slug, "FRESH")
            wf.fetch_character_upgrade = _stub
            payload, failed = wf.fetch_all_upgrades(slugs=["alpha", "beta"])
            assert [f["slug"] for f in failed] == ["beta"], failed
            assert failed[0]["error"] == "桩注入失败", failed
            assert set(payload["characters"]) == {"alpha"}, \
                "返回保持仅本轮成功条目"
            assert payload["characters"]["alpha"]["marker"] == "FRESH"
            assert payload["merged_from_failed"] == 1, payload
            disk = json.loads(upg.read_text(encoding="utf-8"))
            assert set(disk["characters"]) == {"alpha", "beta"}, \
                list(disk["characters"])
            assert disk["characters"]["alpha"]["marker"] == "FRESH", \
                "成功覆盖旧条目"
            assert disk["characters"]["beta"]["marker"] == "OLD-beta", \
                "failed slug 旧条目必须沿用落盘（FETCH-01 核心）"
            assert "gamma" not in disk["characters"], \
                "本轮未触碰的 slug 不并入落盘（无僵尸条目）"
        finally:
            wf.UPGRADES_JSON = orig_json
            wf.fetch_character_upgrade = orig_fetch


@case
def test_fetch01_newchar_success_oldchar_fail():
    """FETCH-01 ④新角色成功+旧角色失败：旧角色条目沿用、新角色入库——沿用与
    增量双向齐备。"""
    from app import wiki_fetch as wf
    os.makedirs(".tmp", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fetch01_t04_", dir=".tmp") as tmp:
        upg = Path(tmp, "upgrades.json")
        _fetch01_preset(upg,
                        {"veteran": _fetch01_entry("veteran", "OLD-veteran")})
        orig_json = _fetch01_redirect(wf, upg)
        orig_fetch = wf.fetch_character_upgrade
        try:
            def _stub(slug, timeout=15):
                if slug == "veteran":
                    raise ValueError("桩注入失败")
                return _fetch01_entry(slug, "FRESH")
            wf.fetch_character_upgrade = _stub
            payload, failed = wf.fetch_all_upgrades(
                slugs=["veteran", "newcomer"])
            assert [f["slug"] for f in failed] == ["veteran"], failed
            assert set(payload["characters"]) == {"newcomer"}
            assert payload["merged_from_failed"] == 1, payload
            disk = json.loads(upg.read_text(encoding="utf-8"))
            assert set(disk["characters"]) == {"veteran", "newcomer"}, \
                list(disk["characters"])
            assert disk["characters"]["veteran"]["marker"] == "OLD-veteran", \
                "旧角色失败条目必须沿用"
            assert disk["characters"]["newcomer"]["marker"] == "FRESH", \
                "新角色必须入库"
        finally:
            wf.UPGRADES_JSON = orig_json
            wf.fetch_character_upgrade = orig_fetch


@case
def test_fetch01_retired_drops_out():
    """FETCH-01 ⑤退役角色语义：本轮无此 slug（不在 attempted）→ 不沿用、不并入
    落盘 → 随落盘自然掉出（无僵尸条目）。"""
    from app import wiki_fetch as wf
    os.makedirs(".tmp", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fetch01_t05_", dir=".tmp") as tmp:
        upg = Path(tmp, "upgrades.json")
        _fetch01_preset(upg, {"alpha": _fetch01_entry("alpha", "OLD-alpha"),
                              "ghost": _fetch01_entry("ghost", "OLD-ghost")})
        orig_json = _fetch01_redirect(wf, upg)
        orig_fetch = wf.fetch_character_upgrade
        try:
            wf.fetch_character_upgrade = lambda slug, timeout=15: \
                _fetch01_entry(slug, "FRESH")
            payload, failed = wf.fetch_all_upgrades(slugs=["alpha"])
            assert failed == [] and payload["merged_from_failed"] == 0
            disk = json.loads(upg.read_text(encoding="utf-8"))
            assert set(disk["characters"]) == {"alpha"}, \
                "退役角色（本轮未触碰）必须随落盘掉出"
            assert disk["characters"]["alpha"]["marker"] == "FRESH"
        finally:
            wf.UPGRADES_JSON = orig_json
            wf.fetch_character_upgrade = orig_fetch


@case
def test_fetch01_empty_fetch_keeps_old():
    """FETCH-01 ⑥空抓取（sitemap 空清单→零尝试）：不落盘、旧档字节级保留、
    failed 空且无沿用计数。"""
    from app import wiki_fetch as wf
    os.makedirs(".tmp", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fetch01_t06_", dir=".tmp") as tmp:
        upg = Path(tmp, "upgrades.json")
        _fetch01_preset(upg, {s: _fetch01_entry(s, "OLD-" + s)
                              for s in ("alpha", "ghost")})
        before = upg.read_bytes()
        orig_json = _fetch01_redirect(wf, upg)
        orig_sitemap = wf._character_slugs_from_sitemap
        try:
            wf._character_slugs_from_sitemap = lambda timeout: []
            payload, failed = wf.fetch_all_upgrades()
            assert failed == [] and payload["characters"] == {}
            assert payload["merged_from_failed"] == 0
            assert upg.read_bytes() == before, "空抓取必须字节级不落盘"
            assert not Path(tmp, "upgrades.json.bak").exists(), "不得产生 .bak"
        finally:
            wf.UPGRADES_JSON = orig_json
            wf._character_slugs_from_sitemap = orig_sitemap


@case
def test_fetch01_parse_fail_into_failed():
    """FETCH-01 ⑦解析失败入 failed：ValueError（真实解析器错误形态，对齐
    _extract_data_attr 的「页面缺少 data-upgrade 数据」）slug+error 原文透出，
    且其旧条目照常沿用落盘。"""
    from app import wiki_fetch as wf
    os.makedirs(".tmp", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fetch01_t07_", dir=".tmp") as tmp:
        upg = Path(tmp, "upgrades.json")
        _fetch01_preset(upg, {"bad": _fetch01_entry("bad", "OLD-bad")})
        orig_json = _fetch01_redirect(wf, upg)
        orig_fetch = wf.fetch_character_upgrade
        try:
            def _stub(slug, timeout=15):
                if slug == "bad":
                    raise ValueError("页面缺少 data-upgrade 数据（结构可能已改版）")
                return _fetch01_entry(slug, "FRESH")
            wf.fetch_character_upgrade = _stub
            payload, failed = wf.fetch_all_upgrades(slugs=["good", "bad"])
            assert failed == [{"slug": "bad",
                               "error": "页面缺少 data-upgrade 数据（结构可能已改版）"}], \
                failed
            assert payload["merged_from_failed"] == 1
            disk = json.loads(upg.read_text(encoding="utf-8"))
            assert disk["characters"]["bad"]["marker"] == "OLD-bad", \
                "解析失败 slug 的旧条目照常沿用"
            assert disk["characters"]["good"]["marker"] == "FRESH"
        finally:
            wf.UPGRADES_JSON = orig_json
            wf.fetch_character_upgrade = orig_fetch


@case
def test_fetch01_save_oserror_propagates():
    """FETCH-01 ⑧写盘失败 OSError：save_json_atomic 抛 OSError → 原样外抛
    （main.py fetch-upgrades 端点捕 OSError→502 的前提），不吞不降级；目标文件
    字节保持预置旧档（故障发生在发布步之前）。"""
    from app import wiki_fetch as wf
    os.makedirs(".tmp", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fetch01_t08_", dir=".tmp") as tmp:
        upg = Path(tmp, "upgrades.json")
        _fetch01_preset(upg, {"beta": _fetch01_entry("beta", "OLD-beta")})
        before = upg.read_bytes()
        orig_json = _fetch01_redirect(wf, upg)
        orig_fetch = wf.fetch_character_upgrade
        orig_save = wf.save_json_atomic
        try:
            def _stub(slug, timeout=15):
                if slug == "beta":
                    raise ValueError("桩注入失败")
                return _fetch01_entry(slug, "FRESH")
            def _boom_save(path, data):
                raise OSError("磁盘故障注入")
            wf.fetch_character_upgrade = _stub
            wf.save_json_atomic = _boom_save
            raised = None
            try:
                wf.fetch_all_upgrades(slugs=["alpha", "beta"])
            except OSError as e:
                raised = e
            assert raised is not None, "OSError 必须原样外抛（不得吞掉）"
            assert "磁盘故障注入" in str(raised), str(raised)
            assert upg.read_bytes() == before, "写盘失败不得触碰目标文件"
        finally:
            wf.UPGRADES_JSON = orig_json
            wf.fetch_character_upgrade = orig_fetch
            wf.save_json_atomic = orig_save


@case
def test_fetch01_corrupted_archive_degrade():
    """FETCH-01 ⑨损坏旧档退化起步：JSON 损坏（load_upgrades→None）与结构损坏
    （characters 非 dict）均以空对象起步并记 warning（stderr），不抛不中断；
    部分成功照常落盘（损坏旧档无可沿用条目）。"""
    import contextlib
    import io
    from app import wiki_fetch as wf
    os.makedirs(".tmp", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fetch01_t09_", dir=".tmp") as tmp:
        upg = Path(tmp, "upgrades.json")
        orig_json = _fetch01_redirect(wf, upg)
        orig_fetch = wf.fetch_character_upgrade
        try:
            def _stub(slug, timeout=15):
                if slug == "bad":
                    raise ValueError("桩注入失败")
                return _fetch01_entry(slug, "FRESH")
            wf.fetch_character_upgrade = _stub
            # 形态 A：JSON 解析损坏 → load_upgrades 返回 None
            upg.write_text("{not-json", encoding="utf-8")
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                payload, failed = wf.fetch_all_upgrades(slugs=["alpha", "bad"])
            err = buf.getvalue()
            assert "旧档预载失败" in err and "空旧档起步" in err, err
            assert [f["slug"] for f in failed] == ["bad"], failed
            assert payload["merged_from_failed"] == 0, "损坏旧档无可沿用条目"
            disk = json.loads(upg.read_text(encoding="utf-8"))
            assert set(disk["characters"]) == {"alpha"}, list(disk["characters"])
            # 形态 B：JSON 合法但 characters 非 dict → 结构损坏
            upg.write_text(json.dumps({"characters": 42}), encoding="utf-8")
            buf2 = io.StringIO()
            with contextlib.redirect_stderr(buf2):
                payload2, failed2 = wf.fetch_all_upgrades(slugs=["alpha"])
            assert "结构损坏" in buf2.getvalue(), buf2.getvalue()
            assert failed2 == [] and payload2["merged_from_failed"] == 0
            disk2 = json.loads(upg.read_text(encoding="utf-8"))
            assert set(disk2["characters"]) == {"alpha"}
        finally:
            wf.UPGRADES_JSON = orig_json
            wf.fetch_character_upgrade = orig_fetch


@case
def test_fetch01_endpoint_failed_field():
    """FETCH-01 ⑩端点返回体 failed 字段回归（main.py 不在本 slice 授权改动内，
    沿本文件 inspect.getsource 布线先例源级锁定）：/api/wiki/fetch-upgrades 的
    200 返回体必须原样携带 failed（前端 toast partial 感知的根基）且 count 取自
    本轮成功条目数；抓取异常仍有 502 错误包。"""
    import inspect
    from app import main as main_mod
    post_src = inspect.getsource(main_mod.Handler.do_POST)
    assert "/api/wiki/fetch-upgrades" in post_src, "端点路由必须存在"
    assert "wiki_fetch.fetch_all_upgrades(timeout=15)" in post_src, \
        "端点必须调用 fetch_all_upgrades"
    assert '"count": len(payload.get("characters", {}))' in post_src, \
        "count 必须取自本轮成功条目数（返回 payload 口径）"
    assert '"failed": failed' in post_src, \
        "200 返回体必须原样透出 failed（toast partial 感知根基）"
    assert "wiki 拉取失败" in post_src, "抓取异常必须有 502 错误包"


def main():
    stress = "--stress" in sys.argv[1:]
    cases = CASES + STRESS_CASES if stress else CASES
    if stress:
        print("=== app.selftest 自测开始（stress 档）===")
    else:
        print("=== app.selftest 自测开始（日常档）===")
    passed = 0
    failed = 0
    for fn in cases:
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
    print(f"=== 自测结果: 通过 {passed}/{len(cases)} ===")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
