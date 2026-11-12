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
from pathlib import Path

from .state import DATA_DIR, save_json_atomic

CREATE_NO_WINDOW = 0x08000000  # 无控制台父进程（打包 EXE）下，缺它每次检测都弹可见黑窗

LAUNCHER_NAME = "run_planner_task.py"
LAUNCHER_SRC = (
    "# OKNTE_PLANNER_HEADLESS 规划器无界面启动器\n"
    "import os, sys\n"
    "sys.path.insert(0, os.getcwd())\n"
    "from ok import run_task\n"
    "import src.config as src_config\n"
    "cfg = dict(src_config.config)\n"
    "cfg['config_folder'] = os.environ['OKNTE_PLANNER_PROFILE']\n"
    "# 截图方式注入（键路径核对自 ok-nte working\\src\\config.py 的 windows.capture_method，\n"
    "# 注释明示支持 BitBlt/WGC/BitBlt_RenderFull/DXGI）。2026-11-10 真机事故修订：\n"
    "# 旧序列 ['DXGI','BitBlt_RenderFull'] 把 DXGI 放首位——本机（Win10 19045）ok-nte\n"
    "# 内部 DXGI 初始化 E_INVALIDARG（ok-script.log:34 update_capture_method exception\n"
    "# return None: (-2147024809,'参数错误。')），DeviceManager 直接 \"cant find a usable\n"
    "# windows capture\"（:35），LauncherTask 10s connect False 后 DailyRoutineTask 整轮\n"
    "# 跳过并每 15s 死循环；同日 16:53 GUI 手动启动用默认 ['WGC','BitBlt_RenderFull']\n"
    "# 一次成功（ok-script.log:759 use BitBlt_RenderFull capture render_full: True）。\n"
    "# 新序列与 GUI 已验证可用形态对齐：WGC 首位（ok-nte 按 Windows 版本自检，\n"
    "# 19045 < 20348 自动跳过，见 ok-script.log:758）、BitBlt_RenderFull 第二（本机\n"
    "# 实际生效的采集路径）、DXGI 末位（探测通过才放行——门控必须真实初始化 d3dshot，\n"
    "# 只 import 能过而 create 失败时 DXGI 构造仍会抛异常并被\n"
    "# ok\\device\\capture_methods\\update.py 的 except 整体吞掉返回 None，连 BitBlt\n"
    "# 兜底一起丢；放末位即使真机初始化失败也不挡 BitBlt 生效）。\n"
    "_capture_methods = ['WGC', 'BitBlt_RenderFull']\n"
    "try:\n"
    "    import d3dshot\n"
    "    _dd = d3dshot.create(capture_output='numpy')\n"
    "    _dd.stop()\n"
    "    _capture_methods.append('DXGI')\n"
    "    print(\"[capture-probe] DXGI 可用（Desktop Duplication 初始化成功），\"\n"
    "          \"capture_method=\" + str(_capture_methods))\n"
    "except Exception as _probe_err:\n"
    "    # 复审阻断⑤：探测失败必须可观测（此前完全静默——capture_method 恒为\n"
    "    # BitBlt_RenderFull 即 PrintWindow 同步截图卡死路径，运维无从知晓卡死\n"
    "    # 防护未生效）。此行经面板 runner 的 stdout 泵进「最新日志」与\n"
    "    # ok-nte-live.log（runner._launch_and_monitor._pump）\n"
    "    print(f\"[capture-probe] DXGI 不可用（d3dshot 导入/初始化失败: \"\n"
    "          f\"{_probe_err.__class__.__name__}: {_probe_err}），采集回落 \"\n"
    "          \"BitBlt_RenderFull（PrintWindow 同步截图，存在卡死风险；\"\n"
    "          \"建议在内嵌 Python 执行 -m pip install d3dshot）\")\n"
    "cfg['windows'] = dict(cfg.get('windows') or {})\n"
    "cfg['windows']['capture_method'] = _capture_methods\n"
    "run_task(cfg, task=os.environ['OKNTE_PLANNER_TASK'])\n"
)

