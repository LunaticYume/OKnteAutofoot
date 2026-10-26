"""阶段7：把 Web 面板封装为 EXE 分发（启动器方案，纯本机依赖）。

产物：dist/OKntePlanner/
    异环养成规划器.exe   csc 编译的启动器（requireAdministrator，控制台保留日志）
    app/ data/           面板源码 + 数据表模板（不含用户 state.json/backups/runtime）
    使用说明.txt
可选档：PyInstaller 单文件 EXE（dist/OKntePlanner_single/OKntePlanner.exe），
       联网安装失败不阻断（"尽量封装"边界见 docs/results/EXE-PACKAGING-Result.md）。

启动器定位 Python 的顺序：
    1) <exe目录>/python/python.exe   —— 用户自行放入完整 Python 即可脱依赖
    2) data/state.json 的 oknte_root 指向的 ok-nte 内嵌 Python
    3) D:\\GamingAssistant\\ok-nte 默认安装位置
找到后以 `python -m app.main`（cwd=exe 目录）拉起面板。

运行方式（项目根，任一 Python 3.10+）：
    python tools/build_exe.py
验证：dist 内跑 app.selftest / app.smoke_test（真实退出码），并检查 EXE 的 PE 头与清单。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist", "OKntePlanner")
DIST_SINGLE = os.path.join(ROOT, "dist", "OKntePlanner_single")
EMBEDDED_DEFAULT = r"D:\GamingAssistant\ok-nte\data\apps\ok-nte\python\python.exe"
TABLE_FILES = ("materials.json", "yields.json", "requirements.json", "game_data.json")

LAUNCHER_CS = r"""using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;

// 异环养成规划器启动器（winexe：无控制台黑框）。
// 职责：定位 Python → 用 pythonw 隐藏拉起面板（面板自己打开 Edge 应用模式桌面窗口）→
// 等面板退出。致命错误用 MessageBox 弹窗 + launcher.log，绝不无声消失。
class Launcher {
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    static extern int MessageBoxW(IntPtr hWnd, string text, string caption, uint type);

    static string BaseDir;
    static string LogPath;

    static void Log(string line) {
        try {
            File.AppendAllText(LogPath,
                DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + line + Environment.NewLine,
                Encoding.UTF8);
        } catch { }
    }

    static void Alert(string text) {
        Log("失败：" + text.Replace("\r\n", " | "));
        MessageBoxW(IntPtr.Zero, text, "异环养成规划器", 0x10);
    }

    static string FindPython() {
        string local = Path.Combine(BaseDir, "python", "python.exe");
        if (File.Exists(local)) return local;
        try {
            string state = Path.Combine(BaseDir, "data", "state.json");
            if (File.Exists(state)) {
                Match m = Regex.Match(File.ReadAllText(state),
                    "\"oknte_root\"\\s*:\\s*\"([^\"]+)\"");
                if (m.Success) {
                    string emb = Path.Combine(m.Groups[1].Value.Replace("\\\\", "\\"),
                        "data", "apps", "ok-nte", "python", "python.exe");
                    if (File.Exists(emb)) return emb;
                }
            }
        } catch { }
        string fallback = @"D:\GamingAssistant\ok-nte\data\apps\ok-nte\python\python.exe";
        if (File.Exists(fallback)) return fallback;
        try {
            var psi = new ProcessStartInfo("cmd.exe", "/c where python 2>nul") {
                UseShellExecute = false, RedirectStandardOutput = true, CreateNoWindow = true };
            using (var p = Process.Start(psi)) {
                string output = p.StandardOutput.ReadToEnd();
                try { p.WaitForExit(5000); } catch { }
                foreach (string line in output.Split('\n')) {
                    string cand = line.Trim();
                    if (cand.EndsWith("python.exe", StringComparison.OrdinalIgnoreCase)
                            && File.Exists(cand)) return cand;
                }
            }
        } catch { }
        return null;
    }

