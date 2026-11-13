"""SEC-10 / B2.3 受限提权辅助进程（方案B，用户裁决 2026-11-14）。

主面板（HTTP 服务、token 闸、OCR 截屏与识别、状态文件）全部降为普通权限运行；
确需管理员权限的操作收敛到本进程，操作白名单封闭：

- launch_runner：提权拉起 ok-nte 无头 runner 子链。解释器固定为启动时固化指纹的
  oknte_root 内嵌 python.exe（路径 + SHA-256，指纹变化一律拒绝）；launcher 只认
  <root>/working/configs_planner/run_planner_task.py；stdout 只能重定向到启动时
  固定的面板数据目录内、文件名匹配白名单正则的日志文件。
- stop_runner：只允许停止本会话内经 launch_runner 拉起的进程。
- set_foreground / click / scroll：向游戏窗口的合成输入原语。目标窗口必须通过
  窗口校验（IsWindow + 可见 + 进程名与启动时固化的 game_process 一致，或窗口类
  UnrealWindow）；坐标钳制在虚拟屏幕范围内。
- ping / shutdown：握手与退出。

边界口径（诚实声明，绝不过度声称）：
- IPC 只听 127.0.0.1 临时端口，每个请求必须携带会话令牌（hmac.compare_digest
  常量时间比较），未认证连接一律拒绝并审计。
- 本进程不声称防御「同用户同权限的恶意进程」——那是 OS 级边界（同用户进程可读
  面板内存与令牌文件），不在本修复声称范围内。本设计只把边界推到：未认证的本机
  连接、其他用户会话、以及「Web 面板面被攻破时不能直接触达管理员权限操作」。
- oknte_root 与内嵌解释器指纹固化于启动时；变更需面板重新经 UAC 拉起新会话
  （再次 UAC 同意），旧会话拒绝服务。
- 生命周期：看门狗监听父面板进程句柄（随主进程退出）、shutdown、父进程死亡
  兜底轮询。所有请求（含拒绝）写安全事件审计日志；令牌绝不落盘、绝不进审计。

纯标准库实现：本模块绝不 import 本仓库其他模块（被提权进程独立拉起，依赖面最小）。
"""
import ctypes
import hashlib
import hmac
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000

CONFIG_SCHEMA = 1
# 配置文件名由面板生成并经 argv 传入；白名单正则防 argv 注入任意路径
CONFIG_NAME_RE = re.compile(r"^elev_helper_config-\d{1,10}-[0-9a-f]{8}\.json$")
TOKEN_RE = re.compile(r"^[0-9a-f]{32,128}$")
# runner stdout 镜像文件名：仅面板数据目录下、无路径分隔符，固定前缀防任意文件写
STDOUT_NAME_RE = re.compile(r"^ok-nte-headless-[A-Za-z0-9._-]{1,80}\.log$")
LAUNCHER_NAME = "run_planner_task.py"
TASKS = ("日常任务", "AnomalyHunter")
PROFILE_DIR_NAME = "configs_planner"
GAME_WINDOW_CLASS = "UnrealWindow"
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
REQUEST_DEADLINE = 15.0   # 单请求读写总时限（秒）
STOP_RUNNER_LIMIT = 30.0  # stop_runner 内部 taskkill 上限（秒）

KERNEL32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
USER32 = ctypes.WinDLL("user32.dll", use_last_error=True)
PROCESS_SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED = 0x1000
INFINITE = 0xFFFFFFFF


class ConfigError(Exception):
    """辅助进程启动配置非法（fail-closed：配置不可信宁可拒绝启动）。"""


# ---------- 纯校验函数（selftest 直接覆盖，不触系统资源） ----------

def safe_stdout_name(name):
    """stdout 镜像文件名白名单：固定前缀 + 无路径分隔符，拒绝任意文件写。"""
    return STDOUT_NAME_RE.fullmatch(str(name or "")) is not None


def task_allowed(task):
    """runner 任务名白名单（与 ok-nte 无头链两个入口一一对应）。"""
    return str(task or "") in TASKS


