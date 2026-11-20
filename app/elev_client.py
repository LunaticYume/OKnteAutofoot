"""SEC-10 / B2.3 提权辅助进程的面板侧客户端（方案B）。

面板（普通权限）经本模块与受限提权辅助进程（app.elev_helper）通信：

- ensure(oknte_root)：确保辅助进程可用且固化指纹与 oknte_root 一致。root、内嵌
  解释器指纹变化或辅助进程已死（被强杀）时先停旧会话再重新经 UAC 拉起（变更
  需再次 UAC 同意）。root 结构不完整（无内嵌 python.exe）时直接失败，绝不弹 UAC。
- 生产拉起走 PowerShell Start-Process -Verb RunAs（与旧 run_panel.bat 同通道）；
  selftest/smoke 经 set_test_spawn(True) 走普通 Popen（跳过 UAC，仅测试路径）。
- 每个 IPC 请求携带会话令牌；令牌只经配置文件交接一次（面板写、辅助进程读毕即删），
  绝不进命令行、环境变量、日志或审计。
- 边界口径：本模块不声称防御「同用户同权限恶意进程」（OS 级边界，面板降权后其
  内存可被同用户进程读取）；只把边界推到「未认证的本机连接/其他用户会话/Web 面
  被攻破时不能直接触达管理员权限操作」。
"""
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import data_dacl
from . import elev_helper as _eh
from . import oknte_adapter as _oa  # SEC-11：写方 LAUNCHER_SHA256 期望值来源

CREATE_NO_WINDOW = _eh.CREATE_NO_WINDOW
CONFIG_NAME_RE = _eh.CONFIG_NAME_RE
ENDPOINT_NAME_RE = _eh.ENDPOINT_NAME_RE
STDOUT_NAME_RE = _eh.STDOUT_NAME_RE
LAUNCHER_NAME = _eh.LAUNCHER_NAME
TASKS = _eh.TASKS
PROFILE_DIR_NAME = _eh.PROFILE_DIR_NAME
HELPER_MODULE = "app.elev_helper"

_UAC_PS = ("Start-Process -FilePath '{exe}' -ArgumentList '{args}' "
           "-Verb RunAs -WindowStyle Hidden -WorkingDirectory '{cwd}'")


class ElevError(Exception):
    """提权辅助进程调用失败（连接失败/鉴权失败/操作被拒）。"""


class ElevRejected(ElevError):
    """辅助进程明确拒绝该次操作（白名单/参数/指纹）。会话本身仍然健康，
    客户端不得因此作废整个会话。"""


def set_test_spawn(value):
    """selftest/smoke 专用：True 时 ensure() 用普通 Popen 拉起辅助进程（无 UAC）。"""
    global _TEST_SPAWN
    _TEST_SPAWN = bool(value)


_TEST_SPAWN = False


def file_sha256(path):
    """暴露给测试与客户端的文件 SHA-256（复用辅助进程实现）。"""
    return _eh._sha256_file(path)