# DXGI 采集探测片段（内嵌解释器实跑；与 LAUNCHER_SRC 的探测段同口径——门控必须
# 真实 create 而非仅 import，见 LAUNCHER_SRC 注释）。成功输出 __DXGI_PROBE_OK__
_DXGI_PROBE_SNIPPET = (
    "import d3dshot\n"
    "_dd = d3dshot.create(capture_output='numpy')\n"
    "_dd.stop()\n"
    "print('__DXGI_PROBE_OK__')\n"
)

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


# ---- 面板侧执行计划覆盖（data/oknte_plan.json；POST /api/oknte/plan 写入） ----
# 用户在面板保存的「执行计划选项覆盖」：执行链构建配置副本时消费（prepare_daily_profile
# 的①②③④），计划生成时消费（planner.make_plan 的 plan_targets）。
# ok-nte 真实 configs\ 依旧零写入——覆盖只落 data/ 与配置副本。
OKNTE_PLAN_FILE = Path(DATA_DIR) / "oknte_plan.json"


def load_oknte_plan_overrides():
    """读 data/oknte_plan.json 的 overrides 段（有效 dict 返回；否则 None）。

    无文件 → None（=现有行为，零覆盖）；损坏/结构不对 → 打印警告返回 None 不中断：
    覆盖是增强功能，读不出时回退现有行为比中断执行更安全（告警经 stdout 泵进
    面板「最新日志」）。"""
    if not OKNTE_PLAN_FILE.is_file():
        return None
    try:
        with OKNTE_PLAN_FILE.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print(f"[oknte-plan] 覆盖文件无法解析，忽略（{OKNTE_PLAN_FILE}）："
              f"{e.__class__.__name__}: {e}")
        return None
    overrides = data.get("overrides") if isinstance(data, dict) else None
    return overrides if isinstance(overrides, dict) and overrides else None


def oknte_plan_target_ids():
    """overrides.plan_targets → 去重保序的材料 id 字符串列表（无覆盖返回 []）。

    是否为可规划材料（TARGETS）由 planner.make_plan 校验并对未知 id 告警，
    本函数不做目录校验（避免 oknte_adapter 反向依赖 planner）。"""
    overrides = load_oknte_plan_overrides()
    raw = (overrides or {}).get("plan_targets")
    if not isinstance(raw, list):
        return []
    ids = [str(item).strip() for item in raw if str(item).strip()]
    return list(dict.fromkeys(ids))