def coords_in_bounds(x, y, vs):
    """坐标必须在虚拟屏幕范围内（vs=(vx, vy, vw, vh)）。注入坐标越界会点到
    游戏窗口之外的其他应用（与 main._sanitize_ocr_layout 同口径）。"""
    try:
        vx, vy, vw, vh = (int(v) for v in vs)
        x, y = int(x), int(y)
    except (TypeError, ValueError):
        return False
    if vw <= 0 or vh <= 0:
        return False
    return vx <= x < vx + vw and vy <= y < vy + vh


def launcher_allowed(launcher, root):
    """launcher 必须是 <root>/working/configs_planner/run_planner_task.py
    （realpath 归属校验）：辅助进程提权执行的脚本面固定为面板写入配置副本
    目录的单一入口，拒绝任意 Python 脚本路径。"""
    try:
        root_real = os.path.realpath(str(root or ""))
        launcher_real = os.path.realpath(str(launcher or ""))
        profile_real = os.path.realpath(
            os.path.join(root_real, "working", PROFILE_DIR_NAME))
        return (os.path.dirname(launcher_real) == profile_real
                and os.path.basename(launcher_real) == LAUNCHER_NAME)
    except (OSError, ValueError):
        return False


def profile_allowed(profile, root):
    """profile 目录必须是 <root>/working/configs_planner。"""
    try:
        root_real = os.path.realpath(str(root or ""))
        return os.path.realpath(str(profile or "")) == os.path.realpath(
            os.path.join(root_real, "working", PROFILE_DIR_NAME))
    except (OSError, ValueError):
        return False


def _contained(path, base):
    """路径归属校验（realpath + commonpath，与本仓库 selftest/_avatar 同口径）。
    目标 realpath 必须落在 base realpath 之内，否则拒绝——所有由外部输入
    （配置/请求参数）参与拼接的路径都必须先过本闸，杜绝目录逃逸。"""
    base_real = os.path.realpath(str(base or ""))
    path_real = os.path.realpath(str(path or ""))
    if not base_real or not path_real \
            or os.path.commonpath([path_real, base_real]) != base_real:
        raise ConfigError("路径归属校验失败: 目标不在允许目录内")
    return path_real


def _sha256_file(path):
    """文件 SHA-256（分块读取；内嵌解释器指纹固化用）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(256 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------- 启动配置 ----------

def _config_args_from_argv(argv):
    """argv 解析 --config <目录> <文件名>；文件名必须匹配白名单正则
    （无分隔符，拼接在构造上不可能越出目录）。返回 (目录, 文件名)。"""
    directory = None
    name = None
    i = 0
    while i < len(argv):
        if argv[i] == "--config" and i + 2 < len(argv):
            directory, name = argv[i + 1], argv[i + 2]
            i += 3
            continue
        i += 1
    if not directory or not name \
            or not CONFIG_NAME_RE.fullmatch(name):
        raise ConfigError("缺少或非法的 --config 参数（文件名白名单正则不匹配）")
    return directory, name


def _read_json_file(path):
    """容错 JSON 读取（read-only；坏文件返回 None）。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _locate_python(root):
    """在 oknte_root 内定位内嵌 python.exe（realpath + commonpath 归属闸）。"""
    root_real = os.path.realpath(str(root or ""))
    py_real = os.path.realpath(os.path.join(root_real, "data", "apps",
                                            "ok-nte", "python", "python.exe"))
    if os.path.commonpath([py_real, root_real]) != root_real:
        raise ConfigError("解释器路径不在 oknte_root 内")
    return py_real


