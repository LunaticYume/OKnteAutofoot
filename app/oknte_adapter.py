"""与 ok-nte 的全部交互：配置读写（含备份/校验）、进程检测、无界面 CLI 拉起、日志解析。

不修改 ok-nte 任何源码；只读写 working/configs/DailyRoutineTaskConfigs.json（用户数据文件，
自动更新会保留），并读取 working/logs/ok-script*.log。
"""
import copy
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime

from .state import DATA_DIR

ROUTINE_CONFIG_NAME = "DailyRoutineTaskConfigs.json"
ROUTINE_ITEMS_NAME = "DailyRoutineTask.json"
HUNTER_CONFIG_NAME = "AnomalyHunter.json"
TASK_CLI_ARGS = ["-m", "ok", "run_task", "DailyRoutineTask", "-e"]
HUNTER_CLI_ARGS = ["-m", "ok", "run_task", "AnomalyHunter", "-e"]
HUNTER_COST_PER_RUN = 60  # 异象追猎每次 60 体力（ok-nte AnomalyHunter.TASK_COST）
# daily_anomaly 中规划器必须能读写的键（缺失/类型不符 = incompatible，拒绝写入）
REQUIRED_DAILY_KEYS = ("任务类型", "目标消耗体力", "循环模式")
# daily_anomaly 中规划器可能写入的全部键（写前校验允许出现的差异范围）
INTENDED_DAILY_KEYS = frozenset({
    "任务类型", "具体奖励目标", "异能材料序号", "弧盘材料序号", "空幕序号",
    "目标消耗体力", "循环模式",
})


class OkNteError(Exception):
    pass


