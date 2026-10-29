"""执行器：单循环状态机。

开始后只有一个循环：规划 → 校验环境 → 写 ok-nte 配置 → 无界面拉起日常任务 →
监听日志 → 更新库存/历史 → 若体力额度未尽且仍有缺口则继续。
持续模式下遇阻（ok-nte 占用、游戏内体力不足、当日额度用尽或达标、拉黑后无可执行）
不结束，转入 waiting 等 busy_retry_seconds 后自动重试；停止按钮随时可打断。

运行时持久化与崩溃恢复（本阶段）：
- data/runtime.json 记录 {state, execution, last_execution}，state 取
  stopped/running/waiting/stopping（interrupted 为启动对账后的派生展示态，不落盘）。
- 一次子进程执行按 execution_id 走 planned → launched → exited → accounted 生命周期；
  accounted 以清空 execution 槽位表示（last_execution 留档）。
- 面板启动时 recover_on_startup() 只读对账：launched/exited 且未记账 → 未决执行
  （needs_reconciliation，阻塞新执行，绝不自动重跑）；pid 仍存活 → 挂起并提示。
  绝不拉起/杀掉任何进程；所有写盘走 state.save_json_atomic。
"""
import ctypes
import os
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta

from . import planner
from .oknte_adapter import LogTail, OkNte, OkNteError, RunMonitor
from .state import DATA_DIR, STATE, load_json, save_json_atomic

CREATE_NO_WINDOW = 0x08000000

RUNTIME_FILE = os.path.join(DATA_DIR, "runtime.json")
_RUNTIME_DEFAULT = {
    "version": 1, "state": "stopped",
    "execution": None, "last_execution": None, "updated_at": None,
}
_UNSET = object()  # _write_runtime 的“未指定”哨兵（区分 None=清除）