def save_oknte_plan_overrides(overrides):
    """覆盖对象 → data/oknte_plan.json（pathlib + save_json_atomic；被覆盖时
    save_json_atomic 自动把上一版复制为 .bak）。返回落盘路径字符串。"""
    payload = {"schema": 1, "version": 1,
               "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "overrides": overrides}
    save_json_atomic(str(OKNTE_PLAN_FILE), payload)
    return str(OKNTE_PLAN_FILE)


def _detection_patterns(working, apps_dir):
    """进程检测的 PowerShell 片段（纯函数，selftest 可离线断言）。

    真机取证（2026-10-30）：ok-nte GUI 必然提权运行（profile admin:true），面板
    非提权时 WMI 读提权进程的 CommandLine/ExecutablePath 全为 NULL——任何命令行
    匹配都会漏（这正是首版修复后仍漏报的原因）；GUI 主窗口标题含 ok-nte，跨完整性
    级别可读，tasklist /V 是非提权场景的唯一兜底。

    匹配面（面板提权可读时；路径全部由参数推导，不硬编码盘符）：
    - ExecutablePath 以 ok-nte\\python\\pythonw.exe 结尾（GUI 独占 pythonw；
      无头子进程用 python.exe，不冲突）
    - CommandLine 含 working\\main.py（启动器拉起形态）
    - CommandLine 含 .pyappify-shortcut.py（快捷方式监督进程形态）
    - CommandLine 含 -m ok run_task（规划器自己的无头子进程，防双开）
    防误伤：不用宽泛 '*ok-nte*'（本仓库路径含 OKnte 字样）。"""
    def ps_quote(text):
        return text.replace(chr(39), chr(39) * 2)

    work = ps_quote(working)
    shortcut = ps_quote(os.path.join(apps_dir, ".pyappify-shortcut.py"))
    exe_suffix = ps_quote(os.path.join("ok-nte", "python", "pythonw.exe"))
    return (
        "$ps = Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\";"
        "$hit = @($ps | Where-Object { "
        f"$_.ExecutablePath -like '*{exe_suffix}' "
        f"-or $_.CommandLine -like '*{work}\\main.py*' "
        f"-or $_.CommandLine -like '*{shortcut}' "
        "-or $_.CommandLine -like '*-m ok run_task*' "
        "-or $_.CommandLine -like '*run_planner_task.py*' "
        "-or $_.CommandLine -like '*OKNTE_PLANNER_HEADLESS*' });"
        "if ($hit.Count -gt 0) { $hit[0].ProcessId }"
    )


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
        # 无界面运行的独立配置副本（脚本用自己的一份配置，用户原 configs\ 零写入）
        self.profile_dir = os.path.join(self.working, "configs_planner")
        self.profile_items = os.path.join(self.profile_dir, ROUTINE_ITEMS_NAME)
        self.profile_daily = os.path.join(self.profile_dir, ROUTINE_CONFIG_NAME)
        self.profile_hunter = os.path.join(self.profile_dir, HUNTER_CONFIG_NAME)
        self.profile_launcher = os.path.join(self.profile_dir, LAUNCHER_NAME)

    @staticmethod
    def _contained(path, base):
        """写路径归属校验：目标 realpath 必须落在 base 目录内。"""
        base_real = os.path.realpath(base)
        path_real = os.path.realpath(path)
        if os.path.commonpath([path_real, base_real]) != base_real:
            raise OkNteError(f"拒绝写入允许目录之外的路径：{path}")

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

    # ---- DXGI 采集可用性探测（复审阻断⑤：探测失败必须对用户可见） ----
    # 进程级缓存（root → 探测结果）：真探测要在内嵌解释器里拉起一次 Desktop
    # Duplication（秒级），每次 bootstrap 重探不可接受；装/卸 d3dshot 后需重启
    # 面板进程重探（capture 可用性在会话期内视为不变）。
    _DXGI_PROBE_CACHE = {}

    def dxgi_available(self):
        """内嵌 Python 能否真实初始化 DXGI 采集（d3dshot import + create + stop，
        与 launcher 探测段同口径）。解释器缺失/执行失败/超时一律 False。
        结果按 root 进程级缓存——面板进程生命周期内只探测一次。"""
        cached = OkNte._DXGI_PROBE_CACHE.get(self.root)
        if cached is not None:
            return cached
        ok = False
        if os.path.isfile(self.python_exe):
            try:
                proc = subprocess.run(
                    [self.python_exe, "-c", _DXGI_PROBE_SNIPPET],
                    cwd=self.working, capture_output=True, timeout=30,
                    creationflags=CREATE_NO_WINDOW)
                ok = proc.returncode == 0 and b"__DXGI_PROBE_OK__" in proc.stdout
            except (OSError, subprocess.TimeoutExpired):
                ok = False
        OkNte._DXGI_PROBE_CACHE[self.root] = ok
        return ok

    def capture_probe_warning(self):
        """DXGI 采集不可用时的面板可见警告（None = 可用/不适用）。

        复审阻断⑤第二防线：launcher 的 DXGI 探测失败此前完全静默——capture_method
        恒回落 BitBlt_RenderFull（PrintWindow 同步截图=两次卡死实证路径），卡死防护
        只剩 90s 看门狗兜底且运维无从知晓。本警告经 /api/bootstrap → 前端侧栏警示
        标签常驻展示；launcher 运行时的成败 print 是任务启动时的第二处可见信号。
        解释器缺失时返回 None（compat problems 首条已告警，不重复）。"""
        if not os.path.isfile(self.python_exe):
            return None
        if self.dxgi_available():
            return None
        return ("内嵌 Python 缺少 d3dshot（或桌面复制不可用）——DXGI 采集未生效，"
                "无头截图回落 BitBlt_RenderFull（PrintWindow 同步截图，存在卡死风险，"
                "看门狗 90s 自动重启兜底）。修复：" + self.python_exe
                + " -m pip install d3dshot，装完重启面板")

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
        """三级进程检测（按取证报告 2026-10-30 落地）：
        1. tasklist 过滤 ok-nte.exe——只覆盖启动器存活的十几秒窗口期（GUI 显示后自杀）；
        2. CIM 主模式——面板提权时可读 GUI 的 ExecutablePath/CommandLine（失效史与
           匹配面详见 _detection_patterns 的取证注释）；
        3. tasklist /V 兜底——面板非提权时 WMI 字符串属性对提权 GUI 全为 NULL，
           但 GUI 主窗口标题（ok-nte v1.4.21 ...）跨完整性级别可读。
        检测失败保守返回 True。"""
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq ok-nte.exe", "/NH"],
                capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW,
            ).stdout
            if b"ok-nte.exe" in out:
                return True
        except (OSError, subprocess.TimeoutExpired):
            return True
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 _detection_patterns(self.working, self.apps_dir)],
                capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW,
            ).stdout
            if out.strip():
                return True
        except (OSError, subprocess.TimeoutExpired):
            return True
        try:
            out = subprocess.run(
                ["tasklist", "/V", "/FI", "IMAGENAME eq pythonw.exe", "/NH"],
                capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW,
            ).stdout
            if "ok-nte" in out.decode("utf-8", errors="replace").lower():
                return True
        except (OSError, subprocess.TimeoutExpired):
            return True
        return False

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
        tmp = Path(self.routine_config + ".planner.tmp")
        tmp.write_text(json.dumps(patch["patched"], ensure_ascii=False, indent=4) + "\n",
                       encoding="utf-8")
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
        tmp = Path(self.routine_config + ".planner.tmp")
        tmp.write_text(json.dumps(content, ensure_ascii=False, indent=4) + "\n",
                       encoding="utf-8")
        os.replace(tmp, self.routine_config)
        return safe

    # ---- 无界面执行（独立配置副本：脚本用自己的一份配置副本，用户原配置零写入） ----
    # 启动器常量在模块级（LAUNCHER_NAME/LAUNCHER_SRC）。

    def prepare_daily_profile(self, target_config, stamina_target):
        """构建日常无界面运行的独立配置副本 working\\configs_planner\\：
        - configs 全量拷贝（框架配置 _ok.json/devices 等随行，窗口绑定不丢失）；
        - DailyRoutineTask.json 只启用 daily_anomaly（咖啡/领奖/影院/喷泉/家具/礼物
          等与体力计划无关的子任务不再执行）；有 data/oknte_plan.json 覆盖时按覆盖
          设置 enabled（①不再强制只开 daily_anomaly，顶层 BOT 通知/任务后退出同样
          只在覆盖给出时写入，且仅当键已存在于副本——结构变更防护）；
        - DailyRoutineTaskConfigs.json 的 daily_anomaly 段写入本步目标体力；有覆盖
          时其键再覆盖（②循环序列=用户多选列表，任务类型/奖励目标/序号/目标体力
          同理；循环模式仍强制「停用」），misc 各段若覆盖含键则副本同段 update（④）；
        - 有 overrides.hunter 时副本 AnomalyHunter.json 写 {追猎目标, 领取奖励次数}
          （③：覆盖可启用 daily_anomaly_hunter 子任务，其配置随副本生效）。
        覆盖缺失/损坏 = 现有行为（load_oknte_plan_overrides 损坏时告警返回 None）。
        用户原 configs\\ 全程零写入；返回启动器脚本路径。"""
        src_cfg = os.path.join(self.working, "configs")
        profile = os.path.join(self.working, "configs_planner")
        if not os.path.isdir(src_cfg):
            raise OkNteError(f"未找到配置目录: {src_cfg}")
        if os.path.isdir(profile):
            shutil.rmtree(profile)
        shutil.copytree(src_cfg, profile, ignore=shutil.ignore_patterns("*.tmp"))
        overrides = load_oknte_plan_overrides()
        if overrides:
            print("[oknte-plan] 应用执行计划覆盖（data/oknte_plan.json）："
                  + "、".join(sorted(overrides)))
        items_path = os.path.join(profile, ROUTINE_ITEMS_NAME)
        original = self._load_json_or(items_path, None)
        if not isinstance(original, dict) or not isinstance(original.get("Routine Items"), list):
            raise OkNteError("配置副本缺少 Routine Items（ok-nte 结构可能已变更）")
        patched = copy.deepcopy(original)
        ri_override = overrides.get("routine_items") if overrides else None
        if isinstance(ri_override, dict) and ri_override:
            # ① 按覆盖设置 enabled：只动覆盖点名的条目，未点名条目沿用副本值
            for entry in patched["Routine Items"]:
                if isinstance(entry, dict) and entry.get("id") in ri_override:
                    entry["enabled"] = bool(ri_override[entry["id"]])
            for okey, fkey in (("bot_notify", "存在失败时发送 BOT 通知"),
                               ("exit_after_task", "Exit After Task")):
                if okey in ri_override and fkey in patched:
                    patched[fkey] = bool(ri_override[okey])
        else:
            for entry in patched["Routine Items"]:
                if isinstance(entry, dict) and entry.get("id"):
                    entry["enabled"] = entry["id"] == "daily_anomaly"
        save_json_atomic(items_path, patched)
        daily_path = os.path.join(profile, ROUTINE_CONFIG_NAME)
        daily = self._load_json_or(daily_path, {})
        if not isinstance(daily, dict) or not isinstance(daily.get("daily_anomaly"), dict):
            raise OkNteError("配置副本缺少 daily_anomaly 段")
        intended = dict(target_config)
        intended["目标消耗体力"] = int(stamina_target)
        daily_override = overrides.get("daily_anomaly") if overrides else None
        if isinstance(daily_override, dict):
            # ② 覆盖键后写：循环序列=用户多选列表，任务类型/奖励目标/序号/目标体力同理
            for key, value in daily_override.items():
                if key == "循环模式":
                    continue  # 循环模式始终由规划器强制（见下），覆盖不放行
                intended[key] = value
        intended["循环模式"] = "停用"  # 切换由规划器接管
        daily["daily_anomaly"].update(intended)
        misc_override = overrides.get("misc") if overrides else None
        if isinstance(misc_override, dict):
            # ④ 杂项各段（coffee/cinema_date/…，同一 DailyRoutineTaskConfigs.json）：
            # 覆盖含键则副本同段 update；段缺失/不是对象跳过并告警（结构变更防护）
            for section, section_patch in misc_override.items():
                if not isinstance(section_patch, dict) or not section_patch:
                    continue
                if not isinstance(daily.get(section), dict):
                    print(f"[oknte-plan] 杂项段『{section}』在配置副本中缺失或不是对象，"
                          "跳过该段覆盖")
                    continue
                daily[section].update(section_patch)
        save_json_atomic(daily_path, daily)
        hunter_override = overrides.get("hunter") if overrides else None
        if isinstance(hunter_override, dict) and hunter_override:
            # ③ 副本 AnomalyHunter.json（合法键与 write_hunter_config 白名单一致）
            hunter_path = os.path.join(profile, HUNTER_CONFIG_NAME)
            hcfg = self._load_json_or(hunter_path, {})
            if not isinstance(hcfg, dict):
                hcfg = {}
            if "追猎目标" in hunter_override:
                hcfg["追猎目标"] = str(hunter_override["追猎目标"])
            if "领取奖励次数" in hunter_override:
                hcfg["领取奖励次数"] = max(0, int(hunter_override["领取奖励次数"]))
            save_json_atomic(hunter_path, hcfg)
        launcher_path = os.path.join(profile, LAUNCHER_NAME)
        Path(launcher_path).write_text(LAUNCHER_SRC, encoding="utf-8")
        return launcher_path

    def prepare_hunter_profile(self, target, reward_count):
        """构建追猎无界面运行的独立配置副本：AnomalyHunter.json 写入目标与次数。
        用户原 configs\\ 与 AnomalyHunter.json 零写入；返回启动器脚本路径。"""
        src_cfg = os.path.join(self.working, "configs")
        profile = os.path.join(self.working, "configs_planner")
        if not os.path.isdir(src_cfg):
            raise OkNteError(f"未找到配置目录: {src_cfg}")
        if os.path.isdir(profile):
            shutil.rmtree(profile)
        shutil.copytree(src_cfg, profile, ignore=shutil.ignore_patterns("*.tmp"))
        hunter_path = os.path.join(profile, HUNTER_CONFIG_NAME)
        hcfg = self._load_json_or(hunter_path, {})
        if not isinstance(hcfg, dict):
            hcfg = {}
        hcfg["追猎目标"] = str(target)
        hcfg["领取奖励次数"] = max(0, int(reward_count))
        save_json_atomic(hunter_path, hcfg)
        launcher_path = os.path.join(profile, LAUNCHER_NAME)
        Path(launcher_path).write_text(LAUNCHER_SRC, encoding="utf-8")
        return launcher_path

    def run_command(self, launcher_path, task="日常任务"):
        """编程式无界面启动：独立配置副本 + 指定任务。

        不带 -e（其语义是"任务完成后退出游戏和应用"——用户要求任务完成后游戏留存）：
        任务结束 run_task 自然返回、脚本进程自行退出，游戏进程不被动；中断/超时由
        执行器只杀脚本进程（taskkill 无 /T）。环境变量携带 profile 与任务名。"""
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env["OKNTE_PLANNER_PROFILE"] = os.path.dirname(launcher_path)
        env["OKNTE_PLANNER_TASK"] = task
        return [self.python_exe, "-u", launcher_path], self.working, env

    def run_command_hunter(self, launcher_path):
        """追猎无界面启动（独立配置副本 + AnomalyHunter 任务）。"""
        return self.run_command(launcher_path, task="AnomalyHunter")

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
        tmp = Path(path + ".planner.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=4) + "\n",
                       encoding="utf-8")
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

    def ensure_routine_item(self, item_id, enabled=True, check_running=True):
        """最小补丁 DailyRoutineTask.json 的 Routine Items 开关（如 daily_anomaly）。

        无界面跑日常时 ok-nte 只执行 enabled 的子任务，规划器必须保证目标子任务
        已启用；同互斥组的另一项交给 ok-nte 自己的 normalize 处理。条目缺失时
        不创建（结构变更防护），返回 (False, 原因)。
        check_running=False：调用方刚做过实时检测时跳过（每步 3 次检测收敛为 2 次，
        每次检测都是 tasklist+PowerShell，最坏 25s 且不可打断）。
        """
        if check_running and self.is_running():
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
    RE_IDX = re.compile(r"选择项目序号: (?:第 )?(\d+)")
    RE_UNITS = re.compile(r"双倍次数: (\d+), 单倍次数: (\d+)")
    RE_STAMINA_TARGET = re.compile(r"info_set 体力消耗目标 (\d+)")
    RE_USED = re.compile(r"ocr found used stamina (\d+)")
    RE_HUNTER_TOTAL = re.compile(r"共计消耗体力[:：]?\s*(\d+)")
    RE_FAIL_TASK = re.compile(r"任务运行失败: (.+)|任务失败: (.+)")
    FAIL_MARKS = ("未找到副本按钮", "DailyRoutineTask error", "未成功领取奖励")
    # v1.4.22 实测日志标记（真机取证：无头任务卡在战斗进入检测时，面板此前无法区分
    # "卡在战斗"只会傻等超时）——只做展示，不参与 outcome 判定
    MARK_IN_DUNGEON = "进入副本并等待"
    MARK_COMBAT_START = "开始执行战斗流程"
    MARK_COMBAT_END = "战斗结束，正在前往领取奖励"
    # 窗口可见性标记（真机取证 2026-11-08：开战 30-70s 游戏窗口失焦/最小化 → 日志反复
    # 出现 changed,visible:False → 屏幕采集死，战斗检测静默死等）。"hwnd changed" 伴随
    # 形式（changed,visible:False）已被 visible:False 判据覆盖；无 visible:False 伴随的
    # hwnd changed 是常规采集切换，不报警。只做展示（runner 透出 window_warning），
    # 不参与 outcome 判定；恢复 visible:True 可清除。
    WINDOW_MARK_INVISIBLE = "visible:False"
    WINDOW_MARK_VISIBLE = "visible:True"
    # 屏幕采集初始化失败标记（2026-11-10 真机取证）：无头拉起注入 ['DXGI',
    # 'BitBlt_RenderFull'] 时本机 DXGI 初始化 E_INVALIDARG（ok-script.log:34
    # "update_capture_method exception, return None: (-2147024809, '参数错误。')"），
    # DeviceManager 找不到任何可用采集（:35 "cant find a usable windows capture"），
    # LauncherTask 10s 刷新超时（:129 "try refresh timeout 10s, executor connect
    # False"）→ DailyRoutineTask "Game launch or capture initialization is
    # incomplete; skipping task"（:134）整轮跳过，且 ok-nte 内部每 15s 重新排队
    # LauncherTask 死循环到 90 分钟时限。两标记在 11-08/11-09 两天正常日志中
    # 零出现（grep 实证），是可靠的快速失败判据：出现即当轮任务直接失败返回
    # （杀子进程止血，不再陪 ok-nte 的 15s 内部循环空转），由 _execute_step 的
    # 失败重试（≤2 次）接管。判定置位后不复位——同一子进程内采集初始化失败
    # 不会自行恢复，恢复只能靠重启进程。
    CAPTURE_FAIL_MARK = "cant find a usable windows capture"
    CONNECT_FAIL_MARK = "executor connect False"
    # 卡死看门狗阈值：打出「开始执行战斗流程」后，超过该秒数无任何新日志行即判定
    # 执行线程挂死。PrintWindow(PW_RENDERFULLCONTENT) 同步截图阻塞时日志完全静默，
    # ok-nte 内置的 wait_until 超时因每轮先 next_frame()（TaskExecutor.py:651）而永不
    # 触发（两次卡死 0 条超时日志实锤）——只能由面板侧按"日志静默时长"判定。
    COMBAT_FREEZE_SECONDS = 90

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
        self.in_dungeon = False   # 已进入副本
        self.combat_started = False  # 已打出"开始执行战斗流程"
        self.window_invisible = False  # 游戏窗口不可见（visible:False），恢复 visible:True 清除
        self.capture_fail = ""  # 屏幕采集初始化失败的首条原文（CAPTURE_FAIL_MARK/
        # CONNECT_FAIL_MARK 命中即置位，置位后不复位；runner 据此当轮快速失败）
        self.last_log_time = None  # 最近一条新日志行的到达时刻（卡死看门狗静默基准）

    def feed(self, lines):
        if lines:
            self.last_log_time = time.time()
        for line in lines:
            # 窗口可见性独立判定（不走下方任务标记 elif 链：可见性行可能同时命中其他标记）
            if self.WINDOW_MARK_INVISIBLE in line:
                self.window_invisible = True
            elif self.WINDOW_MARK_VISIBLE in line:
                self.window_invisible = False
            # 屏幕采集初始化失败（2026-11-10 真机取证，见 CAPTURE_FAIL_MARK 注释）：
            # 命中即记首条原文（置位后不复位）；独立判定不挂可见性 elif 链，
            # runner 轮询里立即杀子进程失败返回，不再陪 ok-nte 内部 15s 循环空转
            if self.CAPTURE_FAIL_MARK in line or self.CONNECT_FAIL_MARK in line:
                if not self.capture_fail:
                    self.capture_fail = line.strip()[:160]
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
            elif self.MARK_IN_DUNGEON in line:
                self.in_dungeon = True
            elif self.MARK_COMBAT_START in line:
                self.in_dungeon = False
                self.combat_started = True
            elif self.MARK_COMBAT_END in line:
                self.combat_started = False
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

    def combat_freeze_seconds(self, now=None):
        """战斗流程日志静默时长（秒）：已打出「开始执行战斗流程」且尚未打出「战斗结束」
        （combat_started 为 True）以来，无任何新日志行的持续时间；未武装返回 None。

        判据是"任意新日志行的到达间隔"而非等待某个特定条件：执行线程阻塞在
        PrintWindow 同步截图（bitblt_utils.py:78）时整份日志静默，任何条件超时
        日志都不会出现（两次卡死 0 条超时日志实证）。"""
        if not self.combat_started or self.last_log_time is None:
            return None
        return max(0.0, (now if now is not None else time.time()) - self.last_log_time)

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