    static int Run() {
        string py = FindPython();
        if (py == null) {
            Alert("未找到可用的 Python。请任选其一：\r\n"
                + "  1) 把完整 Python 3.10+ 目录放到本目录的 python\\ 子目录；\r\n"
                + "  2) 安装 ok-nte（本面板默认复用其内嵌 Python）；\r\n"
                + "  3) 把 python.exe 所在目录加入系统 PATH。");
            return 1;
        }
        // 无控制台：优先 pythonw（面板输出镜像到 data\panel.log）
        string pythonDir = Path.GetDirectoryName(py);
        string pyw = Path.Combine(pythonDir, "pythonw.exe");
        if (File.Exists(pyw)) py = pyw;
        Log("使用 Python: " + py);
        var psi = new ProcessStartInfo {
            FileName = py,
            Arguments = "-m app.main",
            WorkingDirectory = BaseDir,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        psi.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";
        using (var p = Process.Start(psi)) {
            p.WaitForExit();  // 面板退出（用户关闭 Edge 桌面窗口）即收尾
            Log("面板退出，退出码 " + p.ExitCode);
            if (p.ExitCode != 0) {
                Alert("面板异常退出（退出码 " + p.ExitCode + "）。\r\n"
                    + "详情见本目录 launcher.log 与 data\\panel.log / crash.log。");
            }
            return p.ExitCode;
        }
    }

    static int Main() {
        BaseDir = AppDomain.CurrentDomain.BaseDirectory;
        LogPath = Path.Combine(BaseDir, "launcher.log");
        Log("=== 启动器启动，目录: " + BaseDir);
        try {
            return Run();
        } catch (Exception e) {
            Log("启动器异常: " + e);
            Alert("启动器异常: " + e.Message);
            return 1;
        }
    }
}
"""

APP_MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<assembly manifestVersion="1.0" xmlns="urn:schemas-microsoft-com:asm.v1">
  <assemblyIdentity version="1.0.0.0" name="OKntePlanner" type="win32"/>
  <description>异环养成规划器面板启动器</description>
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v2">
    <security>
      <requestedPrivileges xmlns="urn:schemas-microsoft-com:asm.v3">
        <requestedExecutionLevel level="requireAdministrator" uiAccess="false"/>
      </requestedPrivileges>
    </security>
  </trustInfo>
</assembly>
"""

USAGE_TXT = """异环养成规划器（OKnteAutofoot）使用说明
==========================================

【推荐】独立桌面版：OKntePlanner_single\\OKntePlanner.exe
  双击 → UAC 点"是" → 直接弹出一个独立的应用窗口（自己的进程与任务栏项，
  无黑框、无浏览器、无 Edge 壳）。关闭窗口即整套退出（正在执行的任务会先
  安全停止并记账）。

备用：异环养成规划器.exe（启动器版）
  复用 ok-nte 内嵌 Python，以 Edge 应用窗口打开面板（本机无独立 Python 时用；
  已强制直连，不会因系统代理出现"拒绝连接"）。

首次使用：
  - 「角色」页建角色填目标；「库存」页按背包实际填一次库存；
  - 「数据表」页校准"每轮产出"（默认是估计值，务必校准）；
  - 面板里先点「预演（不落盘）」确认无误，再「开始执行」。

数据与日志：
  - 用户数据都在 data\\state.json（可随时备份/迁移）；dist 不含任何历史数据。
  - 排查日志：launcher.log（启动器）、data\\panel.log（面板）、crash.log（异常）。
  - 端口被占会自动避让并在 panel.log 提示新端口；双击无反应先看上述日志。

详细文档见源码仓库 docs\\ 目录（上游兼容清单、各阶段验收报告）。
"""


def find_csc():
    candidates = [shutil.which("csc")]
    for frame in ("Framework64", "Framework"):
        candidates.append(os.path.join(
            r"C:\Windows\Microsoft.NET", frame, "v4.0.30319", "csc.exe"))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def embedded_python():
    override = os.environ.get("OKNTE_PY", "").strip()
    if override:
        return override
    try:  # 优先用本机 state.json 记录的 oknte_root 定位内嵌 Python
        with open(os.path.join(ROOT, "data", "state.json"), encoding="utf-8") as f:
            root = json.load(f)["settings"]["oknte_root"]
        cand = os.path.join(root, "data", "apps", "ok-nte", "python", "python.exe")
        if os.path.isfile(cand):
            return cand
    except (OSError, ValueError, KeyError):
        pass
    return EMBEDDED_DEFAULT


def copy_tree_clean(src, dst):
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def build_dist():
    print("== 组装 dist/OKntePlanner ==")
    if os.path.isdir(DIST):
        shutil.rmtree(DIST)
    os.makedirs(os.path.join(DIST, "data"))
    copy_tree_clean(os.path.join(ROOT, "app"), os.path.join(DIST, "app"))
    copy_tree_clean(os.path.join(ROOT, "tests"), os.path.join(DIST, "tests"))
    for name in TABLE_FILES:
        shutil.copy2(os.path.join(ROOT, "data", name),
                     os.path.join(DIST, "data", name))
    with open(os.path.join(DIST, "使用说明.txt"), "w", encoding="utf-8") as f:
        f.write(USAGE_TXT)
    if os.path.isfile(os.path.join(ROOT, "README.md")):
        shutil.copy2(os.path.join(ROOT, "README.md"), DIST)