def _pid_alive(pid):
    """探测 pid 是否存活（ctypes OpenProcess，不产生任何子进程/窗口）。

    句柄可开 => 存活；ERROR_ACCESS_DENIED(5) => 进程存在但无权限，按存活（保守）；
    其余（含 ERROR_INVALID_PARAMETER，pid 不存在）=> 不存活。
    非 Windows / 调用异常 => 按存活（保守，宁可多对账不可漏对账）。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return kernel32.GetLastError() == 5
    except (OSError, AttributeError):
        return True


def _notify(title, message):
    """Windows 气泡通知（尽力而为，失败不影响主流程）。"""
    if not STATE.settings.get("notify_enabled"):
        return
    ps = (
        "[reflection.assembly]::loadwithpartialname('System.Windows.Forms');"
        "[reflection.assembly]::loadwithpartialname('System.Drawing');"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
        f"$n.ShowBalloonTip(8000,'{title}','{message}',[System.Windows.Forms.ToolTipIcon]::Info);"
        "Start-Sleep 6;$n.Dispose()"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, timeout=20, creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _reconcile_used_stamina(oknte):
    """把 ok-nte 日志扫描到的今日已耗体力落账到 STATE（执行器拥有的运行时副作用）。

    planner.make_plan 是纯读预览：扫描值只影响当次计划、不落盘；持久化在此显式归属执行器，
    供主循环每次规划前调用。返回 (是否落账, 扫描值)。
    """
    day = STATE.ensure_today()
    scanned = oknte.scan_used_stamina(day)
    if scanned is None:
        return False, None
    with STATE.lock:
        local = int(STATE.today.get("used_stamina", 0) or 0)
        if scanned > local:
            STATE.today["used_stamina"] = scanned
            STATE.save()
            return True, scanned
    return False, scanned


def _actual_gain(step, spent):
    """实际产出：按实际消耗体力折算。ok-nte 提前完成/追猎部分失败汇总
    （spent < 计划消耗）时按比例记账，绝不按计划满额多记库存。"""
    planned = int(step.get("stamina") or 0)
    if planned > 0 and 0 <= spent < planned:
        return int(round(int(step.get("gain") or 0) * spent / planned))
    return int(step.get("gain") or 0)


class Runner:
    def __init__(self):
        self.lock = threading.RLock()
        self.thread = None
        self.stop_event = threading.Event()
        self.child = None
        self.fixed_runs_left = 0  # 追猎覆盖次数的本次剩余额度（0=按缺口自动）
        self.status = {
            "state": "idle",  # idle/running/waiting/stopping/done/error/interrupted
            "mode": "daily",  # daily/hunter
            "message": "",
            "current": None,
            "plan": None,
            "started_at": None,
            "finished_at": None,
            "waiting_reason": "",
            "next_retry_at": None,
        }
        self.lines = deque(maxlen=300)
        self.pending = []  # 未决执行（needs_reconciliation）：阻塞 start，等用户手动对账
        self._runtime = dict(_RUNTIME_DEFAULT)

    # ---- runtime.json：唯一运行时持久化 ----
    def _write_runtime(self, state=_UNSET, execution=_UNSET, last_execution=_UNSET):
        """合并写入 runtime.json（原子写；self.lock 可重入）。"""
        with self.lock:
            rt = dict(self._runtime)
            rt["version"] = 1
            if state is not _UNSET:
                rt["state"] = state
            if execution is not _UNSET:
                rt["execution"] = execution
            if last_execution is not _UNSET:
                rt["last_execution"] = last_execution
            rt["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._runtime = rt
            save_json_atomic(RUNTIME_FILE, rt)

    def recover_on_startup(self):
        """面板启动时对账上次遗留的运行状态（只读检测，绝不拉起/杀掉任何进程）。

        判定规则（决策详见 docs/results/RECOVERY-Result.md）：
        - runtime 文件缺失（首次启动）→ 直接写干净的 stopped，什么都不提示；
        - runtime 损坏且无可用 .bak → interrupted + 提示人工核对（无法确知，不猜测）；
        - execution.phase=launched/exited 且 pid 仍存活 → 未决执行（阻塞新执行，提示等待结束）；
        - execution.phase=launched/exited 且 pid 已不存在 → 未决执行（结果未知，需手动对账）；
        - execution.phase=planned（未证实拉起过）或 accounted（已记账）→ 安全清除；
        - 上次 running/waiting/stopping 且无未决 → interrupted（可直接重新开始，不卡死）。
        未决记录保留在 runtime.json 的 execution 槽位：面板再次重启仍可见，直到用户处理。
        返回恢复摘要 dict（供 main 打印与测试断言）。
        """
        with self.lock:
            self.pending = []
            file_exists = os.path.isfile(RUNTIME_FILE)
            rt = load_json(RUNTIME_FILE)
            if not isinstance(rt, dict):
                rt = None
            if rt is None:
                self._runtime = dict(_RUNTIME_DEFAULT)
                self._write_runtime(state="stopped", execution=None, last_execution=None)
                if file_exists:  # 文件在但读不出（load_json 已留档 .corrupt）→ 无法确知
                    self.status.update({
                        "state": "interrupted",
                        "message": "上次运行状态文件损坏且无备份，无法确知上次执行情况；"
                                   "若上次有任务在执行，请核对 ok-nte 日志与背包库存",
                        "finished_at": datetime.now().strftime("%H:%M:%S")})
                return {"prev_state": None, "pending": [], "corrupt": file_exists}

            prev = rt.get("state")
            exec_rec = rt.get("execution")
            if not isinstance(exec_rec, dict):
                exec_rec = None
            pending, message = [], ""
            if exec_rec and exec_rec.get("phase") in ("launched", "exited"):
                alive = _pid_alive(exec_rec.get("pid"))
                item = dict(exec_rec)
                item["alive"] = alive
                step_name = (exec_rec.get("step") or {}).get("name", "?")
                if alive:
                    item["reason"] = "面板上次退出时该任务仍在运行，结果未知；请等其结束（或手动结束）后再处理"
                    message = (f"上次面板异常退出：ok-nte 任务『{step_name}』（PID {exec_rec.get('pid')}）"
                               "仍在运行，已挂起为未决执行")
                else:
                    item["reason"] = ("任务已结束但未记账，是否消耗体力/获得材料未知；"
                                      "请核对 ok-nte 日志与背包库存后处理")
                    message = (f"上次 ok-nte 任务『{step_name}』结束但未记账，已挂起为未决执行，"
                               "处理前不会开始新的执行")
                pending = [item]
            elif exec_rec is None and prev in ("running", "stopping"):
                message = "上次面板在启动任务前退出（未确认拉起过 ok-nte），无需对账"
            # planned / accounted / 其他：未证实执行过或已记账 → 安全清除

            if pending or prev in ("running", "waiting", "stopping"):
                if not message:
                    message = {
                        "running": "上次面板在执行间隙退出（已启动的任务均已记账），可直接重新开始",
                        "waiting": "上次面板在等待重试中退出，可直接重新开始",
                        "stopping": "上次面板在停止过程中退出，无未决执行",
                    }.get(prev, "")
                self.status.update({
                    "state": "interrupted", "message": message,
                    "finished_at": datetime.now().strftime("%H:%M:%S")})
            self.pending = pending
            self._runtime = rt
            # 未决保留在文件里（下次启动仍能看到）；无未决则清干净
            self._write_runtime(state="stopped", execution=exec_rec if pending else None)
            return {"prev_state": prev, "pending": [dict(p) for p in pending], "corrupt": False}

    def resolve_pending(self, execution_id, resolution):
        """手动对账一笔未决执行。绝不替用户猜数字：
        inventory_done=用户已按日志/背包核对并校准库存；discard=放弃记账（不动库存与额度）。
        两种处理都只移除未决记录并补一条 status=reconciled 的零值历史（审计留痕，
        绝不把 expected_gain 当实际产出）。任务仍存活时拒绝处理（防止过早解除阻塞导致双刷）。
        """
        if resolution not in ("inventory_done", "discard"):
            return False, "resolution 必须是 inventory_done 或 discard"
        with self.lock:
            if self.status["state"] in ("running", "waiting", "stopping"):
                return False, "规划器运行中，不能处理未决执行"
            match = next((p for p in self.pending
                          if p.get("execution_id") == execution_id), None)
            if match is None:
                return False, f"未找到未决执行: {execution_id}"
            if _pid_alive(match.get("pid")):
                return False, (f"该任务仍在运行（PID {match.get('pid')}），"
                               "请先等它结束或手动结束再处理")
            self.pending = [p for p in self.pending
                            if p.get("execution_id") != execution_id]
            self._write_runtime(
                state="stopped",
                execution=self._runtime.get("execution") if self.pending else None)
        step = match.get("step") or {}
        note = ("已人工核对并校准库存" if resolution == "inventory_done"
                else "放弃记账（不修改库存与额度）")
        with STATE.lock:
            entry = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "mat_id": step.get("mat_id", ""), "name": step.get("name", "?"),
                "runs": 0, "stamina": 0, "gain": 0, "status": "reconciled",
                "note": f"[{match.get('day', '')} {match.get('started_at', '')} "
                        f"未决执行 {execution_id}] {note}",
            }
            STATE.today.setdefault("entries", []).append(entry)
            STATE.data["history"].append(
                {"day": match.get("day") or STATE.today.get("date"), **entry})
            STATE.data["history"] = STATE.data["history"][-500:]
            STATE.save()
        return True, "已处理未决执行：" + note

    # ---- 对外接口 ----
    def start(self, mode="daily", fixed_runs=0):
        """启动执行循环。mode: "daily"（日常体力本）/ "hunter"（异象追猎独立计划）。
        两个模式共用同一执行器，同一时刻只允许一个在跑（ok-nte 两侧互斥）。"""
        mode = mode if mode in ("daily", "hunter") else "daily"
        with self.lock:
            if self.pending:
                names = "、".join(str((p.get("step") or {}).get("name", "?"))
                                  for p in self.pending)
                return False, (f"存在未对账的历史执行（{names}），"
                               "请先在总览页『未决执行』中处理后再开始")
            if self.status["state"] in ("running", "waiting", "stopping"):
                return False, "已在运行中"
            self.stop_event.clear()
            self.lines.clear()
            self.fixed_runs_left = max(0, int(fixed_runs or 0))
            self.status.update({
                "state": "running", "mode": mode, "message": "准备中…", "current": None,
                "plan": None, "started_at": datetime.now().strftime("%H:%M:%S"),
                "finished_at": None, "waiting_reason": "", "next_retry_at": None,
            })
            self._write_runtime(state="running", execution=None)
        self.thread = threading.Thread(target=self._loop, args=(mode,), daemon=True)
        self.thread.start()
        return True, ("已开始追猎计划" if mode == "hunter" else "已开始日常计划")

    def stop(self):
        with self.lock:
            if self.status["state"] not in ("running", "waiting"):
                return False, "当前没有在运行"
            self.status["state"] = "stopping"
            self.status["message"] = "正在停止…"
            self.status["waiting_reason"] = ""
            self.status["next_retry_at"] = None
            self._write_runtime(state="stopping")
        self.stop_event.set()
        # taskkill /T 最长可阻塞 20s，曾超过前端 15s 超时令"停止"看起来失败：移入后台线程
        threading.Thread(target=self._kill_child, daemon=True, name="oknte-kill").start()
        return True, "已请求停止"

    def snapshot(self):
        with self.lock:
            snap = dict(self.status)
            snap["lines"] = list(self.lines)
            snap["pending"] = [dict(p) for p in self.pending]
            return snap

    # ---- 内部 ----
    def _log(self, line):
        stamp = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.lines.append(f"{stamp} {line}")
            self.status["message"] = line

    def _kill_child(self):
        child = self.child
        if child and child.poll() is None:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(child.pid)],
                capture_output=True, timeout=20, creationflags=CREATE_NO_WINDOW,
            )

    def _loop(self, mode="daily"):
        try:
            self._run_loop(mode)
        except OkNteError as e:
            self._finish("error", f"环境错误：{e}", preserve_execution=True)
        except Exception as e:  # 兜底：任何异常都要落到面板可见的状态里
            self._finish("error", f"内部错误：{type(e).__name__}: {e}",
                         preserve_execution=True)
        finally:
            STATE.save()

    def _run_loop(self, mode="daily"):
        oknte = OkNte(STATE.settings["oknte_root"])
        compat = oknte.compat_check()
        if compat["status"] == "incompatible":
            self._finish("error", "兼容性检查失败：" + "；".join(compat["problems"]))
            return
        for warn in compat.get("warnings", []):
            self._log(f"兼容性警告：{warn}")
        if STATE.settings.get("known_oknte_version") and \
                compat["version"] != STATE.settings["known_oknte_version"]:
            self._log(f"ok-nte 已更新 {STATE.settings['known_oknte_version']} → {compat['version']}，"
                      f"能力校验通过，继续执行")
        STATE.settings["known_oknte_version"] = compat["version"]

        if mode == "hunter":
            self._hunter_loop(oknte)
        else:
            self._daily_loop(oknte)

    def _daily_loop(self, oknte):
        settings = STATE.settings
        continuous = bool(settings.get("continuous_mode", True))
        hunter_day = None  # 追猎配置已随写配置带上的日期（跨天后当天首写需重新带上）

        while not self.stop_event.is_set():
            day = STATE.ensure_today()
            hunter = None
            if settings.get("reserve_anomaly_hunter") and hunter_day != day:
                hunter = {
                    "stamina": int(settings.get("anomaly_hunter_stamina", 180)),
                    "target": settings.get("anomaly_hunter_target", ""),
                }
            # 扫描落账归执行器：规划器纯读，日志扫描出的已耗体力先显式写回 STATE
            changed, scanned = _reconcile_used_stamina(oknte)
            if changed:
                self._log(f"已按 ok-nte 日志校准今日已耗体力 → {scanned}")
            plan = planner.make_plan(oknte, now=datetime.now())
            with self.lock:
                self.status["plan"] = plan
            if not plan["steps"]:
                if not continuous:  # 一次性模式：当日收工即结束
                    self._finish("done", plan.get("reason") or "今日无可执行步骤")
                    _notify("异环规划器", plan.get("reason") or "今日计划执行完毕")
                    return
                if not self._wait(self._blocked_reason(plan)):
                    break  # 等待中被停止
                continue
            for warn in plan["warnings"]:
                self._log(warn)

            step = plan["steps"][0]
            for attempt in (1, 2):
                if self.stop_event.is_set():
                    self._finish("done", "已手动停止")
                    return
                outcome = self._execute_step(oknte, step, hunter, attempt)
                if hunter is not None and outcome[0] != "busy" \
                        and not outcome[2].startswith("写配置失败"):
                    hunter_day = day  # 已随本次写配置带上，当天不再带；busy/写失败未写成则保留
                    hunter = None
                if outcome[0] in ("done", "short", "skipped", "busy"):
                    break
                if attempt == 1:
                    self._log(f"步骤失败（{outcome[2]}），重试一次")
            status, spent, note = outcome
            if status == "busy":
                # ok-nte 正在运行（启动时或写配置前检测到）
                if not continuous:
                    self._finish("done", "检测到 ok-nte 正在运行，请先完全退出它（托盘也要退出）")
                    return
                if not self._wait("ok-nte 正在运行，等待其完全退出后重试"):
                    break
                continue
            self._settle(step, status, spent, note)
            if status == "failed" and (note or "").startswith("启动失败"):
                # 启动失败（互斥/环境）不是游戏内失败：已按"不拉黑"落账，等待后重试
                if not continuous:
                    self._finish("done", f"启动失败：{note}")
                    return
                if not self._wait(f"启动失败，稍后自动重试（{note}）"):
                    break
                continue
            if status == "short":
                if not continuous:
                    self._finish("done", "游戏内体力不足，今日收工")
                    _notify("异环规划器", "体力不足，今日收工")
                    return
                if not self._wait("游戏内体力不足，等待体力恢复后重试"):
                    break
                continue
            if status == "skipped":
                if not continuous:
                    self._finish("done", "ok-nte 判定当日活跃度已达标，收工")
                    return
                if not self._wait("ok-nte 判定当日活跃度已达标，等待次日额度重置"):
                    break
                continue

        self._finish("done", "已手动停止")
        _notify("异环规划器", "已手动停止")

    def _wait(self, reason):
        """持续模式下遇阻等待：到点自动重试；停止按钮立即打断。返回 False 表示已被停止。"""
        seconds = max(1, int(STATE.settings.get("busy_retry_seconds", 300) or 0))
        with self.lock:
            self.status["state"] = "waiting"
            self.status["waiting_reason"] = reason
            self.status["next_retry_at"] = (
                datetime.now() + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")
            self._write_runtime(state="waiting")
        self._log(f"等待中（{reason}），{seconds} 秒后自动重试")
        self.stop_event.wait(timeout=seconds)
        with self.lock:
            self.status["waiting_reason"] = ""
            self.status["next_retry_at"] = None
            if not self.stop_event.is_set():
                self.status["state"] = "running"
                self.status["message"] = "重试中…"
                self._write_runtime(state="running")
        return not self.stop_event.is_set()

    def _blocked_reason(self, plan):
        """计划为空时的等待原因：优先提示失败拉黑耗尽，其次额度用尽/全部达标（等次日）。"""
        gaps = planner.snapshot()["gaps"]
        blocked = [planner.TARGETS.get(m, {}).get("name", m)
                   for m in STATE.today.get("blacklist", []) if gaps.get(m, 0) > 0]
        if blocked:
            return "当日失败目标已拉黑（" + "、".join(blocked) + "）且无其他可执行目标"
        return (plan.get("reason") or "今日无可执行步骤") + "，等待次日额度重置"

    def _execute_step(self, oknte, step, hunter, attempt):
        with self.lock:
            self.status["current"] = step
        if oknte.is_running():
            return "busy", 0, "检测到 ok-nte 正在运行"
        # 无界面日常只执行 enabled 的子任务：确保 daily_anomaly 已启用（最小补丁）
        # 入口刚做过 is_running 实时检测 → 跳过本函数内部的重复检测（每步 3 次→2 次）
        ok, why = oknte.ensure_routine_item("daily_anomaly", True, check_running=False)
        if not ok:
            self._log(f"提示：{why}（无界面日常可能不会执行异象界域）")
        mat = planner.TARGETS[step["mat_id"]]
        label = f"{step['name']}×{step['runs']}轮（{step['stamina']}体力）"
        self._log(f"第 {attempt} 次尝试：写入配置 → {label}")

        try:
            if attempt == 1:
                backup = oknte.backup_routine_config()
                if backup:
                    self._log(f"已备份原配置: {backup}")
            oknte.write_anomaly_config(
                mat["config"], step["stamina"], hunter=hunter,
            )
        except (OkNteError, OSError, ValueError) as e:
            if isinstance(e, OkNteError) and "正在运行" in str(e):
                return "busy", 0, "检测到 ok-nte 正在运行"
            return "failed", 0, f"写配置失败：{e}"

        cmd, cwd, env = oknte.run_command()
        day = STATE.ensure_today()
        return self._launch_and_monitor(oknte, cmd, cwd, env, day, step,
                                        step["stamina"])

    def _launch_and_monitor(self, oknte, cmd, cwd, env, day, step, planned_stamina):
        """拉起无界面子进程并监听日志至退出（日常与追猎共用的执行尾段）。

        execution_id 生命周期：planned（即将拉起，未证实）→ launched（拿到 pid）
        → exited → accounted（在 _settle/_settle_hunter 中收口）。
        """
        tail = LogTail(oknte.today_log_files(day))
        tail.read_new()  # 建立基线偏移，之后只读新增行
        monitor = RunMonitor()
        record = {
            "execution_id": datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
            "phase": "planned", "pid": None,
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "day": day,
            "step": {k: step[k] for k in ("mat_id", "name", "runs", "stamina", "gain")},
            "outcome": None,
        }
        self._write_runtime(execution=record)
        self._log(f"启动 ok-nte 无界面任务: {' '.join(cmd[1:])}")

        if self.stop_event.is_set():
            # 写配置已成功，但用户已点停止：绝不拉起新任务（真机反馈"点停止还在启动"）
            self._write_runtime(execution=None)
            return "failed", 0, "手动停止（未拉起任务）"

        try:
            self.child = subprocess.Popen(
                cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW,
            )
        except OSError as e:
            self._write_runtime(execution=None)  # 拉起失败：从未运行，撤销 planned 记录
            return "failed", 0, f"启动失败：{e}"
        record["phase"] = "launched"
        record["pid"] = self.child.pid
        self._write_runtime(execution=record)

        # 消费子进程 stdout：PIPE 无人读取会在 64KB 缓冲塞满后卡死子进程；
        # 且互斥冲突等关键诊断（"Another application instance..."）只出现在这里。
        child_tail = deque(maxlen=5)

        def _pump():
            try:
                for raw in self.child.stdout:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line:
                        child_tail.append(line[:200])
                        self._log(f"[ok-nte] {line[:200]}")
            except (OSError, ValueError):
                pass

        threading.Thread(target=_pump, daemon=True, name="oknte-stdout").start()

        deadline = time.time() + int(STATE.settings.get("max_run_minutes", 90)) * 60
        rc = None
        try:
            while True:
                if self.stop_event.is_set():
                    self._kill_child()
                    return "failed", 0, "手动停止"
                monitor.feed(tail.read_new())
                rc = self.child.poll()
                if rc is not None:
                    break
                if time.time() > deadline:
                    self._log("超出单次运行时限，强制结束子进程")
                    self._kill_child()
                    return "failed", 0, "运行超时"
                time.sleep(1)
            # 进程退出后再收一次日志，避免最后几行丢失
            for _ in range(3):
                monitor.feed(tail.read_new())
                time.sleep(1)
        finally:
            self.child = None

        status, spent, note = monitor.outcome(planned_stamina, planner.COST_PER_RUN)
        if rc not in (0, None) and status == "failed" and "未捕获" in (note or ""):
            # 子进程非正常退出且日志零任务标记：这是"启动失败"（互斥/环境问题），
            # 不是游戏内任务失败——带上真实诊断（此前被 PIPE 丢弃，用户只看到盲目拉黑）
            tail_text = " | ".join(child_tail).strip()[-180:]
            note = f"启动失败（exit {rc}）" + (f"：{tail_text}" if tail_text else "：子进程无输出")
        record["phase"] = "exited"
        record["outcome"] = {"status": status, "spent": spent, "note": note,
                             "exit_code": rc}
        self._write_runtime(execution=record)
        self._log(f"结果: {status}，实际消耗约 {spent} 体力" + (f"（{note}）" if note else ""))
        return status, spent, note

    # ---- 追猎独立循环（阶段13） ----

    def _hunter_loop(self, oknte):
        settings = STATE.settings
        continuous = bool(settings.get("continuous_mode", True))
        while not self.stop_event.is_set():
            STATE.ensure_today()
            changed, scanned = _reconcile_used_stamina(oknte)
            if changed:
                self._log(f"已按 ok-nte 日志校准今日已耗体力 → {scanned}")
            plan = planner.make_hunter_plan(oknte, now=datetime.now(),
                                            fixed_runs=self.fixed_runs_left)
            with self.lock:
                self.status["plan"] = plan
            if not plan["steps"]:
                reason = plan.get("reason") or "追猎无步骤"
                if plan.get("no_op_reason") in ("no_enabled_targets",
                                                "invalid_material_config"):
                    self._finish("error", f"追猎配置问题：{reason}")
                    return
                if not continuous:
                    self._finish("done", reason)
                    _notify("异环规划器", reason)
                    return
                if not self._wait(reason + "，到点自动重查"):
                    break
                continue
            for warn in plan["warnings"]:
                self._log(warn)

            step = plan["steps"][0]
            for attempt in (1, 2):
                if self.stop_event.is_set():
                    self._finish("done", "已手动停止")
                    return
                outcome = self._execute_hunter_step(oknte, step, attempt)
                if outcome[0] in ("done", "short", "skipped", "busy"):
                    break
                if attempt == 1:
                    self._log(f"步骤失败（{outcome[2]}），重试一次")
            status, spent, note = outcome
            if status == "busy":
                if not continuous:
                    self._finish("done", "检测到 ok-nte 正在运行，请先完全退出它（托盘也要退出）")
                    return
                if not self._wait("ok-nte 正在运行，等待其完全退出后重试"):
                    break
                continue
            self._settle_hunter(step, status, spent, note)
            if status == "failed" and (note or "").startswith("启动失败"):
                if not continuous:
                    self._finish("done", f"启动失败：{note}")
                    return
                if not self._wait(f"启动失败，稍后自动重试（{note}）"):
                    break
                continue
            if status == "done" and self.fixed_runs_left > 0:
                self.fixed_runs_left = max(0, self.fixed_runs_left - step["runs"])
                if self.fixed_runs_left == 0:
                    self._finish("done", "已达到指定的追猎次数")
                    _notify("异环规划器", "已达到指定的追猎次数")
                    return
            if status == "short":
                if not continuous:
                    self._finish("done", "游戏内体力不足，追猎收工")
                    _notify("异环规划器", "体力不足，追猎收工")
                    return
                if not self._wait("游戏内体力不足，等待体力恢复后重试"):
                    break
                continue
            if status == "failed":
                # BOSS 已当日拉黑；若额度仍在但无其他目标，计划层会给出等待原因
                if not continuous:
                    self._finish("done", f"追猎失败：{note}")
                    return
                if not self._wait(f"追猎失败（{note}），该 BOSS 今日已拉黑"):
                    break
                continue
            if status == "skipped":
                if not continuous:  # 与日常口径一致：一次性模式遇跳过即收工，不无限重查
                    self._finish("done", "ok-nte 跳过了追猎任务，收工")
                    return
                if not self._wait("ok-nte 跳过了追猎任务，稍后重查"):
                    break
                continue

        self._finish("done", "已手动停止")
        _notify("异环规划器", "已手动停止")

    def _execute_hunter_step(self, oknte, step, attempt):
        with self.lock:
            self.status["current"] = step
        if oknte.is_running():
            return "busy", 0, "检测到 ok-nte 正在运行"
        boss = step["boss"]
        self._log(f"第 {attempt} 次尝试：写入追猎配置 → {boss}×{step['runs']}局"
                  f"（{step['stamina']}体力）")
        try:
            oknte.write_hunter_config(boss, step["runs"])  # 内含备份/白名单校验/原子写
        except (OkNteError, OSError, ValueError) as e:
            if isinstance(e, OkNteError) and "正在运行" in str(e):
                return "busy", 0, "检测到 ok-nte 正在运行"
            return "failed", 0, f"写配置失败：{e}"

        cmd, cwd, env = oknte.run_command_hunter()
        day = STATE.ensure_today()
        return self._launch_and_monitor(oknte, cmd, cwd, env, day, step,
                                        step["stamina"])

    def _settle_hunter(self, step, status, spent, note):
        with STATE.lock:
            today = STATE.today
            today["hunter_spent"] = int(today.get("hunter_spent", 0) or 0) + spent
            entry = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "mat_id": step["mat_id"], "name": step["name"],
                "runs": step["runs"] if status == "done" else 0,
                "stamina": spent, "gain": step["gain"] if status == "done" else 0,
                "status": status, "note": note, "mode": "hunter",
            }
            today.setdefault("entries", []).append(entry)
            STATE.data["history"].append({"day": today["date"], **entry})
            STATE.data["history"] = STATE.data["history"][-500:]
            if status == "done":
                inv = STATE.data["inventory"]
                inv[step["mat_id"]] = int(inv.get(step["mat_id"], 0) or 0) + _actual_gain(step, spent)
            elif status == "failed" and not (note or "").startswith("启动失败"):
                marker = f"hunt:{step['boss']}"
                today.setdefault("blacklist", []).append(marker)
                today["blacklist"] = list(dict.fromkeys(today["blacklist"]))
            STATE.save()
        # accounted：记账已持久化 → 清空未决槽位（与日常共用生命周期收口）
        with self.lock:
            record = self._runtime.get("execution")
        if isinstance(record, dict) and record.get("phase") == "exited":
            last = dict(record)
            last["phase"] = "accounted"
            last["accounted_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._write_runtime(execution=None, last_execution=last)
        else:
            self._write_runtime(execution=None)
        if status == "failed":
            self._log(f"{step['boss']} 今日不再尝试（已加入当日拉黑）")

    def _settle(self, step, status, spent, note):
        with STATE.lock:
            today = STATE.today
            today["used_stamina"] = int(today.get("used_stamina", 0)) + spent
            today["planner_spent"] = int(today.get("planner_spent", 0)) + spent
            entry = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "mat_id": step["mat_id"], "name": step["name"],
                "runs": step["runs"] if status == "done" else 0,
                "stamina": spent, "gain": step["gain"] if status == "done" else 0,
                "status": status, "note": note, "mode": "daily",
            }
            today.setdefault("entries", []).append(entry)
            STATE.data["history"].append({"day": today["date"], **entry})
            STATE.data["history"] = STATE.data["history"][-500:]
            if status == "done":
                inv = STATE.data["inventory"]
                inv[step["mat_id"]] = int(inv.get(step["mat_id"], 0) or 0) + _actual_gain(step, spent)
            elif status == "failed" and not (note or "").startswith("启动失败"):
                # 拉黑只针对游戏内任务失败；启动失败（互斥/环境）进等待重试，见 _daily_loop
                today.setdefault("blacklist", []).append(step["mat_id"])
                today["blacklist"] = list(dict.fromkeys(today["blacklist"]))
            STATE.save()
        # accounted：记账已持久化 → 清空未决槽位，生命周期收口（last_execution 留档）
        with self.lock:
            record = self._runtime.get("execution")
        if isinstance(record, dict) and record.get("phase") == "exited":
            last = dict(record)
            last["phase"] = "accounted"
            last["accounted_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._write_runtime(execution=None, last_execution=last)
        else:
            self._write_runtime(execution=None)
        if status == "failed":
            self._log(f"{step['name']} 今日不再尝试（已加入当日黑名单）")

    def _finish(self, state, message, preserve_execution=False):
        """收尾：落状态与 runtime。preserve_execution=True（执行中异常崩溃）时保留
        launched/exited 的未记账槽位，下次启动对账仍能看到并处理；正常路径清空。"""
        with self.lock:
            self.status["state"] = state
            self.status["message"] = message
            self.status["finished_at"] = datetime.now().strftime("%H:%M:%S")
            self.status["current"] = None
            record = self._runtime.get("execution")
            in_flight = isinstance(record, dict) and record.get("phase") in ("launched", "exited")
            self._write_runtime(
                state="stopped", execution=record if (preserve_execution and in_flight) else None)


RUNNER = Runner()
