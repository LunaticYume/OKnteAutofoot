"""阶段7：把 Web 面板封装为 EXE 分发（启动器方案，纯本机依赖）。

产物：dist/OKntePlanner/
    异环养成规划器.exe   csc 编译的启动器（SEC-10 方案B：asInvoker 普通权限，
                         提权收敛到受限提权辅助进程 app.elev_helper）
    app/ data/           面板源码 + 数据表模板（不含用户 state.json/backups/runtime）
    使用说明.txt
可选档：PyInstaller 单文件 EXE（dist/OKntePlanner_single/OKntePlanner.exe），
       联网安装失败不阻断（"尽量封装"边界见 docs/results/EXE-PACKAGING-Result.md）。

启动器定位 Python 的顺序：
    1) <exe目录>/python/python.exe   —— 用户自行放入完整 Python 即可脱依赖
    2) <exe目录>/data/state.json（旧布局，升级用户旧文件永存、实为主源）与
       %LOCALAPPDATA%/OKntePlanner/data/state.json（用户数据目录）的 oknte_root
       指向的 ok-nte 内嵌 Python
    3) D:\\GamingAssistant\\ok-nte 默认安装位置
    4) where python
找到后以 `python -m app.main`（cwd=exe 目录）拉起面板，并注入
OKNTE_PLANNER_USER_DATA=1 声明打包态（用户数据统一落 %LOCALAPPDATA%）。
面板用户数据不再落 dist——打包前 check_dist_data_clean 自检阻断含个人数据的
dist（main() 最先调用，先于 build_dist() 的 rmtree）。

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
from datetime import datetime
from io import FileIO
from pathlib import Path

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

    static string ReadOknteRoot(string statePath) {
        // 从 state.json 文本提取 oknte_root（EXE 旁旧布局与用户数据目录两处共用）
        try {
            if (File.Exists(statePath)) {
                Match m = Regex.Match(File.ReadAllText(statePath),
                    "\"oknte_root\"\\s*:\\s*\"([^\"]+)\"");
                if (m.Success) return m.Groups[1].Value.Replace("\\\\", "\\");
            }
        } catch { }
        return null;
    }

    static string PythonFromRoot(string root) {
        if (root == null) return null;
        try {
            string emb = Path.Combine(root, "data", "apps", "ok-nte", "python", "python.exe");
            return File.Exists(emb) ? emb : null;
        } catch { return null; }
    }

    static string FindPython() {
        // 1) 用户自带完整 Python
        string local = Path.Combine(BaseDir, "python", "python.exe");
        if (File.Exists(local)) return local;
        // 2) 旧布局：EXE 旁 data\state.json 的 oknte_root（迁移只复制不删、旧文件永存，实为主源）
        string py = PythonFromRoot(ReadOknteRoot(Path.Combine(BaseDir, "data", "state.json")));
        if (py != null) return py;
        // 3) 新布局：用户数据目录 %LOCALAPPDATA%\OKntePlanner\data\state.json
        string userState = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "OKntePlanner", "data", "state.json");
        py = PythonFromRoot(ReadOknteRoot(userState));
        if (py != null) return py;
        // 4) 默认安装位置回退
        string fallback = @"D:\GamingAssistant\ok-nte\data\apps\ok-nte\python\python.exe";
        if (File.Exists(fallback)) return fallback;
        // 5) where python
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
        // 打包态声明：state.py 读 OKNTE_PLANNER_USER_DATA=1 → 用户数据统一落
        // %LOCALAPPDATA%\OKntePlanner\data（变量名与 state.py 是两处无编译期检查的
        // 字符串，接线由 build_exe._launch_and_probe 的 .migrated 断言验证）
        psi.EnvironmentVariables["OKNTE_PLANNER_USER_DATA"] = "1";
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
        <!-- SEC-10 方案B（2026-11-14 用户裁决）：面板降为普通权限（asInvoker），
             HTTP/token 服务面不再随面板提权；确需提权的操作由面板经一次 UAC
             同意拉起的受限提权辅助进程（app.elev_helper）完成。 -->
        <requestedExecutionLevel level="asInvoker" uiAccess="false"/>
      </requestedPrivileges>
    </security>
  </trustInfo>
</assembly>
"""