    csc = find_csc()
    if not csc:
        raise RuntimeError("未找到 csc.exe（.NET Framework 编译器）；"
                           "Windows 10/11 自带，请检查 C:\\Windows\\Microsoft.NET")
    with tempfile.TemporaryDirectory(prefix="oknte_build_") as tmp:
        cs = os.path.join(tmp, "Launcher.cs")
        manifest = os.path.join(tmp, "app.manifest")
        with open(cs, "w", encoding="utf-8-sig") as f:  # BOM：csc 才能按 UTF-8 读中文
            f.write(LAUNCHER_CS)
        with open(manifest, "w", encoding="utf-8") as f:
            f.write(APP_MANIFEST)
        exe = os.path.join(DIST, "异环养成规划器.exe")
        result = subprocess.run(
            [csc, "/nologo", "/codepage:65001", "/target:winexe",
             f"/win32manifest:{manifest}", f"/out:{exe}", cs],
            capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"csc 编译失败:\n{result.stdout}\n{result.stderr}")
        print(f"启动器 EXE 编译完成: {exe}")
    return exe


def verify_dist(py):
    print("== dist 验证 ==")
    checks = []
    exe = os.path.join(DIST, "异环养成规划器.exe")
    with open(exe, "rb") as f:
        head = f.read(2)
    assert head == b"MZ", f"EXE 不是合法 PE 文件: {head!r}"
    checks.append("EXE PE 头 (MZ) ✅")
    with open(exe, "rb") as f:
        blob = f.read()
    if b"requireAdministrator" in blob:
        checks.append("清单含 requireAdministrator ✅")
    else:
        checks.append("清单检查：未在资源段直接匹配到 requireAdministrator（以 /win32manifest 参数为准，人工复核）")