def validate_config(cfg, config_path, config_dir_real):
    """校验启动配置（fail-closed）。返回 (token, parent_pid, root, log_dir,
    game_process)。config_dir_real 是 main() 锚定的配置目录 realpath；
    config_path 已在 main() 归属校验通过（realpath 后 dirname 必须等于锚定目录）。"""
    if not isinstance(cfg, dict) or cfg.get("schema") != CONFIG_SCHEMA:
        raise ConfigError("配置 schema 不符")
    token = str(cfg.get("token") or "")
    if not TOKEN_RE.fullmatch(token):
        raise ConfigError("令牌格式非法")
    parent_pid = cfg.get("parent_pid")
    if not isinstance(parent_pid, int) or isinstance(parent_pid, bool) \
            or not 1 <= parent_pid <= 2147483647:
        raise ConfigError("parent_pid 非法")
    root = str(cfg.get("root") or "")
    log_dir = str(cfg.get("log_dir") or "")
    if not root or not os.path.isdir(root):
        raise ConfigError("oknte_root 不存在或不是目录")
    if not log_dir or not os.path.isdir(log_dir):
        raise ConfigError("log_dir 不存在或不是目录")
    # 配置文件必须位于 log_dir 内（防外部目录里的恶意配置顶替会话配置）
    if os.path.realpath(config_dir_real) != os.path.realpath(log_dir):
        raise ConfigError("配置文件必须位于 log_dir 内")
    py_path = _locate_python(root)
    if not os.path.isfile(py_path):
        raise ConfigError("oknte_root 内未找到内嵌 python.exe")
    game_process = str(cfg.get("game_process") or "").strip()
    if not game_process or len(game_process) > 64:
        raise ConfigError("game_process 需要非空短字符串")
    return token, parent_pid, root, log_dir, game_process


# ---------- Windows 窗口原语（辅助进程侧） ----------

def _window_process_name(hwnd):
    """hwnd 所属进程镜像名（跨完整性级别可读）。失败返回空串。"""
    try:
        wpid = ctypes.c_uint32(0)
        USER32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd),
                                        ctypes.byref(wpid))
        if not wpid.value:
            return ""
        proc = KERNEL32.OpenProcess(PROCESS_QUERY_LIMITED, False, wpid)
        if not proc:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_uint32(1024)
            if KERNEL32.QueryFullProcessImageNameW(
                    ctypes.c_void_p(proc), 0, buf, ctypes.byref(size)):
                name = buf.value.replace("/", "\\")
                return name.rsplit("\\", 1)[-1] if name else ""
            return ""
        finally:
            KERNEL32.CloseHandle(ctypes.c_void_p(proc))
    except (OSError, ValueError):
        return ""


def _window_class_name(hwnd):
    """窗口类名（无消息语义，跨完整性级别可读）。失败返回空串。"""
    try:
        buf = ctypes.create_unicode_buffer(256)
        n = USER32.GetClassNameW(ctypes.c_void_p(hwnd), buf, 256)
        return buf.value if n else ""
    except (OSError, ValueError):
        return ""


def window_allowed(hwnd, game_process):
    """输入原语的目标窗口校验：真实存在 + 可见 +（进程名匹配固化 game_process
    或类名 UnrealWindow）。拒绝把合成输入发往任意窗口。"""
    try:
        hwnd = int(hwnd)
    except (TypeError, ValueError):
        return False, "hwnd 不是整数"
    if hwnd <= 0:
        return False, "hwnd 非法"
    if not USER32.IsWindow(ctypes.c_void_p(hwnd)):
        return False, "窗口不存在（IsWindow 失败）"
    if not USER32.IsWindowVisible(ctypes.c_void_p(hwnd)):
        return False, "窗口不可见"
    proc = _window_process_name(hwnd).lower()
    if proc.endswith(".exe"):
        proc = proc[:-4]
    target = str(game_process or "").strip().lower()
    if target.endswith(".exe"):
        target = target[:-4]
    if not proc or (proc != target and _window_class_name(hwnd) != GAME_WINDOW_CLASS):
        return False, "窗口不属于固化的游戏进程或 UnrealWindow 类"
    return True, proc


