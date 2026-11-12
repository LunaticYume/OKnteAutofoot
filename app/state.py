"""规划器状态持久化：设置、角色、库存、当日进度、历史。"""
import errno
import json
import os
import shutil
import sys
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

# ---- 数据目录派生（全仓库唯一派生点；STATE 在模块尾导入期实例化，首启迁移必须在此之前完成）----
_FROZEN = getattr(sys, "frozen", False)  # PyInstaller 等打包环境
# 打包态声明：frozen，或启动器注入 OKNTE_PLANNER_USER_DATA=1——启动器档经
# python -m app.main 拉起、永远非 frozen，由 LAUNCHER_CS Run() 注入该变量声明；
# 变量名与 state.py 读取名是两处无编译期检查的字符串，接线由 build_exe.
# _launch_and_probe 的 .migrated 断言在每次构建真机拉起时验证
_PACKAGED_RUN = _FROZEN or os.environ.get("OKNTE_PLANNER_USER_DATA") == "1"
if _FROZEN:
    ROOT = os.path.dirname(os.path.abspath(sys.executable))  # EXE 旁（出厂数据表位）
else:
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 仓库根/dist 根
# 用户数据目录：打包态实际数据位；源码态仅供迁移 CLI（tools/migrate_user_data）引用。
# 两个 EXE 共用同一份（requireAdministrator 异账户提权时指向提权账户的 LocalAppData）
USER_DATA_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local"),
    "OKntePlanner", "data")
if _PACKAGED_RUN:
    BUNDLED_DATA_DIR = os.path.join(ROOT, "data")  # EXE 旁出厂数据表（首启播种源）
    DATA_DIR = USER_DATA_DIR
else:  # 源码态：行为与旧版完全一致（数据落仓库/dist data/，不触发任何迁移）
    BUNDLED_DATA_DIR = None
    DATA_DIR = os.path.join(ROOT, "data")
STATE_FILE = os.path.join(DATA_DIR, "state.json")