    pristine = set()
    data_dir = os.path.join(DIST, "data")
    for base, _dirs, files in os.walk(DIST):
        for name in files:
            pristine.add(os.path.join(base, name))
    try:
        for step, cmd, env_extra in (
            ("自测", [py, "-m", "app.selftest"], {}),
            ("冒烟", [py, "-m", "app.smoke_test"],
             {"OKNTE_PLANNER_PORT": "18770", "OKNTE_PLANNER_NO_BROWSER": "1"}),
        ):
            env = dict(os.environ)
            env.setdefault("PYTHONIOENCODING", "utf-8")
            env.update(env_extra)
            r = subprocess.run(cmd, cwd=DIST, env=env, capture_output=True,
                               text=True, timeout=600)
            tail = (r.stdout or r.stderr or "").strip().splitlines()[-1:]
            print(f"dist {step}: exit={r.returncode} {tail[0] if tail else ''}")
            assert r.returncode == 0, f"dist {step} 未通过（exit {r.returncode}）"
            checks.append(f"dist {step} 通过 ✅")
    finally:  # 验证过程新生成的用户数据文件清掉，保持 dist 模板干净
        for base, _dirs, files in os.walk(DIST):
            for name in files:
                path = os.path.join(base, name)
                if path not in pristine:
                    os.remove(path)
        shutil.rmtree(os.path.join(data_dir, "backups"), ignore_errors=True)
    return checks


def _ensure_pyi_deps(py, packages):
    """把额外依赖装进隔离目录 tools/_build/pyi（绝不碰 ok-nte 的 Python）。返回是否成功。"""
    os.makedirs(PYI_DEPS, exist_ok=True)
    r = subprocess.run(
        [py, "-m", "pip", "install", "--quiet", "--target", PYI_DEPS,
         "--index-url", "https://mirrors.cloud.tencent.com/pypi/simple/",
         *packages],
        capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        print(f"依赖安装失败（不阻断，运行时将自动降级）:\n"
              f"{r.stdout[-400:]}\n{r.stderr[-400:]}")
        return False
    return True


def try_pyinstaller_single(py):
    print("== 可选档：PyInstaller 单文件 EXE（失败不阻断）==")
    deps = PYI_DEPS
    work = os.path.join(ROOT, "tools", "_build", "work")
    try:
        probe = subprocess.run([py, "-m", "pip", "--version"],
                               capture_output=True, timeout=60)
        if probe.returncode != 0:
            print("跳过：该 Python 无可用 pip")
            return None
        if not _ensure_pyi_deps(py, ["pyinstaller"]):
            print("跳过：pyinstaller 安装失败")
            return None
        # pywebview：原生窗口（自己的 WinForms 窗口与进程）；失败仅降级 Edge 壳
        global HAS_PYWEBVIEW
        HAS_PYWEBVIEW = _ensure_pyi_deps(py, ["pywebview"])
        os.makedirs(DIST_SINGLE, exist_ok=True)
        env = dict(os.environ)
        env["PYTHONPATH"] = deps
        collect = ["--collect-all", "webview", "--collect-all", "pythonnet"] \
            if HAS_PYWEBVIEW else []
        r = subprocess.run(
            [py, "-m", "PyInstaller", "--noconfirm", "--onefile", "--noconsole",
             "--uac-admin", *collect,
             "--add-data",
             os.path.join(ROOT, "app", "web", "index.html") + os.pathsep + os.path.join("app", "web"),
             "--name", "OKntePlanner", "--distpath", DIST_SINGLE,
             "--workpath", work, "--specpath", os.path.dirname(work) or ".",
             os.path.join("app", "main.py")],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
        exe = os.path.join(DIST_SINGLE, "OKntePlanner.exe")
        if r.returncode != 0 or not os.path.isfile(exe):
            print(f"跳过：PyInstaller 构建失败\n{r.stdout[-600:]}\n{r.stderr[-600:]}")
            return None
        size_mb = os.path.getsize(exe) / 1024 / 1024
        print(f"单文件 EXE 构建完成: {exe} ({size_mb:.1f} MB)")
        # 冻结模式数据目录在 EXE 旁：数据表模板必须随行（否则首启 import planner 失败）
        os.makedirs(os.path.join(DIST_SINGLE, "data"), exist_ok=True)
        for name in TABLE_FILES:
            shutil.copy2(os.path.join(ROOT, "data", name),
                         os.path.join(DIST_SINGLE, "data", name))
        with open(os.path.join(DIST_SINGLE, "使用说明.txt"), "w", encoding="utf-8") as f:
            f.write(USAGE_TXT)
        # 探针（无 admin 标志）：验证冻结环境下数据目录落在 EXE 旁而非 _MEIPASS 临时目录
        probe_dir = os.path.join(ROOT, "tools", "_build", "probe")
        os.makedirs(probe_dir, exist_ok=True)
        probe_py = os.path.join(probe_dir, "probe.py")
        with open(probe_py, "w", encoding="utf-8") as f:
            f.write("from app.state import DATA_DIR\nprint(DATA_DIR)\n")
        r2 = subprocess.run(
            [py, "-m", "PyInstaller", "--noconfirm", "--onefile",
             "--name", "probe", "--distpath", probe_dir,
             "--workpath", work, "--specpath", probe_dir, probe_py],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
        assert r2.returncode == 0, f"探针构建失败:\n{r2.stderr[-400:]}"
        r3 = subprocess.run([os.path.join(probe_dir, "probe.exe")],
                            capture_output=True, text=True, timeout=60)
        data_dir = (r3.stdout or "").strip().splitlines()[-1] if r3.stdout.strip() else ""
        assert data_dir.lower() == os.path.join(probe_dir, "data").lower(), \
            f"冻结环境数据目录应在 EXE 旁，实际 {data_dir!r}"
        print(f"冻结环境数据目录探针通过 ✅（{data_dir}）")
        return exe
    except Exception as e:  # noqa: BLE001 —— 可选档任何失败都不阻断主产物
        print(f"跳过：可选档异常 {type(e).__name__}: {e}")
        return None


PYI_DEPS = os.path.join(ROOT, "tools", "_build", "pyi")
HAS_PYWEBVIEW = False  # 由 try_pyinstaller_single 设置（原生窗口依赖是否打进包）


def _launch_and_probe(exe_path, port, ui_env, timeout=90):
    """真实拉起一个 EXE：轮询 /api/bootstrap 与首页，随后杀树并清理新产生的文件。
    ui_env 控制 UI 策略（如 {"OKNTE_PLANNER_UI": "edge"} / {}=frozen 默认原生窗口）。
    失败时保留并返回子进程控制台输出尾部，供构建摘要定位根因。"""
    import time as _time
    import urllib.request
    exe_dir = os.path.dirname(exe_path)
    pristine = {os.path.join(base, n)
                for base, _dirs, files in os.walk(exe_dir) for n in files}
    out_path = os.path.join(exe_dir, "_probe_out.log")
    out_file = open(out_path, "wb")
    proc = subprocess.Popen(
        [exe_path], cwd=exe_dir, env={**os.environ,
                                      "OKNTE_PLANNER_PORT": str(port),
                                      **ui_env},
        stdout=out_file, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    # 强制直连：系统代理/TUN 可能截获 127.0.0.1 请求导致探测假超时。
    # 单次请求超时必须 > /api/bootstrap 的响应耗时（内含 tasklist+PowerShell 进程检测，2~4 秒）。
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    _time.sleep(4)  # 先给启动链（EXE→python→面板）留出时间，再做轮询
    api_ok, page_ok, detail = False, False, ""
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        try:
            with direct.open(f"http://127.0.0.1:{port}/api/bootstrap", timeout=15) as resp:
                if resp.status == 200:
                    api_ok = True
                    break
        except OSError as e:
            detail = f"{type(e).__name__}: {e}"
            _time.sleep(1)
    if api_ok:
        try:
            with direct.open(f"http://127.0.0.1:{port}/", timeout=5) as resp:
                page_ok = resp.status == 200 and "异环养成规划器".encode("utf-8") in resp.read()
        except OSError as e:
            detail = f"首页获取失败: {e}"
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                   capture_output=True, timeout=30)
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        pass
    _time.sleep(1)
    out_file.close()
    console_tail = ""
    try:
        console_tail = open(out_path, "rb").read().decode("utf-8", "replace").strip()[-500:]
    except OSError:
        pass
    if not api_ok:  # 失败先保留现场（含日志），再清理
        keep = os.path.join(ROOT, "tools", "_build", "probe_failure")
        shutil.rmtree(keep, ignore_errors=True)
        shutil.copytree(exe_dir, keep)
        detail += f"（现场已保留: {keep}）"
    for base, dirs, files in os.walk(exe_dir, topdown=False):  # 清理实跑产生的数据/日志/空目录
        for name in files:
            path = os.path.join(base, name)
            if path not in pristine:
                os.remove(path)
        for name in dirs:
            path = os.path.join(base, name)
            if path not in pristine and not os.listdir(path):
                os.rmdir(path)
    return api_ok, page_ok, detail, console_tail


def _compile_test_launcher(out_exe):
    """编译无管理员清单的启动器测试变体（供非管理员 shell 做真实拉起验证）。"""
    csc = find_csc()
    if not csc:
        return None
    with tempfile.TemporaryDirectory(prefix="oknte_livecs_") as tmp:
        cs = os.path.join(tmp, "Launcher.cs")
        with open(cs, "w", encoding="utf-8-sig") as f:
            f.write(LAUNCHER_CS)
        result = subprocess.run(
            [csc, "/nologo", "/codepage:65001", "/target:winexe",
             f"/out:{out_exe}", cs], capture_output=True, text=True, timeout=120)
        return out_exe if result.returncode == 0 else None


def _build_test_single(py, dist_dir, has_webview):
    """构建无管理员清单的单文件测试变体（与生产同参数）；依赖不可用时返回 None。"""
    if not os.path.isdir(PYI_DEPS):
        return None
    work = os.path.join(ROOT, "tools", "_build", "work_test")
    spec_dir = os.path.join(ROOT, "tools", "_build", "spec_test")
    os.makedirs(spec_dir, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = PYI_DEPS
    collect = ["--collect-all", "webview", "--collect-all", "pythonnet"] \
        if has_webview else []
    r = subprocess.run(
        [py, "-m", "PyInstaller", "--noconfirm", "--onefile", "--noconsole",
         *collect,
         "--add-data",
         os.path.join(ROOT, "app", "web", "index.html") + os.pathsep + os.path.join("app", "web"),
         "--name", "OKntePlannerTest", "--distpath", dist_dir,
         "--workpath", work, "--specpath", spec_dir,
         os.path.join(ROOT, "app", "main.py")],
        env=env, capture_output=True, text=True, timeout=900)
    exe = os.path.join(dist_dir, "OKntePlannerTest.exe")
    return exe if r.returncode == 0 and os.path.isfile(exe) else None


def _probe_auto_avoid(py):
    """复刻用户真实场景：不带端口 env 启动面板——若 8765 被占（如输入法），
    应自动避让到相邻端口并在该端口就绪。"""
    import shutil as _shutil
    import time as _time
    import urllib.request
    with tempfile.TemporaryDirectory(prefix="oknte_autoport_") as tmp:
        d = os.path.join(tmp, "app_root")
        _shutil.copytree(DIST, d, ignore=_shutil.ignore_patterns("__pycache__"))
        env = {**os.environ, "OKNTE_PLANNER_NO_BROWSER": "1"}
        proc = subprocess.Popen([py, "-m", "app.main"], cwd=d, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL)
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        found = None
        deadline = _time.time() + 90
        while _time.time() < deadline and found is None:
            for port in range(8765, 8776):
                try:
                    # 单次超时 10s：bootstrap 含进程检测，响应可能需要数秒
                    with direct.open(f"http://127.0.0.1:{port}/api/bootstrap", timeout=10) as r:
                        if r.status == 200:
                            found = port
                            break
                except OSError:
                    continue
            if found is None:
                _time.sleep(1)
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
        if found:
            note = "8765 被占用，已自动避让" if found != 8765 else "8765 空闲，直接绑定"
            return f"端口冲突自动避让：✅ 面板起在 {found}（{note}）"
        return "端口冲突自动避让：✗ 90 秒内未在任何候选端口(8765-8775)就绪"


def verify_exes_live(py):
    """真实拉起验证。管理员 → 直接实跑正式双 EXE（含 UAC 层）；
    非管理员 → 自动构建 asInvoker 测试变体实跑（验证 EXE 逻辑本身，UAC 层列 Manual）。"""
    import ctypes
    try:
        admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        admin = False
    results = []

    def probe(label, exe, port, ui_env):
        api_ok, page_ok, detail, console = _launch_and_probe(exe, port, ui_env)
        if api_ok and page_ok:
            results.append(f"{label} 实跑：API+首页 ✅")
        elif api_ok:
            results.append(f"{label} 实跑：API ✅ 首页 ✗ {detail}")
        else:
            results.append(f"{label} 实跑：✗ 未就绪（{detail}）")
            if console:
                print(f"     [{label} 控制台尾部] {console}")

    # 生产启动器无 pywebview（面板走源码路径，非 frozen）→ Edge 壳；显式声明保证确定性
    launcher_ui = {"OKNTE_PLANNER_UI": "edge"}
    if admin:
        for label, exe, port, ui_env in (
            ("启动器 EXE", os.path.join(DIST, "异环养成规划器.exe"), 18771, launcher_ui),
            ("单文件 EXE(原生窗口)", os.path.join(DIST_SINGLE, "OKntePlanner.exe"), 18772, {}),
        ):
            if os.path.isfile(exe):
                probe(label, exe, port, ui_env)
            else:
                results.append(f"{label} 实跑：跳过（未构建）")
        return results

    print("  ⚠️ 当前构建 shell 非管理员：改用 asInvoker 测试变体真实拉起（UAC 层留 Manual）")
    with tempfile.TemporaryDirectory(prefix="oknte_live_") as tmp:
        # 启动器测试变体：复制 dist + 无清单编译
        test_dist = os.path.join(tmp, "launcher_variant")
        shutil.copytree(DIST, test_dist, ignore=shutil.ignore_patterns("__pycache__"))
        variant = _compile_test_launcher(os.path.join(test_dist, "启动器_测试.exe"))
        if variant:
            probe("启动器(asInvoker 测试变体)", variant, 18771, launcher_ui)
        else:
            results.append("启动器测试变体：编译失败")
        # 单文件测试变体（frozen 默认 = 原生窗口进程）
        single_dir = os.path.join(tmp, "single_variant")
        os.makedirs(single_dir, exist_ok=True)
        os.makedirs(os.path.join(single_dir, "data"), exist_ok=True)
        for name in TABLE_FILES:
            shutil.copy2(os.path.join(ROOT, "data", name),
                         os.path.join(single_dir, "data", name))
        single = _build_test_single(py, single_dir, HAS_PYWEBVIEW)
        if single:
            probe("单文件(asInvoker 测试变体·原生窗口)", single, 18772, {})
        else:
            results.append("单文件测试变体：未构建（PyInstaller 依赖不可用）")
    results.append("正式双 EXE（含 UAC 层）：Manual——请双击验证 UAC 允许后面板打开")
    results.append(_probe_auto_avoid(py))
    return results


def main():
    py = embedded_python()
    print(f"验证用 Python: {py}")
    build_dist()
    checks = verify_dist(py)
    single = try_pyinstaller_single(py)
    checks += verify_exes_live(py)
    print("\n== 构建摘要 ==")
    for line in checks:
        print(" ", line)
    print(f"  主产物: {DIST}")
    if single:
        print(f"  可选单文件: {single}")
    else:
        print("  可选单文件: 未构建（不影响主产物）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