class OkNte:
    def __init__(self, root):
        self.root = root
        self.apps_dir = os.path.join(root, "data", "apps", "ok-nte")
        self.working = os.path.join(self.apps_dir, "working")
        self.python_exe = os.path.join(self.apps_dir, "python", "python.exe")
        self.routine_config = os.path.join(self.working, "configs", ROUTINE_CONFIG_NAME)
        self.routine_items = os.path.join(self.working, "configs", ROUTINE_ITEMS_NAME)
        self.hunter_config = os.path.join(self.working, "configs", HUNTER_CONFIG_NAME)
        self.log_dir = os.path.join(self.working, "logs")

    # ---- 环境检查 ----
    def exists(self):
        return os.path.isfile(self.routine_config)

    def read_app_json(self):
        path = os.path.join(self.apps_dir, "app.json")
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def version(self):
        return self.read_app_json().get("current_version", "")

    def compat_check(self):
        """兼容性指纹：检查规划器依赖的能力（文件/键/类型/入口），不比对版本号。

        incompatible 时调用方必须拒绝修改 ok-nte 配置；warnings 需在 UI 展示原因。
        """
        problems = []   # 任一存在 => incompatible
        warnings = []   # 任一存在 => compatible_with_warning
        if not os.path.isfile(self.python_exe):
            problems.append(f"未找到内嵌 Python: {self.python_exe}")
        if not os.path.isfile(os.path.join(self.working, "main.py")):
            problems.append(f"未找到 {os.path.join(self.working, 'main.py')}（无界面入口可能已变更）")
        if not os.path.isfile(os.path.join(self.working, "ok", "__init__.py")):
            problems.append("未找到 ok 框架包，`python -m ok run_task` 可能已失效")
        if not os.path.isfile(self.routine_config):
            problems.append(f"未找到配置文件: {self.routine_config}")
        else:
            try:
                with open(self.routine_config, encoding="utf-8") as f:
                    cfg = json.load(f)
            except (OSError, ValueError) as e:
                problems.append(f"配置文件无法解析: {e}")
                cfg = None
            if isinstance(cfg, dict):
                self._probe_daily_anomaly(cfg, problems, warnings)
            elif cfg is not None:
                problems.append("配置文件顶层不是对象（ok-nte 结构可能已变更）")
        if not os.path.isdir(self.log_dir):
            warnings.append(f"日志目录不存在: {self.log_dir}（将无法解析执行结果）")
        if not os.path.isfile(self.hunter_config):
            warnings.append("AnomalyHunter.json 不存在（首次追猎执行时会自动创建）")
        status = ("incompatible" if problems
                  else "compatible_with_warning" if warnings
                  else "compatible")
        return {"status": status, "version": self.version(),
                "problems": problems, "warnings": warnings}

    @staticmethod
    def _daily_type_problems(daily):
        """daily_anomaly 必需键的存在性与类型校验（指纹与写入共用，保证同一判据）。"""
        problems = []
        for key in REQUIRED_DAILY_KEYS:
            if key not in daily:
                problems.append(f"daily_anomaly 缺少必需键『{key}』")
                continue
            value = daily[key]
            if key == "目标消耗体力":
                if not isinstance(value, int) or isinstance(value, bool):
                    problems.append(f"daily_anomaly『{key}』应为整数，实际 {type(value).__name__}")
            elif not isinstance(value, str):
                problems.append(f"daily_anomaly『{key}』应为字符串，实际 {type(value).__name__}")
        return problems

    @classmethod
    def _probe_daily_anomaly(cls, cfg, problems, warnings):
        daily = cfg.get("daily_anomaly")
        if not isinstance(daily, dict):
            problems.append("配置缺少 daily_anomaly 段（ok-nte 结构可能已变更）")
            return
        problems.extend(cls._daily_type_problems(daily))
        for key in ("异能材料序号", "弧盘材料序号", "空幕序号"):
            if key not in daily:
                warnings.append(f"daily_anomaly 缺少键『{key}』（写入目标时将新增该键）")
        hunter = cfg.get("daily_anomaly_hunter")
        if hunter is None:
            warnings.append("daily_anomaly_hunter 段缺失（启用追猎预留时将无法写入）")
        elif not isinstance(hunter, dict):
            warnings.append("daily_anomaly_hunter 段不是对象，追猎预留将无法写入")

    # 进程检测结果短缓存（类级，跨实例共享——main 每个请求都会新建 OkNte 实例）。
    # 仅供展示类调用复用；写配置前的安全检测必须走 is_running() 实时判定。
    RUN_CACHE_TTL = 5.0
    _RUN_CACHE = {}  # root -> (result: bool, monotonic 时间戳)

    def is_running(self):
        """ok-nte.exe 或其 python 主进程是否在运行。检测失败按运行中处理（保守）。
        结果写入类级短缓存，供 is_running_cached() 在 TTL 内复用（省去 2~3s 的 PowerShell 查询）。"""
        result = self._detect_running()
        OkNte._RUN_CACHE[self.root] = (result, time.monotonic())
        return result

    def is_running_cached(self, ttl=None):
        """展示用：TTL 内直接返回上次检测结果，过期才重新检测。"""
        ttl = OkNte.RUN_CACHE_TTL if ttl is None else ttl
        hit = OkNte._RUN_CACHE.get(self.root)
        if hit is not None and (time.monotonic() - hit[1]) < ttl:
            return hit[0]
        return self.is_running()

    def _detect_running(self):
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq ok-nte.exe", "/NH"],
                capture_output=True, timeout=10,
            ).stdout
            if b"ok-nte.exe" in out:
                return True
        except (OSError, subprocess.TimeoutExpired):
            return True
        # PyAppify 用 pythonw 托管 working/main.py
        ps = (
            "$ps = Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\";"
            f"$ps | Where-Object {{$_.CommandLine -like '{self.working.replace(chr(39), chr(39)*2)}*'}}"
            " | Select-Object -First 1 -ExpandProperty ProcessId"
        )
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, timeout=15,
            ).stdout
            return bool(out.strip())
        except (OSError, subprocess.TimeoutExpired):
            return True

    # ---- 配置写入 ----
    def backup_routine_config(self):
        return self._backup_json(self.routine_config, ROUTINE_CONFIG_NAME)

    def build_anomaly_patch(self, target_config, stamina_target, hunter=None):
        """构建（并校验）配置补丁：读取原配置 → deepcopy → 仅更新目标键 → 差异白名单校验。

        纯读操作：不检测进程、不落盘。真实写入（write_anomaly_config）与 Dry Run 预演
        共用本方法，两条路径产出的补丁完全一致。结构异常/校验失败抛 OkNteError，
        原文件不受影响。返回 {"patched": 补丁后完整配置, "diff": [{section,key,old,new}]}。
        """
        try:
            with open(self.routine_config, encoding="utf-8") as f:
                original = json.load(f)
        except FileNotFoundError:
            raise OkNteError(f"配置文件不存在: {self.routine_config}") from None
        except (OSError, ValueError) as e:
            raise OkNteError(f"配置文件无法解析: {e}") from None
        if not isinstance(original.get("daily_anomaly"), dict):
            raise OkNteError("daily_anomaly 段缺失，ok-nte 配置结构可能已变更")
        type_problems = self._daily_type_problems(original["daily_anomaly"])
        if type_problems:
            raise OkNteError("；".join(type_problems) + "，拒绝生成补丁")

        cfg = copy.deepcopy(original)
        intended_daily = dict(target_config)
        intended_daily["目标消耗体力"] = int(stamina_target)
        intended_daily["循环模式"] = "停用"  # 切换由规划器接管
        cfg["daily_anomaly"].update(intended_daily)

        intended_hunter = {}
        if hunter is not None:
            hunter_cfg = cfg.get("daily_anomaly_hunter")
            if not isinstance(hunter_cfg, dict):
                raise OkNteError("daily_anomaly_hunter 段缺失或不是对象，无法写入追猎预留")
            if hunter.get("stamina") is not None:
                intended_hunter["目标消耗体力"] = int(hunter["stamina"])
            if hunter.get("target"):
                intended_hunter["追猎目标"] = hunter["target"]
            hunter_cfg.update(intended_hunter)

        self._validate_patch(original, cfg, set(intended_daily), set(intended_hunter))
        return {"patched": cfg, "diff": self._patch_diff(original, cfg)}

    @staticmethod
    def _patch_diff(original, patched):
        """计划内差异清单（Dry Run 展示 before/after 用，只列差异不列全量）。"""
        diff = []
        for section in ("daily_anomaly", "daily_anomaly_hunter"):
            old = original.get(section) or {}
            new = patched.get(section) or {}
            for key in sorted(set(old) | set(new)):
                if old.get(key) != new.get(key):
                    diff.append({"section": section, "key": key,
                                 "old": old.get(key), "new": new.get(key)})
        return diff

    def write_anomaly_config(self, target_config, stamina_target, hunter=None):
        """最小补丁写入：与 Dry Run 共用 build_anomaly_patch；校验通过后原子替换。"""
        if self.is_running():
            raise OkNteError("ok-nte 正在运行，拒绝写入配置（运行中的程序会把改动覆盖回去）")
        patch = self.build_anomaly_patch(target_config, stamina_target, hunter=hunter)
        tmp = self.routine_config + ".planner.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(patch["patched"], f, ensure_ascii=False, indent=4)
            f.write("\n")
        os.replace(tmp, self.routine_config)
        return patch["diff"]

    @staticmethod
    def _validate_patch(original, patched, allowed_daily, allowed_hunter):
        """写前校验：除计划内键外的任何内容不得变化；违反即拒绝落盘。"""
        if set(original) != set(patched):
            raise OkNteError("补丁校验失败：配置顶层段发生变化")
        for section in original:
            if section in ("daily_anomaly", "daily_anomaly_hunter"):
                continue
            if original[section] != patched[section]:
                raise OkNteError(f"补丁校验失败：无关段『{section}』被修改")
        for section, allowed in (("daily_anomaly", allowed_daily),
                                 ("daily_anomaly_hunter", allowed_hunter)):
            old, new = original.get(section) or {}, patched.get(section) or {}
            changed = {k for k in set(old) | set(new) if old.get(k) != new.get(k)}
            unexpected = changed - allowed
            if unexpected:
                raise OkNteError(
                    f"补丁校验失败：段『{section}』出现计划外变更: {sorted(unexpected)}")

    def list_backups(self):
        """列出 ok-nte 日常配置的自动备份（新→旧）。"""
        backups = os.path.join(DATA_DIR, "backups")
        result = []
        if os.path.isdir(backups):
            for name in os.listdir(backups):
                if not name.startswith(ROUTINE_CONFIG_NAME + "."):
                    continue
                path = os.path.join(backups, name)
                try:
                    result.append({
                        "name": name,
                        "size": os.path.getsize(path),
                        "mtime": datetime.fromtimestamp(
                            os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S"),
                    })
                except OSError:
                    continue
        result.sort(key=lambda x: x["name"], reverse=True)
        return result

    def restore_backup(self, name):
        """用指定备份整体恢复 ok-nte 日常配置。

        刻意的全量回滚语义（区别于最小补丁写入）：恢复前先把当前配置再备份一份
        （回滚可逆）；ok-nte 运行中拒绝；备份损坏/结构不对拒绝；原子替换。
        """
        if self.is_running():
            raise OkNteError("ok-nte 正在运行，拒绝恢复配置")
        safe = os.path.basename(str(name))  # 防路径拼接逃逸
        path = os.path.join(DATA_DIR, "backups", safe)
        if not safe.startswith(ROUTINE_CONFIG_NAME + ".") or not os.path.isfile(path):
            raise OkNteError(f"备份不存在: {safe}")
        try:
            with open(path, encoding="utf-8") as f:
                content = json.load(f)
        except (OSError, ValueError) as e:
            raise OkNteError(f"备份无法解析: {e}") from None
        if not isinstance(content, dict) or not isinstance(content.get("daily_anomaly"), dict):
            raise OkNteError("备份内容不含 daily_anomaly 段，拒绝恢复")
        self.backup_routine_config()  # 恢复前留当前现状，回滚可逆
        tmp = self.routine_config + ".planner.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(content, f, ensure_ascii=False, indent=4)
            f.write("\n")
        os.replace(tmp, self.routine_config)
        return safe

    # ---- 无界面执行 ----
    def run_command(self):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        return [self.python_exe, "-u"] + TASK_CLI_ARGS, self.working, env

    def run_command_hunter(self):
        """独立追猎：`python -m ok run_task AnomalyHunter -e`（与日常任务完全独立的入口）。"""
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        return [self.python_exe, "-u"] + HUNTER_CLI_ARGS, self.working, env

    # ---- 追猎独立配置与日常开关（阶段13） ----
    def _backup_json(self, path, prefix):
        """通用 JSON 备份：文件不存在返回 None；保留最近 20 份。"""
        backups = os.path.join(DATA_DIR, "backups")
        os.makedirs(backups, exist_ok=True)
        if not os.path.isfile(path):
            return None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = os.path.join(backups, f"{prefix}.{stamp}")
        shutil.copy2(path, dst)
        keep = sorted(p for p in os.listdir(backups) if p.startswith(prefix + "."))
        for old in keep[:-20]:
            os.remove(os.path.join(backups, old))
        return dst

    @staticmethod
    def _load_json_or(path, default):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return default
        except (OSError, ValueError) as e:
            raise OkNteError(f"配置无法解析: {e}") from None

    @staticmethod
    def _atomic_write_json(path, data):
        tmp = path + ".planner.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
            f.write("\n")
        os.replace(tmp, path)

    def write_hunter_config(self, target, reward_count):
        """最小补丁写入独立追猎配置 AnomalyHunter.json（文件不存在则按默认创建）。

        合法键仅 {追猎目标, 领取奖励次数}（ok-nte 独立模式的其他键会被其
        verify_config 丢弃，故白名单与之严格一致）；其余内容不得变化。
        """
        if self.is_running():
            raise OkNteError("ok-nte 正在运行，拒绝写入配置（运行中的程序会把改动覆盖回去）")
        original = self._load_json_or(self.hunter_config, {})
        allowed = {"追猎目标": str(target), "领取奖励次数": max(0, int(reward_count))}
        patched = copy.deepcopy(original)
        patched.update(allowed)
        changed = {k for k in set(original) | set(patched)
                   if original.get(k) != patched.get(k)}
        unexpected = changed - set(allowed)
        if unexpected:
            raise OkNteError(f"补丁校验失败：出现计划外变更: {sorted(unexpected)}")
        self._backup_json(self.hunter_config, HUNTER_CONFIG_NAME)
        self._atomic_write_json(self.hunter_config, patched)
        return sorted(changed)

    def ensure_routine_item(self, item_id, enabled=True):
        """最小补丁 DailyRoutineTask.json 的 Routine Items 开关（如 daily_anomaly）。

        无界面跑日常时 ok-nte 只执行 enabled 的子任务，规划器必须保证目标子任务
        已启用；同互斥组的另一项交给 ok-nte 自己的 normalize 处理。条目缺失时
        不创建（结构变更防护），返回 (False, 原因)。
        """
        if self.is_running():
            raise OkNteError("ok-nte 正在运行，拒绝写入配置（运行中的程序会把改动覆盖回去）")
        original = self._load_json_or(self.routine_items, None)
        if original is None:
            return False, f"未找到 {ROUTINE_ITEMS_NAME}（ok-nte 结构可能已变更）"
        items = original.get("Routine Items")
        if not isinstance(items, list):
            return False, "Routine Items 段缺失（ok-nte 结构可能已变更）"
        idx = next((i for i, entry in enumerate(items)
                    if isinstance(entry, dict) and entry.get("id") == item_id), None)
        if idx is None:
            return False, f"Routine Items 中没有『{item_id}』条目"
        if items[idx].get("enabled") == bool(enabled):
            return True, "已是目标状态"
        patched = copy.deepcopy(original)
        patched["Routine Items"][idx]["enabled"] = bool(enabled)
        # 校验：条目数量与顺序不变；仅目标条目变化，且仅 enabled 字段变化
        if len(patched["Routine Items"]) != len(items):
            raise OkNteError("补丁校验失败：Routine Items 条目数量变化")
        changed = [(i, a, b) for i, (a, b) in enumerate(zip(items, patched["Routine Items"])) if a != b]
        if len(changed) != 1 or changed[0][0] != idx:
            raise OkNteError("补丁校验失败：Routine Items 出现计划外变更")
        _, old_entry, new_entry = changed[0]
        if set(old_entry) != set(new_entry) or old_entry.get("id") != item_id \
                or any(old_entry[k] != new_entry[k] for k in old_entry if k != "enabled"):
            raise OkNteError("补丁校验失败：Routine Items 条目出现 enabled 以外的变更")
        self._backup_json(self.routine_items, ROUTINE_ITEMS_NAME)
        self._atomic_write_json(self.routine_items, patched)
        return True, f"已启用『{item_id}』" if enabled else f"已停用『{item_id}』"

    # ---- 日志 ----
    def today_log_files(self, day):
        return [
            os.path.join(self.log_dir, "ok-script.log"),
            os.path.join(self.log_dir, f"ok-script.{day}.log"),
        ]

    def scan_used_stamina(self, day):
        """从今天的日志里取 OCR 读到的『今日已消耗体力』最大值（X/180 中的 X）。"""
        best = None
        pattern = re.compile(rf"^{re.escape(day)} .*?ocr found used stamina (\d+)")
        for path in self.today_log_files(day):
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        m = pattern.match(line)
                        if m:
                            best = max(best or 0, int(m.group(1)))
            except OSError:
                continue
        return best


class LogTail:
    """跨文件增量读取日志新行（处理当日轮转）。"""

    def __init__(self, paths):
        self.offsets = {}
        self.paths = paths

    def _size(self, path):
        try:
            return os.path.getsize(path)
        except OSError:
            return None

    def read_new(self):
        lines = []
        for path in self.paths:
            size = self._size(path)
            if size is None:
                self.offsets.pop(path, None)
                continue
            offset = self.offsets.get(path)
            if offset is None:
                self.offsets[path] = size
                continue
            if size < offset:  # 文件被轮转/清空
                offset = 0
            if size > offset:
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        f.seek(offset)
                        text = f.read(size - offset)
                except OSError:
                    continue
                self.offsets[path] = size
                lines.extend(text.splitlines())
        return lines


class RunMonitor:
    """汇总一次 run_task 执行期间的日志事件。"""

    RE_STARTED = re.compile(r"开始任务: (.+)")
    RE_IDX = re.compile(r"选择项目序号: 第 (\d+) 个项目")
    RE_UNITS = re.compile(r"双倍次数: (\d+), 单倍次数: (\d+)")
    RE_STAMINA_TARGET = re.compile(r"info_set 体力消耗目标 (\d+)")
    RE_USED = re.compile(r"ocr found used stamina (\d+)")
    RE_HUNTER_TOTAL = re.compile(r"共计消耗体力[:：]?\s*(\d+)")
    RE_FAIL_TASK = re.compile(r"任务运行失败: (.+)|任务失败: (.+)")
    FAIL_MARKS = ("未找到副本按钮", "DailyRoutineTask error", "未成功领取奖励")

    def __init__(self):
        self.started = []
        self.selected_idx = None
        self.units = None
        self.stamina_target = None
        self.used_stamina = None
        self.hunter_total = None
        self.completed = False
        self.skipped = False
        self.stamina_short = False
        self.failures = []

    def feed(self, lines):
        for line in lines:
            if (m := self.RE_STARTED.search(line)):
                self.started.append(m.group(1).strip())
            elif (m := self.RE_IDX.search(line)):
                self.selected_idx = int(m.group(1))
            elif (m := self.RE_UNITS.search(line)):
                double, single = int(m.group(1)), int(m.group(2))
                # ok-nte 语义（AnomalyTask）：双倍领取一次消耗 2 个 40 体力单位，
                # 单倍消耗 1 个；总单位 = 2×双倍 + 单倍，而非两者简单相加。
                self.units = double * 2 + single
            elif (m := self.RE_STAMINA_TARGET.search(line)):
                self.stamina_target = int(m.group(1))
            elif (m := self.RE_USED.search(line)):
                self.used_stamina = int(m.group(1))
            elif (m := self.RE_HUNTER_TOTAL.search(line)):
                # 追猎结束汇总（AnomalyHunter）：成功/失败后输出"共计消耗体力: N"
                self.hunter_total = int(m.group(1))
            elif "任务执行完毕" in line:
                self.completed = True
            elif "跳过每日体力任务" in line:
                self.skipped = True
            elif "体力不足" in line:
                self.stamina_short = True
            elif (m := self.RE_FAIL_TASK.search(line)):
                self.failures.append(":".join(g for g in m.groups() if g))
            elif any(mark in line for mark in self.FAIL_MARKS):
                self.failures.append(line.split(":", 1)[-1].strip()[:80])

    def outcome(self, planned_stamina, cost_per_run):
        """返回 (状态, 实际消耗体力, 原因说明)。状态: done/short/skipped/failed"""
        if self.completed and not self.failures:
            units = self.units if self.units is not None else planned_stamina // cost_per_run
            return "done", units * cost_per_run, ""
        if self.hunter_total is not None:
            # 追猎：以 BOSS 战汇总的"共计消耗体力"为准（可能因连续失败不足计划值）
            return "done", self.hunter_total, ""
        if self.stamina_short:
            return "short", 0, "体力不足"
        if self.skipped:
            return "skipped", 0, "当日活跃度/体力目标已达标，ok-nte 跳过"
        if self.failures:
            return "failed", 0, self.failures[0]
        return "failed", 0, "未捕获到任务完成标记"


def utcnow_ms():
    return int(time.time() * 1000)