USAGE_TXT = """异环养成规划器（OKnteAutofoot）使用说明
==========================================

【推荐】独立桌面版：OKntePlanner_single\\OKntePlanner.exe
  双击 → 直接弹出一个独立的应用窗口（自己的进程与任务栏项，无黑框、无浏览器、
  无 Edge 壳）。面板以普通权限运行（SEC-10：HTTP/token 服务面不随面板提权）；
  首次「开始执行」或背包 OCR 时会弹一次 UAC——那是受限提权辅助进程（仅用于
  提权拉起 ok-nte 任务与向游戏窗口发送输入，操作白名单封闭、随面板退出）。
  关闭窗口即整套退出（正在执行的任务会先安全停止并记账）。

备用：异环养成规划器.exe（启动器版）
  复用 ok-nte 内嵌 Python，以 Edge 应用窗口打开面板（本机无独立 Python 时用；
  已强制直连，不会因系统代理出现"拒绝连接"）。

首次使用：
  - 「角色」页建角色填目标；「库存」页按背包实际填一次库存；
  - 「数据表」页校准"每轮产出"（默认是估计值，务必校准）；
  - 面板里先点「预演（不落盘）」确认无误，再「开始执行」。

数据与日志：
  - 用户数据统一在 %LOCALAPPDATA%\\OKntePlanner\\data（角色/库存/设置/图鉴
    覆盖层/塔吉多凭证/OCR 标定/panel.log 等；两个 EXE 共用同一份，可整体
    备份/迁移）。【勿同时双开两个 EXE】——并发写会互相覆盖。
  - EXE 旁的 data\\ 仅为出厂数据表模板（materials/yields/requirements 等），
    面板不往里写任何用户数据。
  - 首次启动自动初始化用户数据目录；旧版（数据在 EXE 旁 data\\）首次启动会
    自动整树迁移到用户数据目录，原目录原样保留（只复制不删除）。
  - 【放心】重新打包/更新版本不会动用户数据（用户数据不在 EXE 目录内，
    替换 EXE 目录即可升级）。
  - 手动迁移/合并旧数据：python -m tools.migrate_user_data --from <旧数据
    目录> --apply（可加 --dry-run 先演练；目标已存在的文件一律跳过）。
  - 更新出厂数据表：删除用户数据目录下的 .migrated 与对应表文件后重启面板
    （原子 no-clobber 只补缺失文件，其余用户数据不动）。
  - 排查日志：launcher.log/crash.log 在 EXE 旁；panel.log 在用户数据目录。
    端口被占会自动避让并在 panel.log 提示新端口；双击无反应先看上述日志。
  - 启动器找 Python 优先读 EXE 旁 data\\state.json（旧布局）的 oknte_root，
    再读用户数据目录的 state.json——面板内改 oknte_root 写的是用户数据目录
    文件，启动器不跟随。
  - 提权辅助进程的安全事件审计日志在用户数据目录 elev_helper_audit.log
    （SEC-10：所有提权操作与拒绝均可追溯）。
  - 若手动以管理员身份运行面板，数据落管理员账户的 LocalAppData，与普通
    权限运行不共享（建议保持普通权限运行）。

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


def _git_short():
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10, cwd=ROOT)
        return (r.stdout or "").strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def copy_data_tables(dest_data):
    """数据表 + mysqil 权威分组快照 + 构建标识随包分发（图鉴页/离线解析器自测依赖
    data/mysqil/；build_info.json 让用户一眼区分面板新旧）。幂等：重跑构建时目录
    可能已存在（上一次尝试的残留），覆盖而不是报错。"""
    for name in TABLE_FILES:
        shutil.copy2(os.path.join(ROOT, "data", name), os.path.join(dest_data, name))
    Path(dest_data, "build_info.json").write_text(
        json.dumps({"built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "git": _git_short()}, ensure_ascii=False),
        encoding="utf-8")
    mysqil_src = os.path.join(ROOT, "data", "mysqil")
    if os.path.isdir(mysqil_src):
        shutil.copytree(mysqil_src, os.path.join(dest_data, "mysqil"), dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # 攻略站数据源快照：source_crosscheck.json=独立来源交叉核对证据链（静态、
    # selftest 依赖，随包分发）；guide_sites.json 为运行时用户数据，不随包。
    # character_cooldowns.json 已移除（SEC-04①/B3，2026-11-18 用户裁定①：其源站
    # 已全面禁用（AGENTS.md 数据源白名单），文件 git rm 删除，不再随包分发）。
    extra_data_files = [
        os.path.join("sites", "source_crosscheck.json"),
    ]
    for rel in extra_data_files:
        src = os.path.join(ROOT, "data", rel)
        if os.path.isfile(src):
            os.makedirs(os.path.dirname(os.path.join(dest_data, rel)), exist_ok=True)
            shutil.copy2(src, os.path.join(dest_data, rel))


def check_dist_data_clean(dist_roots=(DIST, DIST_SINGLE)):
    """打包前数据自检（BUILD-01/SEC-01 闸门；纯检查，绝不删任何文件）。

    dist 各档 data/ 下只允许出厂数据表集（与 copy_data_tables 写入一一对应）：
    TABLE_FILES + build_info.json + mysqil/ 整目录
    （公开游戏数据，含随包 .bak）+ sites/source_crosscheck.json（sites 下唯一
    放行文件）。character_cooldowns.json 已移除（SEC-04①/B3，2026-11-18 用户
    裁定①：禁用源站数据清除（详见 AGENTS.md 数据源白名单），文件 git rm 删除，保留集 15→14 文件）。其余任何文件——state.json*、backups/、panel.log、runtime.json*、
    oknte_plan.json、ocr_layout.json*、game_data_custom.json、edge_profile/、
    avatars/、tajiduo_*、gacha_history.json、ok-nte-live.log、
    sites/guide_sites.json*、materials.json.pre-manual.bak 等历史 .bak 名等
    个人/运行时数据——收进 offender 相对路径清单返回，调用方据此在
    build_dist() 的 rmtree(DIST) 之前阻断打包。dist 根目录文件（crash.log/
    launcher.log 等）不在本闸门契约范围（只扫各档 data/）。
    """
    allowed_files = set(TABLE_FILES) | {"build_info.json"}  # character_cooldowns.json 已移除（SEC-04①/B3）
    allowed_dirs = {"mysqil"}
    allowed_exact = {os.path.join("sites", "source_crosscheck.json")}
    offenders = []
    for dist_root in dist_roots:
        data = os.path.join(dist_root, "data")
        if not os.path.isdir(data):
            continue
        for base, _dirs, files in os.walk(data):
            for name in files:
                rel = os.path.relpath(os.path.join(base, name), data)
                top = rel.split(os.sep)[0]
                if rel in allowed_exact:
                    continue
                if top in allowed_dirs:
                    continue  # mysqil/ 整目录放行
                if name in allowed_files and os.sep not in rel:
                    continue
                offenders.append(os.path.join(os.path.basename(dist_root), "data", rel))
    return offenders


def _generate_icon(path):
    import sys
    if ROOT not in sys.path:  # 直接运行 tools/build_exe.py 时 sys.path 不含项目根
        sys.path.insert(0, ROOT)
    from app.icon_gen import generate_icon
    generate_icon(path)


def build_dist():
    print("== 组装 dist/OKntePlanner ==")
    if os.path.isdir(DIST):
        shutil.rmtree(DIST)
    os.makedirs(os.path.join(DIST, "data"))
    copy_tree_clean(os.path.join(ROOT, "app"), os.path.join(DIST, "app"))
    copy_tree_clean(os.path.join(ROOT, "tests"), os.path.join(DIST, "tests"))
    copy_data_tables(os.path.join(DIST, "data"))
    Path(DIST, "使用说明.txt").write_text(USAGE_TXT, encoding="utf-8")
    if os.path.isfile(os.path.join(ROOT, "README.md")):
        shutil.copy2(os.path.join(ROOT, "README.md"), DIST)

    csc = find_csc()
    if not csc:
        raise RuntimeError("未找到 csc.exe（.NET Framework 编译器）；"
                           "Windows 10/11 自带，请检查 C:\\Windows\\Microsoft.NET")
    ico = os.path.join(ROOT, "tools", "_build", "app.ico")
    os.makedirs(os.path.dirname(ico), exist_ok=True)
    _generate_icon(ico)  # 应用专属图标（EXE/任务栏，阶段16）
    with tempfile.TemporaryDirectory(prefix="oknte_build_") as tmp:
        cs = os.path.join(tmp, "Launcher.cs")
        manifest = os.path.join(tmp, "app.manifest")
        # BOM：csc 才能按 UTF-8 读中文
        Path(cs).write_text(LAUNCHER_CS, encoding="utf-8-sig")
        Path(manifest).write_text(APP_MANIFEST, encoding="utf-8")
        exe = os.path.join(DIST, "异环养成规划器.exe")
        result = subprocess.run(
            [csc, "/nologo", "/codepage:65001", "/target:winexe",
             f"/win32icon:{ico}", f"/win32manifest:{manifest}", f"/out:{exe}", cs],
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
    if b"asInvoker" in blob:
        checks.append("清单含 asInvoker（SEC-10 面板降权）✅")
    else:
        checks.append("清单检查：未在资源段直接匹配到 asInvoker（以 /win32manifest 参数为准，人工复核）")

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


BUILD_REQS = os.path.join(ROOT, "tools", "requirements-build.txt")


def _build_pins():
    """读 tools/requirements-build.txt 的钉版映射（小写包名→版本）。缺失/坏行→{}。"""
    pins = {}
    try:
        with open(BUILD_REQS, encoding="utf-8") as f:
            for raw in f:
                line = raw.split("#", 1)[0].strip()
                if "==" not in line:
                    continue
                name, ver = (part.strip() for part in line.split("==", 1))
                if name and ver:
                    pins[name.lower()] = ver
    except OSError:
        return {}
    return pins


def _pinned_specs(packages):
    """包名按钉版映射拼成 pkg==ver（无钉版则原样，保持旧行为）。"""
    pins = _build_pins()
    return [f"{p}=={pins[p.lower()]}" if p.lower() in pins else p
            for p in packages]


def _dep_versions(py):
    """PYI_DEPS 内已装版本清单（构建日志留证，供双构建工具链比对）。"""
    r = subprocess.run(
        [py, "-m", "pip", "list", "--path", PYI_DEPS, "--format=freeze",
         "--disable-pip-version-check"],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return f"(pip list 失败 exit={r.returncode})"
    return (r.stdout or "").strip()


def _ensure_pyi_deps(py, packages):
    """把额外依赖装进隔离目录 tools/_build/pyi（绝不碰 ok-nte 的 Python）。返回是否成功。

    镜像优先（腾讯云），失败回退默认 PyPI——2026-11-03 镜像抖动导致
    "Could not find a version" 假性缺依赖、单文件档静默跳过（真机反馈）。

    钉版（B3 W3 BUILD-05 前提）：packages 中在 tools/requirements-build.txt
    有钉版的包按 pkg==ver 安装（--upgrade 保证陈旧 target 目录也被替换），
    两次构建使用同一钉版工具链；文件缺失时退回未钉版旧行为。"""
    os.makedirs(PYI_DEPS, exist_ok=True)
    specs = _pinned_specs(packages)
    r = subprocess.run(
        [py, "-m", "pip", "install", "--quiet", "--upgrade", "--target",
         PYI_DEPS,
         "--index-url", "https://mirrors.cloud.tencent.com/pypi/simple/",
         *specs],
        capture_output=True, text=True, timeout=900)
    if r.returncode == 0:
        print(f"  构建依赖已装（{specs}）→ {PYI_DEPS}")
        print(f"  PYI_DEPS 版本清单:\n{_dep_versions(py)}")
        return True
    print("依赖源 mirrors.cloud.tencent.com 安装失败，尝试默认 PyPI…")
    r = subprocess.run(
        [py, "-m", "pip", "install", "--quiet", "--upgrade", "--target",
         PYI_DEPS,
         "--index-url", "https://pypi.org/simple/",
         *specs],
        capture_output=True, text=True, timeout=900)
    if r.returncode == 0:
        print(f"  构建依赖已装（{specs}，回退 pypi.org）→ {PYI_DEPS}")
        print(f"  PYI_DEPS 版本清单:\n{_dep_versions(py)}")
        return True
    print(f"依赖安装失败（不阻断，运行时将自动降级）:\n"
          f"{r.stdout[-400:]}\n{r.stderr[-400:]}")
    return False


def try_pyinstaller_single(py):
    print("== 可选档：PyInstaller 单文件 EXE（失败不阻断）==")
    deps = PYI_DEPS
    work = os.path.join(ROOT, "tools", "_build", "work")
    ico = os.path.join(ROOT, "tools", "_build", "app.ico")
    os.makedirs(os.path.dirname(ico), exist_ok=True)
    _generate_icon(ico)  # 幂等：单文件档独立生成（不依赖 build_dist 的局部变量）
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
             *collect, "--icon", ico,
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
        # 冻结模式首启播种源在 EXE 旁 data\：数据表模板必须随行（否则首启 import planner 失败）
        os.makedirs(os.path.join(DIST_SINGLE, "data"), exist_ok=True)
        copy_data_tables(os.path.join(DIST_SINGLE, "data"))
        Path(DIST_SINGLE, "使用说明.txt").write_text(USAGE_TXT, encoding="utf-8")
        # 探针（无 admin 标志）：验证冻结环境首启把数据播种进用户数据目录
        # （%LOCALAPPDATA%\OKntePlanner\data）而非 EXE 旁/_MEIPASS 临时目录。
        # EXE 旁预置占位出厂数据表使复制分支真实执行；LOCALAPPDATA 指向临时目录，
        # 不在构建机真实用户目录播种/提前落 .migrated（会关死首启迁移幂等门）。
        probe_dir = os.path.join(ROOT, "tools", "_build", "probe")
        os.makedirs(probe_dir, exist_ok=True)
        probe_py = os.path.join(probe_dir, "probe.py")
        Path(probe_py).write_text(
            "from app.state import DATA_DIR\nprint(DATA_DIR)\n", encoding="utf-8")
        r2 = subprocess.run(
            [py, "-m", "PyInstaller", "--noconfirm", "--onefile",
             "--name", "probe", "--distpath", probe_dir,
             "--workpath", work, "--specpath", probe_dir, probe_py],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
        assert r2.returncode == 0, f"探针构建失败:\n{r2.stderr[-400:]}"
        probe_data = os.path.join(probe_dir, "data")
        os.makedirs(probe_data, exist_ok=True)
        Path(probe_data, "materials.json").write_text(
            json.dumps({"placeholder": "probe"}, ensure_ascii=False), encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="oknte_probe_env_") as env_dir:
            r3 = subprocess.run([os.path.join(probe_dir, "probe.exe")],
                                capture_output=True, text=True, timeout=60,
                                env={**os.environ, "LOCALAPPDATA": env_dir})
            data_dir = (r3.stdout or "").strip().splitlines()[-1] if r3.stdout.strip() else ""
            expected = os.path.join(env_dir, "OKntePlanner", "data")
            assert data_dir.lower() == expected.lower(), \
                f"冻结环境数据目录应在用户数据目录 {expected}，实际 {data_dir!r}"
            assert os.path.isfile(os.path.join(expected, ".migrated")), \
                "冻结探针首启未写 .migrated（播种未完成）"
            assert os.path.isfile(os.path.join(expected, "materials.json")), \
                "冻结探针未播种出厂数据表 materials.json"
            print(f"冻结环境数据目录探针通过 ✅（{data_dir}）")
        return exe
    except Exception as e:  # noqa: BLE001 —— 可选档任何失败都不阻断主产物
        print(f"跳过：可选档异常 {type(e).__name__}: {e}")
        return None


PYI_DEPS = os.path.join(ROOT, "tools", "_build", "pyi")
HAS_PYWEBVIEW = False  # 由 try_pyinstaller_single 设置（原生窗口依赖是否打进包）


def _probe_panel_token(path):
    """读面板令牌文件（打包态=隔离 LOCALAPPDATA 下 OKntePlanner/data/panel_token.json；
    源码态副本=app_root/data/panel_token.json）。缺/坏/空返回 None。仅探针内存使用，
    绝不 print（token 不落构建日志）。"""
    try:
        with open(path, encoding="utf-8") as f:
            token = json.load(f).get("token")
    except (OSError, ValueError):
        return None
    return token if isinstance(token, str) and token else None


def _launch_and_probe(exe_path, port, ui_env, timeout=90):
    """真实拉起一个 EXE：轮询 /api/bootstrap 与首页，随后杀树并清理新产生的文件。
    ui_env 控制 UI 策略（如 {"OKNTE_PLANNER_UI": "edge"} / {}=frozen 默认原生窗口）。
    失败时保留并返回子进程控制台输出尾部，供构建摘要定位根因。
    LOCALAPPDATA 指向临时目录：面板用户数据/首启 .migrated 都落隔离目录，绝不在
    构建机真实用户目录播种或提前关死旧数据的首启自动迁移门；api_ok 时断言
    .migrated 已写——LAUNCHER_CS 注入的 OKNTE_PLANNER_USER_DATA 与 state.py 读取名
    是两处无编译期检查的字符串，拼错即静默失效，此处以真机拉起验证接线。"""
    import time as _time
    import urllib.request
    exe_dir = os.path.dirname(exe_path)
    pristine = {os.path.join(base, n)
                for base, _dirs, files in os.walk(exe_dir) for n in files}
    out_path = os.path.join(exe_dir, "_probe_out.log")
    out_file = FileIO(out_path, "wb")
    # ignore_cleanup_errors：Edge 壳残留子进程可能锁住临时 LOCALAPPDATA 句柄，
    # 清理抛异常会打断构建收尾（项目基线 Python 3.10+）
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True,
                                     prefix="oknte_probe_lad_") as env_dir:
        proc = subprocess.Popen(
            [exe_path], cwd=exe_dir, env={**os.environ,
                                          "OKNTE_PLANNER_PORT": str(port),
                                          "LOCALAPPDATA": env_dir,
                                          **ui_env},
            stdout=out_file, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        # 强制直连：系统代理/TUN 可能截获 127.0.0.1 请求导致探测假超时。
        # 单次请求超时必须 > /api/bootstrap 的响应耗时（内含 tasklist+PowerShell 进程检测，2~4 秒）。
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        # SEC-05：/api 全部要求 X-Panel-Token 头——令牌由面板写进隔离用户数据目录
        # （与 .migrated marker 同 join 口径，frozen 态 DATA_DIR=LOCALAPPDATA/OKntePlanner/data）；
        # 超时内未生成即判失败（面板未以打包态运行），漏带 token 头会表现为探测红
        # 而非静默绕过
        token_path = os.path.join(env_dir, "OKntePlanner", "data", "panel_token.json")
        _time.sleep(4)  # 先给启动链（EXE→python→面板）留出时间，再做轮询
        api_ok, page_ok, detail = False, False, ""
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            token = _probe_panel_token(token_path)
            if not token:
                detail = "panel_token.json 未生成（面板未以打包态运行？）"
                _time.sleep(1)
                continue
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/bootstrap",
                    headers={"X-Panel-Token": token})
                with direct.open(req, timeout=15) as resp:
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
        if api_ok:
            # 接线断言：面板 import app.state 即触发首启迁移写 marker（bundled 非空
            # 分支在真 dist 上必走复制），不依赖面板写任何状态——隔离 LOCALAPPDATA
            # 下未见 .migrated = 面板没以打包态运行（C# 侧注入变量名拼错/未注入）
            marker = os.path.join(env_dir, "OKntePlanner", "data", ".migrated")
            assert os.path.isfile(marker), (
                "LAUNCHER_CS 与 state.py 的 OKNTE_PLANNER_USER_DATA 接线断裂："
                "面板未以打包态运行（隔离 LOCALAPPDATA 下未见 .migrated）")
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
        Path(cs).write_text(LAUNCHER_CS, encoding="utf-8-sig")
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
        # SEC-05：无 LOCALAPPDATA 重定向 → 面板以源码态把令牌写进副本
        # d/data/panel_token.json（源码态 DATA_DIR=app_root/data）；文件未生成期间
        # 请求会 401，故每轮先读令牌、缺则跳过本轮（避免 401 被误判为端口不可用）
        token_path = os.path.join(d, "data", "panel_token.json")
        found = None
        deadline = _time.time() + 90
        while _time.time() < deadline and found is None:
            token = _probe_panel_token(token_path)
            if token:
                for port in range(8765, 8776):
                    try:
                        # 单次超时 10s：bootstrap 含进程检测，响应可能需要数秒
                        req = urllib.request.Request(
                            f"http://127.0.0.1:{port}/api/bootstrap",
                            headers={"X-Panel-Token": token})
                        with direct.open(req, timeout=10) as r:
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
        copy_data_tables(os.path.join(single_dir, "data"))
        single = _build_test_single(py, single_dir, HAS_PYWEBVIEW)
        if single:
            probe("单文件(asInvoker 测试变体·原生窗口)", single, 18772, {})
        else:
            results.append("单文件测试变体：未构建（PyInstaller 依赖不可用）")
    results.append("正式双 EXE（含 UAC 层）：Manual——请双击验证 UAC 允许后面板打开")
    results.append(_probe_auto_avoid(py))
    return results


def main():
    # 打包前数据自检必须最先执行（先于 build_dist 的 rmtree(DIST)）：
    # 新布局下用户数据不再落 dist，stash/restore 已无意义且语义已错
    # （copy2 覆盖会把陈旧表盖回刚刷新的出厂数据表），故删除并以
    # 『自检阻断 + 迁移 CLI』替代
    offenders = check_dist_data_clean()
    if offenders:
        print("== 打包前数据自检未通过：dist data/ 下存在个人/运行时数据 ==")
        for rel in offenders:
            print(f"  {rel}")
        print("处置：先迁移——python -m tools.migrate_user_data --from <对应 dist 的 data 目录>"
              " --apply（可先加 --dry-run 演练）；\n"
              "      或确认无用后手动删除上述文件；若上次构建被中断，残留文件"
              "（自测/冒烟产物）可径直清理后重试。")
        for root in (DIST, DIST_SINGLE):
            if os.path.basename(root) in {o.split(os.sep, 1)[0] for o in offenders}:
                print(f"  待迁移目录: {os.path.join(root, 'data')}")
        return 1
    py = embedded_python()
    print(f"验证用 Python: {py}")
    try:
        build_dist()
        checks = verify_dist(py)
        single = try_pyinstaller_single(py)
        checks += verify_exes_live(py)
    finally:
        # 清理构建期第三方依赖缓存（vendored PyInstaller/bottle 会被安全扫描判 95 条
        # 高危并拦截 commit；均为库自身模式而非项目代码）。下次构建自动重装。
        if os.path.isdir(PYI_DEPS):
            shutil.rmtree(PYI_DEPS, ignore_errors=True)
            print("  已清理构建期依赖缓存 tools/_build/pyi（下次构建自动重建）")
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
