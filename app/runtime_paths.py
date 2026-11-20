"""运行缓存根解析（W4b-R3 用户裁定，2026-11-21）。

用途：打包态的「运行缓存」（panel.log / ok-nte-live.log / ok-nte-headless-*.log /
crash.log / webview 缓存与 edge_profile / elev_helper 审计与 stdout 镜像）统一落
E:\\OKntePlannerRuntime，运行缓存迁出 C 盘；「用户数据」（state.json/凭据/config/
endpoint 等）保持 %LOCALAPPDATA%\\OKntePlanner\\data 不动——用户数据根不动原则，
本模块绝不参与用户数据路径解析，resolve("logs") 不得用于 config/endpoint。

- 根常量：E:/OKntePlannerRuntime；环境变量 OKNTE_RUNTIME_ROOT 覆盖（测试与部署
  灵活性）。
- 打包态判定自含（复用 app/state.py:14-19 同口径，不 import app.state 防循环）：
  getattr(sys, "frozen", False) 或 os.environ.get("OKNTE_PLANNER_USER_DATA") == "1"。
  **仅打包态生效**；源码态一律返回回退路径（开发/测试环境不漂移，保持 selftest
  既有 DATA_DIR 假设），不做 mkdir、不做可写探针。
- API：resolve(kind) -> (path, degraded)，kind ∈ {"logs", "webview", "crash"}。
  打包态内部 mkdir(parents) + 可写探针（写删临时文件）；成功 → 根下对应子目录
  （logs/webview/crash）；失败（OSError）→ degraded=True → 回退既有路径
  （logs/webview→DATA_DIR 现状语义、crash→ROOT 现状）。resolve 结果进程内缓存
  （键含全部输入，环境变量变化自然失效）；degrade_notice(kind) 提供每进程每
  kind 一次的降级告警文本（模块级去重），由调用方 print 一行。
- _MEI 例外：onefile bootloader 的解包目录发生在任何 Python 代码运行之前，
  本模块无法兜底——由 tools/build_exe.py 单文件档 launcher 壳设 TEMP/TMP 指向
  EXE 旁 runtime_tmp 实现（--runtime-tmpdir 相对值按 CWD 解析不可靠，
  .tmp/b3-w4br3-probe 产物为探针实测证据，见 build_exe.LAUNCHER_SINGLE_CS）。
- 纯标准库实现。
"""
import os
import sys
import tempfile
import threading

RUNTIME_ROOT_DEFAULT = "E:/OKntePlannerRuntime"
RUNTIME_ROOT_ENV = "OKNTE_RUNTIME_ROOT"
KINDS = ("logs", "webview", "crash")

# 回退基准：logs/webview → DATA_DIR（panel.log/live log/edge_profile 既有位置）、
# crash → ROOT（EXE 旁 crash.log 既有位置）。与 app/state.py:20-35 派生口径一致。
_FALLBACK = {"logs": "data_dir", "webview": "data_dir", "crash": "root"}

_CACHE = {}          # 缓存键 → (path, degraded)
_DEGRADE_INFO = {}   # kind → (回落 path, OSError 原文)
_NOTIFIED = set()    # 已提示过降级的 kind（每进程每 kind 一次）
_LOCK = threading.Lock()


def is_packaged():
    """打包态判定（与 resolve/_context 同口径，自含三行）：frozen 或
    OKNTE_PLANNER_USER_DATA=1。接线点据此区分「生产新根」与「源码态旧行为」
    （如 elev_client 仅打包态经 config 下发运行缓存根，源码态保持 log_dir）。"""
    return bool(getattr(sys, "frozen", False)) \
        or os.environ.get("OKNTE_PLANNER_USER_DATA") == "1"


def _context():
    """一次算清全部输入：(runtime_root, packaged, root, data_dir)。

    root/data_dir 与 app/state.py:20-35 派生口径逐行对齐；runtime_root 经
    OKNTE_RUNTIME_ROOT 覆盖（默认 E:/OKntePlannerRuntime，normpath 归一）。"""
    frozen = bool(getattr(sys, "frozen", False))
    packaged = frozen or os.environ.get("OKNTE_PLANNER_USER_DATA") == "1"
    if frozen:
        root = os.path.dirname(os.path.abspath(sys.executable))
    else:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if packaged:
        data_dir = os.path.join(
            os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local"),
            "OKntePlanner", "data")
    else:
        data_dir = os.path.join(root, "data")
    raw = os.environ.get(RUNTIME_ROOT_ENV, "").strip() or RUNTIME_ROOT_DEFAULT
    return os.path.normpath(raw), packaged, root, data_dir


def resolve(kind):
    """解析 kind 对应的运行缓存目录。返回 (path, degraded)。

    打包态：E:\\OKntePlannerRuntime\\<kind>（或 OKNTE_RUNTIME_ROOT 覆盖根下同名
    子目录），mkdir + 可写探针通过；失败 → degraded=True 回退既有路径。
    源码态：直接返回既有路径（logs/webview→DATA_DIR、crash→ROOT），degraded=False，
    不做任何 mkdir/写盘（开发/测试环境零副作用）。"""
    if kind not in KINDS:
        raise ValueError("未知运行缓存类别: %r（应为 %s）" % (kind, "/".join(KINDS)))
    runtime_root, packaged, root, data_dir = _context()
    key = (kind, runtime_root, packaged, root, data_dir)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            return hit
        path, degraded, err = _resolve_uncached(
            kind, runtime_root, packaged, root, data_dir)
        _CACHE[key] = (path, degraded)
        if degraded:
            _DEGRADE_INFO[kind] = (path, err)
        return path, degraded


def _resolve_uncached(kind, runtime_root, packaged, root, data_dir):
    if not packaged:
        fallback = root if _FALLBACK[kind] == "root" else data_dir
        return fallback, False, ""
    # kind ∈ KINDS 白名单（纯字母子目录名，无分隔符/../），join 结果构造上
    # 不可能越出 runtime_root（归属语义与仓库 _contained 同口径）。
    target = os.path.join(runtime_root, kind)
    try:
        os.makedirs(target, exist_ok=True)
        # 可写探针：mkstemp 建后即删（无路径字符串拼接；dir= 显式指向目标目录，
        # 与仓库「tempfile 显式 dir=、C 盘零落地」纪律一致）。
        fd, probe = tempfile.mkstemp(prefix="write_probe-", dir=target)
        os.close(fd)
        os.remove(probe)
        return target, False, ""
    except OSError as e:
        fallback = root if _FALLBACK[kind] == "root" else data_dir
        return fallback, True, str(e)


def degrade_notice(kind):
    """降级告警文本（每进程每 kind 只返回一次，模块级去重）；未降级或已提示
    返回 None。调用方 print 一行即可（frozen 无控制台时经 _Tee 落 panel.log）。"""
    path, degraded = resolve(kind)  # 已缓存，无副作用
    if not degraded or kind in _NOTIFIED:
        return None
    _NOTIFIED.add(kind)
    info = _DEGRADE_INFO.get(kind)
    err = info[1] if info else ""
    return ("警告：运行缓存根不可写（%s），%s 运行缓存回落既有位置 %s"
            "（可用环境变量 %s 指定其他位置）" % (err, kind, path, RUNTIME_ROOT_ENV))