def save_json_atomic(path, data):
    """全项目唯一的 JSON 原子写实现（state.json/yields/requirements/runtime 共用）。

    tmp 写全 → 复制当前主文件为 .bak（上一次完好版本）→ tmp 原子替换主文件。
    任何时刻崩溃，主文件要么是旧完整内容、要么是新完整内容，绝不出现半截 JSON。
    旧实现先用 os.replace 把主文件轮转为 .bak 再替换，两步之间崩溃会让主文件
    缺失、只剩 .bak（真机曾出现该形态的 state.json）——现改为先复制后替换，
    主文件除原子替换瞬间外始终存在。
    """
    tmp = Path(path + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.path.exists(path):
        bak_tmp = Path(path + ".bak.tmp")
        try:
            shutil.copyfile(path, bak_tmp)
            os.replace(bak_tmp, path + ".bak")
        except OSError:
            pass  # .bak 落后一版可接受；绝不因备份失败丢掉本次写入
    os.replace(tmp, path)


def load_json(path):
    """容错读取：主文件优先；损坏时留档 .corrupt 并回退 .bak；都没有返回 None。"""
    def _read(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    try:
        return _read(path)
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        try:  # 保留损坏文件供人工检查，再尝试上一次完好版本
            os.replace(path, path + ".corrupt")
        except OSError:
            pass
    try:
        return _read(path + ".bak")
    except (OSError, ValueError):
        return None


# ---- 首启用户数据初始化/迁移（BUILD-01/SEC-01：打包态用户数据统一落 %LOCALAPPDATA%）----

def _remove_quiet(path):
    """尽力删除单个文件（清理复制失败留下的 .tmp 残骸），任何失败静默吞掉。"""
    try:
        os.remove(path)
    except OSError:
        pass


def _copy_tree_no_clobber(src, dst, dry_run=False):
    """整树逐文件原子 no-clobber 复制（首启播种/迁移 CLI/旧数据迁移共用机械实现）。

    语义（迁移契约）：
    - 只复制：绝不移动或删除源目录任何文件；目标已存在的文件一律跳过
      （no-clobber——多个源按处理顺序先到先得，同路径文件先处理的源获胜）。
    - 单文件原子：先写 <dst>.<uuid>.tmp 再 os.rename(tmp, dst)——Windows 上
      rename 到已存在目标抛 FileExistsError，天然原子 no-clobber；tmp 名含
      uuid 防双 EXE 并发首播互踩；进程被硬杀落在复制中途只会留下 .tmp 残骸，
      目标文件要么不存在要么完整，绝不半截（下次启动 no-clobber 自愈重试，
      零失败照写 marker——截断表会让 planner 的裸 json.load 崩且无 .bak 回退）。
    - realpath 相同（src==dst）直接返回，防自拷。
    - check-then-rename 在 POSIX 下存在理论竞态窗口：本产品为 win32 专用构建链，
      可忽略。
    返回 {dirs, copied, skipped, failed, errors}（errors 为目标侧相对路径清单，
      绝不含文件内容或任何敏感值）。
    """
    result = {"dirs": 0, "copied": 0, "skipped": 0, "failed": 0, "errors": []}
    src = os.path.abspath(src)
    dst = os.path.abspath(dst)
    if os.path.realpath(src) == os.path.realpath(dst):
        return result  # 源目标同一目录：跳过防自拷
    for base, _dirs, files in os.walk(src):  # followlinks 默认 False：符号链接目录不进入
        rel = os.path.relpath(base, src)
        target_dir = dst if rel == os.curdir else os.path.join(dst, rel)
        if not os.path.isdir(target_dir):
            if dry_run:
                result["dirs"] += 1
            else:
                try:
                    os.makedirs(target_dir)
                except FileExistsError:  # 并发对手（双 EXE 首播）已建
                    pass
                except OSError:
                    result["failed"] += 1
                    result["errors"].append("." if rel == os.curdir else rel)
                else:
                    result["dirs"] += 1
        for name in files:
            s = os.path.join(base, name)
            d = os.path.join(target_dir, name)
            if os.path.isdir(d):
                # 同名目录占位：文件落不进去，属失败（半迁移状态 → marker 不写，下次可自愈重试）
                result["failed"] += 1
                result["errors"].append(os.path.relpath(d, dst))
                continue
            if os.path.exists(d):
                result["skipped"] += 1
                continue
            if dry_run:
                result["copied"] += 1
                continue
            tmp = "%s.%s.tmp" % (d, uuid.uuid4().hex[:8])
            try:
                shutil.copy2(s, tmp)
                os.rename(tmp, d)
                result["copied"] += 1
            except FileExistsError:
                _remove_quiet(tmp)
                result["skipped"] += 1  # 并发对手已先复制完成
            except OSError as exc:
                _remove_quiet(tmp)
                if getattr(exc, "errno", None) == errno.EEXIST:
                    result["skipped"] += 1
                else:
                    result["failed"] += 1
                    result["errors"].append(os.path.relpath(d, dst))
    return result


def _dir_has_files(path):
    for _base, _dirs, files in os.walk(path):
        if files:
            return True
    return False


def _migration_note(user_dir, msg):
    """迁移告警双通道：user_dir/migration.log（尽力而为）+ stderr（noconsole 下
    sys.stderr 为 None 时跳过）。只写一行文本，绝不落任何文件内容/敏感值。"""
    try:
        with open(os.path.join(user_dir, "migration.log"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass
    try:
        if sys.stderr is not None:
            print("[user-data] %s" % msg, file=sys.stderr)
    except (OSError, ValueError):
        pass


def ensure_user_data(user_dir, bundled_dir, marker_path=None):
    """首启用户数据初始化/迁移（幂等、无网络、显式参数；绝不移动或删除源目录）。

    - marker（默认 <user_dir>/.migrated）已存在 → skip（幂等门）。
    - bundled 存在且含任意文件 → 整树原子 no-clobber 复制到 user_dir：旧 EXE 旁
      用户数据+出厂数据表一并迁移；全新安装 EXE 旁 data 只含出厂数据表，同一
      复制即『播种』——两分支统一一套机械实现。复制零失败才用 save_json_atomic
      写 marker（原子写，绝不半写——半建 marker 会让迁移永久跳过）；有失败抛
      OSError（marker 未写 → 下次启动自愈重试）。marker 内容=时间戳+来源目录
      +计数，无敏感值。
    - bundled 缺失/为空 → 仅建 user_dir 并每次启动告警『EXE 旁缺少出厂数据表』，
      不写 marker（用户日后补齐 EXE 旁数据表后首播播种照常发生，自愈门保持
      开启）。
    返回 {"action": "skip"|"empty"|"migrated", "stats": …}。
    """
    os.makedirs(user_dir, exist_ok=True)
    marker = marker_path if marker_path is not None \
        else os.path.join(user_dir, ".migrated")
    if os.path.isfile(marker):
        return {"action": "skip", "stats": None}
    if not bundled_dir or not os.path.isdir(bundled_dir) \
            or not _dir_has_files(bundled_dir):
        _migration_note(user_dir, "EXE 旁缺少出厂数据表（%s 缺失或为空），本次未播种；"
                                  "补齐后重启面板将自动补种" % (bundled_dir or "出厂数据目录",))
        return {"action": "empty", "stats": None}
    stats = _copy_tree_no_clobber(bundled_dir, user_dir)
    if stats["failed"]:
        raise OSError("用户数据初始化/迁移有 %d 项失败（下次启动自动重试）：%s"
                      % (stats["failed"], "; ".join(stats["errors"])))
    save_json_atomic(marker, {
        "migrated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": bundled_dir,
        "dirs": stats["dirs"],
        "copied": stats["copied"],
        "skipped": stats["skipped"],
    })
    return {"action": "migrated", "stats": stats}


def _ensure_user_data_boot():
    """导入期首启初始化包装：任何 OSError 只告警不抛出（失败绝不阻断面板启动，
    marker 未写 → 下次启动自愈重试）；成功迁移记一行首启可见日志。"""
    try:
        result = ensure_user_data(USER_DATA_DIR, BUNDLED_DATA_DIR)
    except OSError as exc:
        _migration_note(USER_DATA_DIR,
                        "首启用户数据初始化失败（下次启动自动重试）：%s" % exc)
        return
    if result["action"] == "migrated":
        stats = result["stats"]
        _migration_note(USER_DATA_DIR,
                        "首启用户数据初始化完成：新建目录 %d、复制文件 %d、跳过已存在 %d"
                        % (stats["dirs"], stats["copied"], stats["skipped"]))

DEFAULT_SETTINGS = {
    "oknte_root": r"D:\GamingAssistant\ok-nte",
    "daily_stamina_limit": 180,
    "daily_reset_hour": 4,
    "reserve_anomaly_hunter": False,
    "anomaly_hunter_stamina": 180,
    "anomaly_hunter_target": "海囚",
    "notify_enabled": False,
    "max_run_minutes": 90,
    "continuous_mode": True,
    "busy_retry_seconds": 300,
    "port": 8765,
    "known_oknte_version": "",
    # 体力恢复模型（阶段9；阶段15 上限默认 320：家具扩展后游戏内体力上限会升高，设置页可改）
    "stamina_cap": 320,
    "stamina_regen_per_hour": 10,
    "stamina_now": None,
    "stamina_now_at": "",
    # 背包 OCR v2：游戏窗口标题关键词（逗号分隔，任一命中即绑定）与自动翻页页数上限
    "game_window_keywords": "异环,Neverness",
    "ocr_max_pages": 5,
    # 背包 OCR v3：游戏进程名（空=从 ok-nte devices.json 自动读取；/api/ocr/select 后落盘）
    "game_process_name": "",
    # 角色 OCR 全局紧急停止热键（Ctrl+Alt+Q，提权面板监听游戏焦点按键；出错时游戏内一键停）
    "ocr_stop_hotkey": True,
}

TRACK_KEYS = ["char_level", "ability_level", "arc_level", "console_level"]

STATE_SCHEMA = 3  # 自身数据格式版本（导出/导入与未来迁移的依据）
# v2: 角色库/武器库/追猎记账；v3: teams 兜底 + 清洗卡带空值曾被存成字符串 "None" 的脏数据


def _migrate(data):
    """schema 迁移钩子：逐级升级；幂等、只补缺/修脏值，绝不删除任何用户数据。"""
    if not isinstance(data, dict):
        data = {}
    data.setdefault("char_library", [])
    data.setdefault("weapon_library", [])
    data.setdefault("teams", [])
    today = data.get("today")
    if isinstance(today, dict):
        today.setdefault("hunter_spent", 0)
    for char in data.get("characters") or []:
        if not isinstance(char, dict):
            continue
        cassette = char.get("cassette")
        if isinstance(cassette, dict):
            # 旧版 sanitize 把 None 用 str() 转成了字符串 "None" 存库（v3 前的缺陷），
            # 回显时与下拉选项永远匹配不上——这里清洗回 None，字段本身保留
            for key in ("set", "main"):
                if cassette.get(key) == "None":
                    cassette[key] = None
    data["schema"] = STATE_SCHEMA
    return data


def new_character(name=""):
    return {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "enabled": True,
        "mats": {"ability": 1, "arc": 1, "console": 1},
        "tracks": {key: {"cur": 1, "tgt": 1} for key in TRACK_KEYS},
        "extra_needs": {},
    }


def _empty_today(day):
    return {
        "date": day,
        "used_stamina": 0,
        "planner_spent": 0,
        "hunter_spent": 0,
        "blacklist": [],
        "entries": [],
    }


def _apply_default_settings(settings):
    """默认值只补缺，绝不覆盖用户已保存的值。

    历史缺陷（真机实证）：此处曾用 _deep_merge(用户设置, DEFAULT_SETTINGS)，实参顺序
    使默认值获胜——每次启动限额/端口/持续模式等标量设置全部回退（panel.log 里端口
    反复避让 16 次即此痕迹）。
    """
    for key, value in DEFAULT_SETTINGS.items():
        settings.setdefault(key, value)


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.data = self._load()
        self.rev = 0  # 数据版本号（仅存内存，重启归零无碍）：/api/bootstrap 下发、
        # 写端点响应回传；前端保存时携带 base_rev 比对，防止多标签页陈旧 DOM 整行回写
        # 把塔吉多同步刚抬起的 tgt 打回（诊断 item6）

    def _load(self):
        data = load_json(STATE_FILE)
        if not isinstance(data, dict):
            data = {}
        # 类型防线：JSON 合法但字段类型被改坏（手改 state.json 的高发形态）时
        # 重置对应段回默认，而不是在属性访问处抛异常让面板无法启动。
        if not isinstance(data.get("settings"), dict):
            data["settings"] = {}
        _apply_default_settings(data["settings"])
        data["settings"] = {k: data["settings"][k] for k in DEFAULT_SETTINGS}
        if not isinstance(data.get("characters"), list):
            data["characters"] = []
        if not isinstance(data.get("inventory"), dict):
            data["inventory"] = {}
        if not isinstance(data.get("history"), list):
            data["history"] = []
        data = _migrate(data)
        self.data = data  # 先就位，day_key() 要读 settings
        today = data.get("today")
        if isinstance(today, dict) and today.get("date") == self.day_key():
            for key in ("used_stamina", "planner_spent", "hunter_spent"):
                value = today.get(key)
                if not isinstance(value, int) or isinstance(value, bool):
                    today[key] = 0
            if not isinstance(today.get("blacklist"), list):
                today["blacklist"] = []
            if not isinstance(today.get("entries"), list):
                today["entries"] = []
        else:
            data["today"] = _empty_today(self.day_key())
        return data

    def save(self):
        with self.lock:
            self.data["schema"] = STATE_SCHEMA
            save_json_atomic(STATE_FILE, self.data)
            self.rev += 1  # 落盘成功后自增：任何一次写入都使其他标签页的 base_rev 过期

    # ---- 当日窗口 ----
    def day_key(self, now=None):
        now = now or datetime.now()
        reset_hour = int(self.data["settings"].get("daily_reset_hour", 4))
        if now.hour < reset_hour:
            now = datetime(now.year, now.month, now.day) - timedelta(days=1)  # 未到重置点，仍属前一天
        return now.strftime("%Y-%m-%d")

    def ensure_today(self):
        day = self.day_key()
        with self.lock:
            if self.data["today"].get("date") != day:
                self.data["today"] = _empty_today(day)
                self.save()
        return day

    # ---- 供 Web/Runner 使用的便捷访问 ----
    @property
    def settings(self):
        return self.data["settings"]

    @property
    def characters(self):
        return self.data["characters"]

    @property
    def inventory(self):
        return self.data["inventory"]

    @property
    def today(self):
        return self.data["today"]

    @property
    def history(self):
        return self.data["history"]


if _PACKAGED_RUN:
    # 打包态导入期即完成首启播种/迁移：STATE 在下方导入期实例化并读 STATE_FILE，
    # 必须在此之前把 EXE 旁数据整树搬入用户数据目录；失败只告警不阻断启动
    _ensure_user_data_boot()

STATE = State()