def request_once(port, token, op, args=None, timeout=15.0):
    """单次 IPC 请求（低层，测试直接使用；客户端经 _call 带锁复用）。"""
    payload = json.dumps({"auth": str(token), "op": str(op),
                          "args": dict(args or {})}) + "\n"
    with socket.create_connection(("127.0.0.1", int(port)), timeout=5.0) as s:
        s.settimeout(timeout)
        s.sendall(payload.encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 256 * 1024:
                raise ElevError("辅助进程响应超限")
    line = buf.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
    if not line:
        raise ElevError("辅助进程无响应")
    try:
        resp = json.loads(line)
    except ValueError:
        raise ElevError("辅助进程响应不是 JSON") from None
    if not isinstance(resp, dict) or not resp.get("ok"):
        raise ElevRejected(str((resp or {}).get("error") or "辅助进程返回失败"))
    return resp


def _pid_alive(pid):
    """进程存活探测（OpenProcess，绝不产生子进程；与 runner._pid_alive 同语义）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return kernel32.GetLastError() == 5
    except (OSError, AttributeError):
        return True


def _default_log_dir():
    """面板数据目录（运行时延迟导入，避免 elev_client 被 helper 侧误用时的环）。"""
    from .state import DATA_DIR
    return DATA_DIR


def _sweep_stale_helper_files(log_dir, keep_paths=()):
    """B2.7A 握手前陈旧孤儿清扫（兜硬杀孤儿，不改变既有正常退出删除链）。

    策略：正常退出双侧已删——客户端 finally 删 config（_start finally）、helper
    读毕即删 config（elev_helper.run 起始）、helper finally 删 endpoint——本清扫
    兜的是父进程/helper 被硬杀（taskkill /F、崩溃、断电）残留的孤儿：config 形态
    含会话令牌明文（高危，helper 读毕即删永不触发）、endpoint 形态仅 pid/port/
    指纹（非机密）。扫描时机在 _start 写本轮 config 之前（单写者点，客户端进程
    内执行，无需特权；与 helper 无并发写冲突——helper 只删自己的）。

    匹配口径：复用 elev_helper.CONFIG_NAME_RE 原样 ＋ 同风格 ENDPOINT_NAME_RE
    （B2.7A 导出），严格全名匹配不过宽 glob。防误删活跃会话三道闸：
    ① 本轮将要使用的路径经 keep_paths 显式排除；
    ② 名内嵌 pid 仍存活（OpenProcess 探测 _pid_alive；探测出错按存活处理）→
       跳过——天然排除本轮与并发活跃会话；
    ③ 文件 mtime 不早于本轮会话开始时间 → 跳过（pid 复用极端情形第二道闸，
    ②③双条件任一不满足即跳过）。
    OSError 全程静默尽力而为（沿 _start finally 既有兜底风格）：单文件失败不
    重试不刷日志，绝不影响拉起主流程。
    """
    sweep_at = time.time()
    keep = {os.path.normcase(os.path.abspath(str(p))) for p in keep_paths}
    try:
        names = os.listdir(log_dir)
    except OSError:
        return
    for name in names:
        if CONFIG_NAME_RE.fullmatch(name):
            pid = int(name[len("elev_helper_config-"):-len(".json")]
                      .split("-", 1)[0])
        elif ENDPOINT_NAME_RE.fullmatch(name):
            pid = int(name[len("elev_helper_endpoint-"):-len(".json")])
        else:
            continue  # 非本家族文件名（审计 JSONL 等）一律不碰
        path = os.path.join(str(log_dir), name)
        if os.path.normcase(os.path.abspath(path)) in keep:
            continue  # ① 本轮路径：绝不碰
        if pid == os.getpid() or _pid_alive(pid):
            continue  # ② pid 存活（含本轮自身 pid）：活跃会话跳过
        try:
            if os.stat(path).st_mtime >= sweep_at:
                continue  # ③ 会话开始后新写的：pid 复用兜底闸
        except OSError:
            continue
        try:
            os.remove(path)
        except OSError:
            pass  # 尽力而为：失败不重试、不刷日志、不影响主流程


class ElevClient:
    """面板侧会话管理：拉起（UAC/测试）、指纹一致性维护、白名单操作调用、退出清理。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._io_lock = threading.Lock()
        self._token = None
        self._port = None
        self._pid = None
        self._root = None       # 会话固化的 oknte_root（realpath）
        self._root_fp = None    # 会话固化的内嵌解释器 SHA-256
        self._log_dir = None
        self._reason = ""       # 最近一次不可用原因（面板日志/诊断用）

    # ---- 状态 ----
    def available(self):
        return self._port is not None

    def reason(self):
        return self._reason

    def _root_python(self, root):
        root_real = os.path.realpath(str(root or ""))
        py_real = os.path.realpath(os.path.join(
            root_real, "data", "apps", "ok-nte", "python", "python.exe"))
        if os.path.commonpath([py_real, root_real]) != root_real:
            raise ElevError("解释器路径不在 oknte_root 内")
        return py_real

    def _fp_matches(self, py_path):
        try:
            return file_sha256(py_path) == self._root_fp
        except OSError:
            return False

    # ---- 会话生命周期 ----
    def ensure(self, oknte_root, log_dir=None, game_process=None, timeout=90.0):
        """确保辅助进程可用且固化指纹与 oknte_root 一致（root/指纹变化或辅助进程
        已死 → 先停旧会话再重新拉起，需再次 UAC 同意）。root 结构不完整时直接
        失败不弹 UAC。返回 True/False；False 时 reason() 给出原因。"""
        with self._lock:
            root = os.path.realpath(str(oknte_root or ""))
            try:
                py_path = self._root_python(root)
            except ElevError as e:
                self._reason = str(e)
                return False
            if not os.path.isfile(py_path):
                self._reason = "oknte_root 无内嵌 python.exe: " + py_path
                return False
            if self._port is not None:
                # ELEV-01（W4b 2.4）：复用前必须探活——helper 被提权 taskkill /F
                # 强杀后面板内存态不失效，此分支曾对死会话假阳性直接复用（无重
                # 拉、无 UAC、审计无新 start 行）。_pid_alive 既有口径「探测出错
                # 按存活处理」（_sweep_stale_helper_files 闸②同源）：仅确认死
                # （False）才失效重拉；探活异常维持复用，避免瞬态探测错误引发
                # 多余 UAC。
                if self._root == root and self._fp_matches(py_path) \
                        and _pid_alive(self._pid):
                    return True  # 会话匹配且辅助进程存活：直接复用
                # root/指纹变化或辅助进程已死：旧会话失效，重新拉起（再次 UAC）。
                # shutdown() 对已死 helper 幂等安全（_call 连接失败即 _invalidate
                # 清态、ElevError 被其吞掉；等待循环 _pid_alive(None)=False 立即
                # 越过）；重拉链 _start 内 _sweep_stale_helper_files 随之执行（死
                # 会话残留孤儿 endpoint/config 一并清扫），新握手只认新 helper
                # 新写的端点（_read_endpoint+_handshake 既有链，新令牌不认旧内容）。
                self.shutdown()
            log_dir = os.path.realpath(log_dir or _default_log_dir())
            if not os.path.isdir(log_dir):
                self._reason = "面板数据目录不存在: " + log_dir
                return False
            try:
                return self._start(root, py_path, log_dir,
                                   str(game_process or "HTGame.exe"), timeout)
            except data_dacl.DataDaclError as e:
                # TJD-04：log_dir DACL 私有化失败（config 含会话令牌，_start 内闸）
                # ——沿既有 elev 失败链转 reason()+False（调用方
                # main._ensure_helper_for_ocr 据此置位 _ELEV_ENSURE_FAILED 并提示），
                # 绝不带半初始化会话继续。
                self._reason = "配置目录 DACL 私有化失败: " + str(e)
                return False

    def _start(self, root, py_path, log_dir, game_process, timeout):
        # TJD-04：写 config（含会话令牌）前闸目标目录 DACL 私有化；失败沿既有
        # elev 失败链（ensure() 转 reason()+False → _ELEV_ENSURE_FAILED 置位），
        # 绝不半写 config、不弹 UAC。
        data_dacl.require_private(log_dir)
        name = "elev_helper_config-%d-%s.json" % (os.getpid(), secrets.token_hex(4))
        cfg_path = Path(log_dir) / name
        endpoint_path = Path(log_dir) / ("elev_helper_endpoint-%d.json"
                                         % os.getpid())
        # B2.7A：写本轮 config 前清扫历史会话硬杀孤儿（正常退出双侧已删，此处
        # 兜底；本轮两个路径经 keep 排除，活跃会话经 pid 存活闸排除）
        _sweep_stale_helper_files(log_dir, keep_paths=(cfg_path, endpoint_path))
        token = secrets.token_hex(32)
        cfg = {
            "schema": _eh.CONFIG_SCHEMA,
            "token": token,
            "parent_pid": os.getpid(),
            "root": root,
            "log_dir": log_dir,
            "game_process": game_process,
        }
        ps_error = ""
        proc = None
        try:
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False),
                                encoding="utf-8")
            proc = self._spawn(cfg_path)
            deadline = time.time() + max(10.0, timeout)
            while time.time() < deadline:
                if proc is not None:
                    code = proc.poll()
                    if code is not None and code != 0 and not endpoint_path.exists():
                        ps_error = "拉起进程已退出（exit %s）" % code
                        break
                data = self._read_endpoint(endpoint_path)
                if data and self._handshake(data, token, root, py_path, log_dir):
                    self._reason = ""
                    return True
                time.sleep(0.3)
            self._reason = ("提权辅助进程未就绪（%sUAC 拒绝/超时/启动失败）"
                            % (ps_error + "；" if ps_error else ""))
            return False
        finally:
            try:
                if cfg_path.exists():
                    cfg_path.unlink()  # 辅助进程读毕即删；失败路径兜底清理
            except OSError:
                pass

    def _read_endpoint(self, endpoint_path):
        """轮询读取端点文件（pid+port+fp；非机密）。坏文件忽略等下一轮。"""
        try:
            data = json.loads(endpoint_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        pid, port, fp = data.get("pid"), data.get("port"), data.get("root_fp")
        if not isinstance(pid, int) or pid <= 0 \
                or not isinstance(port, int) or not 1 <= port <= 65535 \
                or not re.fullmatch(r"[0-9a-f]{8,64}", str(fp or "")):
            return None
        return {"pid": pid, "port": port, "root_fp": str(fp)}

    def _spawn(self, cfg_path):
        """拉起辅助进程。生产：PowerShell Start-Process -Verb RunAs（UAC）；
        测试（set_test_spawn）：普通 Popen。argv 传 --config <目录> <文件名>
        （helper 侧目录锚定 + 文件名白名单正则，构造上防目录逃逸）。
        返回 Popen 或 None。"""
        cfg_str = str(cfg_path)
        anchor_dir = os.path.dirname(cfg_str)
        cfg_name = os.path.basename(cfg_str)
        frozen = getattr(sys, "frozen", False)
        if frozen:
            exe = sys.executable
            args = "--oknte-elev-helper --config \"%s\" %s" % (anchor_dir, cfg_name)
            cwd = os.path.dirname(sys.executable)
        else:
            exe = sys.executable
            pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            if os.path.isfile(pyw):
                exe = pyw
            args = "-m %s --config \"%s\" %s" % (HELPER_MODULE, anchor_dir, cfg_name)
            cwd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _TEST_SPAWN:
            # 测试专用：跳过 UAC 直接拉起（仅 selftest/smoke 置位）。打包态入口是
            # 本 EXE（bootloader 把参数透传给 main.py 的入口分流），须用
            # --oknte-elev-helper 而非 -m。
            if frozen:
                argv = [sys.executable, "--oknte-elev-helper",
                        "--config", anchor_dir, cfg_name]
            else:
                argv = [sys.executable, "-m", HELPER_MODULE,
                        "--config", anchor_dir, cfg_name]
            return subprocess.Popen(
                argv, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW)
        ps = _UAC_PS.format(exe=exe, args=args, cwd=cwd)
        try:
            return subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", ps],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW)
        except OSError as e:
            self._reason = "PowerShell 拉起失败: %s" % e
            return None

    def _handshake(self, data, token, root, py_path, log_dir):
        """端点握手：ping + 指纹核对（fp 不一致按会话失效处理）。
        ELEV-01：端点文件可能陈旧（helper 被强杀残留，内容仍指向死端口）——
        连接失败（含 OSError 拒绝/超时）按「未就绪」返回 False 继续轮询，等新
        helper 重写端点后再握手；不外抛打断 _start 重拉链。"""
        try:
            resp = request_once(data["port"], token, "ping", timeout=8.0)
        except (ElevError, OSError):
            return False
        if str(resp.get("root_fp") or "") != file_sha256(py_path)[:16]:
            return False
        self._token = token
        self._port = int(data["port"])
        self._pid = int(data["pid"])
        self._root = root
        self._root_fp = file_sha256(py_path)
        self._log_dir = log_dir
        return True

    def shutdown(self):
        """随主面板退出清理：shutdown 操作 → 等退出；绝不留孤儿提权进程。"""
        with self._lock:
            if self._port is None:
                return
            try:
                self._call("shutdown", {}, timeout=6.0)
            except ElevError:
                pass
            deadline = time.time() + 5.0
            while time.time() < deadline and _pid_alive(self._pid):
                time.sleep(0.2)
            self._token = self._port = self._pid = None
            self._root = self._root_fp = self._log_dir = None

    # ---- 白名单操作 ----
    def _call(self, op, args, timeout=15.0):
        with self._io_lock:
            if self._port is None:
                raise ElevError("提权辅助进程不可用: " + (self._reason or "未启动"))
            try:
                return request_once(self._port, self._token, op, args, timeout)
            except ElevRejected:
                raise  # 操作被拒但会话健康：绝不清会话
            except (ElevError, OSError) as e:
                # 会话失效（helper 退出/端口失效）：清会话，下次 ensure 重新拉起
                self._invalidate()
                raise ElevError(str(e)) from None

    def _invalidate(self):
        self._token = self._port = self._pid = None
        self._root = self._root_fp = self._log_dir = None

    def foreground(self, hwnd):
        """把绑定窗口调前台（尽力而为，失败返回 False 不抛）。"""
        try:
            resp = self._call("set_foreground", {"hwnd": int(hwnd)}, timeout=10.0)
            return bool(resp.get("foreground"))
        except (ElevError, OSError, TypeError, ValueError):
            return False

    def click(self, hwnd, x, y):
        """向已绑定游戏窗口发送一次左键点击（SetCursorPos+LEFTDOWN/UP）。"""
        resp = self._call("click", {"hwnd": int(hwnd), "x": int(x), "y": int(y)},
                          timeout=10.0)
        return bool(resp.get("ok"))

    def scroll(self, hwnd, x, y, notches, wheel_delta):
        """向已绑定游戏窗口发送滚轮滚动（notches 格 × wheel_delta）。"""
        resp = self._call("scroll", {"hwnd": int(hwnd), "x": int(x), "y": int(y),
                                     "notches": int(notches),
                                     "wheel_delta": int(wheel_delta)},
                          timeout=15.0)
        return bool(resp.get("ok"))

    def launch_runner(self, root, launcher, task, profile, stdout_name,
                      expected_launcher_sha256=None):
        """提权拉起 ok-nte 无头任务。客户端侧先做一遍与辅助进程同口径的参数
        校验（纵深防御 + 不必为明显非法参数弹 UAC 往返）。返回子进程 pid。

        SEC-11（B3-B）：launcher 内容 SHA-256 契约——expected_launcher_sha256
        缺省取写方常量 oknte_adapter.LAUNCHER_SHA256（prepare_daily_profile/
        prepare_hunter_profile 写盘后自校验同源），随 args 下发
        （expected_launcher_sha256，与 helper 侧 args 校验面字段名一致），
        辅助进程执行前复核盘面一致才执行——写方期望值绑定执行对象。"""
        root = os.path.realpath(str(root or ""))
        if self._root != root:
            raise ElevError("oknte_root 与辅助进程会话不一致（先 ensure）")
        launcher = os.path.realpath(str(launcher or ""))
        if not _eh.launcher_allowed(launcher, root):
            raise ElevError("launcher 不在白名单路径内")
        profile = os.path.realpath(str(profile or ""))
        if not _eh.profile_allowed(profile, root):
            raise ElevError("profile 目录不在白名单内")
        if not _eh.task_allowed(str(task or "")):
            raise ElevError("task 不在白名单内")
        if not _eh.safe_stdout_name(str(stdout_name or "")):
            raise ElevError("stdout 文件名不在白名单内")
        # 动态属性访问（而非 import 快照）：每次调用读写方常量，留测试注入点
        expected_sha = (expected_launcher_sha256
                        if isinstance(expected_launcher_sha256, str)
                        and expected_launcher_sha256 else _oa.LAUNCHER_SHA256)
        resp = self._call("launch_runner",
                          {"launcher": launcher, "profile": profile,
                           "task": str(task), "stdout": str(stdout_name),
                           "expected_launcher_sha256": expected_sha},
                          timeout=25.0)
        pid = resp.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            raise ElevError("辅助进程未返回有效 pid")
        return pid

    def stop_runner(self, pid, timeout=45.0):
        """经辅助进程停止本会话拉起的任务（中 IL 面板 taskkill 打不开高 IL 进程）。"""
        resp = self._call("stop_runner", {"pid": int(pid)}, timeout=timeout)
        return bool(resp.get("ok"))


ELEV = ElevClient()
