"""规划器状态持久化：设置、角色、库存、当日进度、历史。"""
import json
import os
import sys
import threading
import uuid
from datetime import datetime, timedelta

if getattr(sys, "frozen", False):  # PyInstaller 等打包环境：用户数据放 EXE 旁边，不进临时解包目录
    ROOT = os.path.dirname(os.path.abspath(sys.executable))
else:
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
STATE_FILE = os.path.join(DATA_DIR, "state.json")


def save_json_atomic(path, data):
    """全项目唯一的 JSON 原子写实现（state.json/yields/requirements/runtime 共用）。

    tmp 写全 → 旧主文件轮转为 .bak（上一次完好版本）→ tmp 替换为主文件。
    任何时刻崩溃，主文件要么是旧完整内容、要么是新完整内容，绝不出现半截 JSON；
    替换窗口内主文件短暂缺失时，读取方由 .bak 兜底（见 load_json）。
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if os.path.exists(path):
        os.replace(path, path + ".bak")
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
<<<<<<< HEAD
    # 体力恢复模型（阶段9；阶段15 上限默认 320：家具扩展后游戏内体力上限会升高，设置页可改）
    "stamina_cap": 320,
=======
    # 体力恢复模型（阶段9）：cap/regen 为参数；stamina_now 为用户手填的当前体力，
    # stamina_now_at 由服务端在保存时盖章（推算基准时刻）
    "stamina_cap": 240,
>>>>>>> 53d44889c0d699d37fd730b0019e617da1b66bb4
    "stamina_regen_per_hour": 10,
    "stamina_now": None,
    "stamina_now_at": "",
}

TRACK_KEYS = ["char_level", "ability_level", "arc_level", "console_level"]

STATE_SCHEMA = 2  # 自身数据格式版本（导出/导入与未来迁移的依据）；v2: 角色库/武器库/追猎记账


def _migrate(data):
    """schema 迁移钩子：v1 文件按 v2 兼容读取并补默认键；未来版本在这里逐级升级后再使用。"""
    if not isinstance(data, dict):
        data = {}
    data.setdefault("char_library", [])
    data.setdefault("weapon_library", [])
    today = data.get("today")
    if isinstance(today, dict):
        today.setdefault("hunter_spent", 0)
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


def _deep_merge(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.data = self._load()

    def _load(self):
        data = load_json(STATE_FILE)
        if not isinstance(data, dict):
            data = {}
        data.setdefault("settings", {})
        _deep_merge(data["settings"], DEFAULT_SETTINGS)
        data["settings"] = {k: data["settings"][k] for k in DEFAULT_SETTINGS}
        data.setdefault("characters", [])
        data.setdefault("inventory", {})
        data.setdefault("history", [])
        data = _migrate(data)
        self.data = data  # 先就位，day_key() 要读 settings
        data.setdefault("today", _empty_today(self.day_key()))
        return data

    def save(self):
        with self.lock:
            self.data["schema"] = STATE_SCHEMA
            save_json_atomic(STATE_FILE, self.data)

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


STATE = State()