def _virtual_screen():
    """虚拟屏幕矩形 (x, y, w, h)（坐标钳制基准）。"""
    try:
        return (int(USER32.GetSystemMetrics(76)),
                int(USER32.GetSystemMetrics(77)),
                int(USER32.GetSystemMetrics(78)),
                int(USER32.GetSystemMetrics(79)))
    except (OSError, ValueError):
        return 0, 0, 0, 0


# ---------- 辅助进程主体 ----------

class _Helper:
    """单会话提权辅助进程。一次启动服务一个面板会话：令牌、oknte_root 指纹、
    父进程、审计与端点文件位置都在启动时固化；任何一项变化都以拒绝服务收场。"""

    def __init__(self, token, parent_pid, root, log_dir, game_process,
                 config_path):
        self.token = token
        self.parent_pid = parent_pid
        self.root = os.path.realpath(root)
        self.log_dir = os.path.realpath(log_dir)
        self.game_process = game_process
        self.py_path = _locate_python(self.root)
        self.py_fp = _sha256_file(self.py_path)
        self.children = {}  # pid -> Popen（本会话经 launch_runner 拉起的进程）
        self.finished = {}  # pid -> exit code（已收尾子进程；供 stop_runner 幂等与面板判定）
        self.stop_event = threading.Event()
        self.audit_lock = threading.Lock()
        self.audit_path = os.path.join(self.log_dir, "elev_helper_audit.log")
        self.endpoint_path = os.path.join(
            self.log_dir, "elev_helper_endpoint-%d.json" % parent_pid)
        self.config_path = config_path
        self.vs = _virtual_screen()
        self.ops = {
            "ping": self._op_ping,
            "shutdown": self._op_shutdown,
            "launch_runner": self._op_launch_runner,
            "stop_runner": self._op_stop_runner,
            "set_foreground": self._op_set_foreground,
            "click": self._op_click,
            "scroll": self._op_scroll,
        }

    # ---- 审计与端点 ----
    def audit(self, event, op="-", ok=None, detail=""):
        """安全事件审计（JSONL 追加；绝不记录令牌）。"""
        entry = {
            "t": time.strftime("%Y-%m-%d %H:%M:%S"),
            "pid": os.getpid(),
            "event": str(event),
            "op": str(op),
        }
        if ok is not None:
            entry["ok"] = bool(ok)
        if detail:
            entry["detail"] = str(detail)[:300]
        try:
            with self.audit_lock:
                with Path(self.audit_path).open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 审计尽力而为，绝不因审计失败中断服务主流程

    def _write_endpoint(self, port):
        """端点文件（pid+port，非机密）：面板侧轮询读取。pathlib 写入。"""
        endpoint = {
            "pid": os.getpid(), "port": int(port),
            "root_fp": self.py_fp[:16],
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        Path(self.endpoint_path).write_text(
            json.dumps(endpoint), encoding="utf-8")

    # ---- 生命周期 ----
    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(4)
        sock.settimeout(1.0)
        self._write_endpoint(sock.getsockname()[1])
        try:  # 配置文件读毕即删：缩小令牌在盘面的存在窗口
            os.remove(self.config_path)
        except OSError:
            pass
        self.audit("start", detail="port=%d fp=%s parent=%d admin=%s"
                   % (sock.getsockname()[1], self.py_fp[:12], self.parent_pid,
                      bool(ctypes.windll.shell32.IsUserAnAdmin())))
        threading.Thread(target=self._watch_parent, daemon=True,
                         name="oknte-elev-watchdog").start()
        threading.Thread(target=self._reap_children, daemon=True,
                         name="oknte-elev-reaper").start()
        try:
            while not self.stop_event.is_set():
                try:
                    conn, addr = sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    if addr[0] != "127.0.0.1":
                        conn.close()
                        self.audit("reject", detail="非回环来源")
                        continue
                    self._handle(conn)
                finally:
                    try:
                        conn.close()
                    except OSError:
                        pass
        finally:
            sock.close()
            try:
                os.remove(self.endpoint_path)
            except OSError:
                pass
            self.audit("exit", detail="children=%d" % len(self.children))

    def _die(self, reason):
        """看门狗触发的退出：置位停止事件，accept 循环一秒内感知并收尾。"""
        self.audit("parent_exit", detail=str(reason)[:200])
        self.stop_event.set()

    def _reap_children(self):
        """收尾已退出的子进程并释放句柄：Popen 句柄若一直握着，已终止的进程对象
        不会消失，面板侧 OpenProcess 存活探测会把僵尸误判为存活（_ElevChild.poll
        永不返回退出）。每 2 秒 poll 一轮，退出者从 children 摘除（引用清零即
        关句柄）并记入 finished（pid → exit code，供 stop_runner 幂等）。
        注意 del proc：循环变量本身持有 Popen 引用，不显式释放会让最后收尾的
        那个子进程句柄永不关闭（真测踩过：面板侧存活探测恒真）。"""
        while not self.stop_event.is_set():
            for pid in list(self.children.keys()):
                proc = self.children.get(pid)
                if proc is None:
                    continue
                try:
                    code = proc.poll()
                except OSError:
                    code = None
                if code is not None:
                    with self.audit_lock:
                        self.children.pop(pid, None)
                        self.finished[pid] = code
                    self.audit("child_exit", detail="pid=%d code=%s" % (pid, code))
                del proc  # 立即释放 Popen 引用（见 docstring：句柄必须随引用清零关闭）
            time.sleep(2.0)

    def _watch_parent(self):
        """随主面板进程退出：优先阻塞等待父进程句柄；打不开句柄则轮询兜底。"""
        handle = KERNEL32.OpenProcess(PROCESS_SYNCHRONIZE, False,
                                      self.parent_pid)
        if handle:
            KERNEL32.WaitForSingleObject(ctypes.c_void_p(handle), INFINITE)
            KERNEL32.CloseHandle(ctypes.c_void_p(handle))
            self._die("父面板进程已退出（句柄等待）")
            return
        while not self.stop_event.is_set():
            handle = KERNEL32.OpenProcess(PROCESS_QUERY_LIMITED, False,
                                          self.parent_pid)
            if handle:
                KERNEL32.CloseHandle(ctypes.c_void_p(handle))
            elif ctypes.get_last_error() != 5:  # 5=ACCESS_DENIED 按存活保守处理
                self._die("父面板进程已退出（探测不存在）")
                return
            time.sleep(2.0)

    # ---- 协议 ----
    def _handle(self, conn):
        """单连接单请求：读一行 JSON，常量时间鉴权，白名单分发，回一行 JSON。"""
        try:
            conn.settimeout(REQUEST_DEADLINE)
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_REQUEST_BYTES:
                    raise ValueError("请求超限")
        except (OSError, ValueError):
            self.audit("reject", detail="请求读取失败或超限")
            return
        line = buf.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
        if not line:
            return
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError
        except ValueError:
            self.audit("reject", detail="请求不是 JSON 对象")
            self._respond(conn, {"ok": False, "error": "请求无效"})
            return
        op = str(req.get("op") or "")
        if not hmac.compare_digest(str(req.get("auth") or ""), self.token):
            self.audit("reject", op=op, ok=False, detail="鉴权失败")
            self._respond(conn, {"ok": False, "error": "鉴权失败"})
            return
        if self.stop_event.is_set():
            self._respond(conn, {"ok": False, "error": "辅助进程正在退出"})
            return
        handler = self.ops.get(op)
        if handler is None:
            self.audit("reject", op=op, ok=False, detail="未知操作")
            self._respond(conn, {"ok": False, "error": "未知操作"})
            return
        args = req.get("args")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            self.audit("reject", op=op, ok=False, detail="args 不是对象")
            self._respond(conn, {"ok": False, "error": "args 必须是对象"})
            return
        try:
            resp = handler(args)
            ok = bool(resp.get("ok"))
        except Exception as e:  # noqa: BLE001 —— 单请求失败不拖垮服务循环
            resp = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            ok = False
        self.audit("op" if ok else "reject", op=op, ok=ok,
                   detail="" if ok else str(resp.get("error", ""))[:200])
        self._respond(conn, resp)

    @staticmethod
    def _respond(conn, resp):
        try:
            data = (json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8")
            if len(data) > MAX_RESPONSE_BYTES:
                data = '{"ok": false, "error": "响应超限"}\n'.encode("utf-8")
            conn.sendall(data)
        except OSError:
            pass

    # ---- 操作实现 ----
    def _verify_python_fingerprint(self):
        """launch_runner 前的指纹复核：路径与 SHA-256 必须与启动时固化值一致。"""
        if os.path.realpath(self.py_path) != self.py_path \
                or not os.path.isfile(self.py_path) \
                or _sha256_file(self.py_path) != self.py_fp:
            raise RuntimeError("内嵌解释器指纹与启动时固化值不一致，拒绝执行")

    def _op_ping(self, args):
        return {"ok": True, "root_fp": self.py_fp[:16], "root": self.root,
                "admin": bool(ctypes.windll.shell32.IsUserAnAdmin()),
                "pid": os.getpid()}

    def _op_shutdown(self, args):
        self.stop_event.set()
        return {"ok": True, "bye": True}

    def _op_launch_runner(self, args):
        self._verify_python_fingerprint()
        launcher = str(args.get("launcher") or "")
        profile = str(args.get("profile") or "")
        task = str(args.get("task") or "")
        stdout_name = str(args.get("stdout") or "")
        if not launcher_allowed(launcher, self.root):
            raise RuntimeError("launcher 不在白名单路径内")
        if not os.path.isfile(launcher):
            raise RuntimeError("launcher 文件不存在")
        if not profile_allowed(profile, self.root) or not os.path.isdir(profile):
            raise RuntimeError("profile 目录不在白名单内")
        if not task_allowed(task):
            raise RuntimeError("task 不在白名单内")
        if not safe_stdout_name(stdout_name):
            raise RuntimeError("stdout 文件名不在白名单内")
        stdout_real = _stdout_file_path(self.log_dir, stdout_name)
        working = _working_dir(self.root)
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env["OKNTE_PLANNER_PROFILE"] = profile
        env["OKNTE_PLANNER_TASK"] = task
        with Path(stdout_real).open("ab") as f:
            proc = subprocess.Popen(
                [self.py_path, "-u", launcher], cwd=working, env=env,
                stdout=f, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW)
        self.children[proc.pid] = proc
        self.audit("launch", op="launch_runner", ok=True,
                   detail="pid=%d task=%s" % (proc.pid, task))
        return {"ok": True, "pid": proc.pid}

    def _op_stop_runner(self, args):
        pid = args.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise RuntimeError("pid 非法")
        if pid in self.finished:
            return {"ok": True, "pid": pid, "alive": False}  # 已收尾：幂等
        proc = self.children.get(pid)
        if proc is None:
            raise RuntimeError("pid 不是本会话经 launch_runner 拉起的进程")
        rc = None
        if proc.poll() is None:
            done = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                  capture_output=True, timeout=STOP_RUNNER_LIMIT,
                                  creationflags=CREATE_NO_WINDOW)
            rc = done.returncode
        self.audit("stop", detail="pid=%d taskkill_rc=%s" % (pid, rc))
        return {"ok": True, "pid": pid, "alive": proc.poll() is None}

    def _op_set_foreground(self, args):
        ok, why = window_allowed(args.get("hwnd"), self.game_process)
        if not ok:
            raise RuntimeError("窗口校验失败: " + why)
        done = USER32.SetForegroundWindow(ctypes.c_void_p(int(args["hwnd"])))
        return {"ok": True, "foreground": bool(done)}

    def _op_click(self, args):
        ok, why = window_allowed(args.get("hwnd"), self.game_process)
        if not ok:
            raise RuntimeError("窗口校验失败: " + why)
        if not coords_in_bounds(args.get("x"), args.get("y"), self.vs):
            raise RuntimeError("坐标越出虚拟屏幕范围")
        hwnd = int(args["hwnd"])
        USER32.SetForegroundWindow(ctypes.c_void_p(hwnd))
        time.sleep(0.05)
        USER32.SetCursorPos(int(args["x"]), int(args["y"]))
        time.sleep(0.10)
        USER32.mouse_event(0x0002, 0, 0, 0, ctypes.c_void_p(0))  # LEFTDOWN
        time.sleep(0.06)
        USER32.mouse_event(0x0004, 0, 0, 0, ctypes.c_void_p(0))  # LEFTUP
        return {"ok": True}

    def _op_scroll(self, args):
        ok, why = window_allowed(args.get("hwnd"), self.game_process)
        if not ok:
            raise RuntimeError("窗口校验失败: " + why)
        if not coords_in_bounds(args.get("x"), args.get("y"), self.vs):
            raise RuntimeError("坐标越出虚拟屏幕范围")
        notches = args.get("notches")
        wdelta = args.get("wheel_delta")
        if not isinstance(notches, int) or isinstance(notches, bool) \
                or not 1 <= notches <= 20:
            raise RuntimeError("notches 必须是 1-20 整数")
        if not isinstance(wdelta, int) or isinstance(wdelta, bool) \
                or not 120 <= wdelta <= 960:
            raise RuntimeError("wheel_delta 必须是 120-960 整数")
        hwnd = int(args["hwnd"])
        USER32.SetForegroundWindow(ctypes.c_void_p(hwnd))
        time.sleep(0.12)
        USER32.SetCursorPos(int(args["x"]), int(args["y"]))
        time.sleep(0.10)
        for _ in range(notches):
            USER32.mouse_event(0x0800, 0, 0, -int(wdelta), ctypes.c_void_p(0))
            time.sleep(0.06)
        return {"ok": True, "notches": notches}


def _stdout_file_path(log_dir, name):
    """stdout 镜像文件落点（realpath + commonpath 归属闸；name 已过白名单正则）。"""
    log_real = os.path.realpath(str(log_dir or ""))
    path_real = os.path.realpath(os.path.join(log_real, str(name or "")))
    if os.path.commonpath([path_real, log_real]) != log_real:
        raise RuntimeError("stdout 落点越出固定日志目录")
    return path_real


def _working_dir(root):
    """ok-nte working 目录（realpath + commonpath 归属闸）。"""
    root_real = os.path.realpath(str(root or ""))
    path_real = os.path.realpath(os.path.join(root_real, "working"))
    if os.path.commonpath([path_real, root_real]) != root_real:
        raise RuntimeError("working 目录归属校验失败")
    return path_real


def main(argv=None):
    """辅助进程入口。非法配置 exit 2；主循环随父进程退出/shutdown 结束。

    argv 只接受 --config <目录 + 文件名>：目录锚定（realpath + isdir）+ 文件名
    白名单正则（CONFIG_NAME_RE 无分隔符，拼接在构造上不可能越出目录），
    realpath 后再核对 dirname 与锚定目录一致——argv 参与的路径在 open 之前
    完成全部校验。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if os.name != "nt":
        print("elev_helper 仅支持 Windows", file=sys.stderr)
        return 3
    try:
        anchor_dir, name = _config_args_from_argv(argv)
        base_real = os.path.realpath(anchor_dir)
        if not base_real or not os.path.isdir(base_real):
            raise ConfigError("配置目录不存在")
        cfg_real = os.path.realpath(os.path.join(base_real, name))
        if os.path.dirname(cfg_real) != base_real:
            raise ConfigError("配置文件越出配置目录")
        cfg = _read_json_file(cfg_real)
        if cfg is None:
            raise ConfigError("配置文件读取失败")
        token, parent_pid, root, log_dir, game_process = \
            validate_config(cfg, cfg_real, base_real)
    except ConfigError as e:
        print("elev_helper 配置非法: %s" % e, file=sys.stderr)
        return 2
    helper = _Helper(token, parent_pid, root, log_dir, game_process, cfg_real)
    helper.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
