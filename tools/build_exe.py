"""阶段7：把 Web 面板封装为 EXE 分发（W4b-R5：唯一发布形态=单文件主程序）。

产物（唯一发布形态，2026-11-21 用户裁定 W4b-R5）：
    dist/OKntePlanner_single/
        异环养成规划器.exe  PyInstaller 单文件主程序（--name 异环养成规划器，
                            自带 Python，双击即弹出独立应用窗口）
        data/              出厂数据表模板（不含用户 state.json/backups/runtime）
        使用说明.txt
构建参数（W4b-R5，探针证据 .tmp/b3-w4br5-probe/，PyInstaller 6.22.3 实测）：
    --runtime-tmpdir E:/OKntePlannerRuntime/temp —— 绝对路径（R3 探针实测相对值
    按 CWD 解析不可靠）；不同 CWD 两次启动 _MEI 全落 E；目录不存在 bootloader
    自动创建（含多级父目录）；创建失败明确报错退出、绝不静默回落 %TEMP%。
    --name 异环养成规划器 —— 中文名构建实跑可行。
目录版停止生成：launcher 壳（目录启动器与单文件壳）一并撤销，csc 编译链整体
    移除；构建入口显式 rmtree(dist/OKntePlanner) 清掉现存残留，构建后该目录
    必须不存在（发布形态硬 gate 把守）。
打包态：EXE 自带 Python（frozen），用户数据统一落 %LOCALAPPDATA%\\OKntePlanner\\data；
    打包前 check_dist_data_clean 自检阻断含个人数据的 dist（main() 最先调用，
    先于任何 rmtree 清理）。
验证（verify_dist）：目录版不再生成 → dist 内源码态自测前提消失（single 档无
    app/ 散文件），改在源码树自测（cwd=ROOT，钉扎 Python 跑 selftest/smoke——
    同一份 app 代码同一 3.12）+ 唯一 EXE 实跑探针（API+首页，_launch_and_probe
    既有先例）。
发布形态硬 gate（check_dist_product_gates）：dist 下递归 *.exe 恰 1 个且为
    dist/OKntePlanner_single/异环养成规划器.exe；dist/OKntePlanner 不存在；
    任一不满足 → 构建 FAIL。
联网安装失败不阻断（"尽量封装"边界见 docs/results/EXE-PACKAGING-Result.md）。

运行方式（项目根，任一 Python 3.10+）：
    python tools/build_exe.py
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
DIST = os.path.join(ROOT, "dist", "OKntePlanner")  # 废弃目录版：仅存清理/硬 gate 引用（构建后不存在）
DIST_SINGLE = os.path.join(ROOT, "dist", "OKntePlanner_single")
SINGLE_EXE_NAME = "异环养成规划器"  # 唯一发布 EXE 名（W4b-R5；中文名构建实测可行）
SINGLE_EXE = os.path.join(DIST_SINGLE, SINGLE_EXE_NAME + ".exe")
# 绝对运行缓存目录（W4b-R5 探针实测形态）：R3 探针证实相对值按 CWD 解析不可靠；
# 绝对值不同 CWD 两场景 _MEI 全落 E；bootloader 对缺失目录自动创建（含父树）、
# 创建失败明确报错退出（不静默回落 %TEMP%）。
RUNTIME_TMPDIR = "E:/OKntePlannerRuntime/temp"
TMP_ROOT = os.path.join(ROOT, ".tmp")  # C 盘零落地：构建期 tempfile 一律 dir= 此处（E 盘）
EMBEDDED_DEFAULT = r"D:\GamingAssistant\ok-nte\data\apps\ok-nte\python\python.exe"
TABLE_FILES = ("materials.json", "yields.json", "requirements.json", "game_data.json")

USAGE_TXT = """异环养成规划器（OKnteAutofoot）使用说明
==========================================

【发布形态，唯一受支持】OKntePlanner_single\\异环养成规划器.exe
  双击 → 直接弹出一个独立的应用窗口（自己的进程与任务栏项，无黑框、无浏览器、
  无 Edge 壳）。EXE 即主程序（PyInstaller 单文件打包，自带 Python，无需安装）。
  面板以普通权限运行（SEC-10：HTTP/token 服务面不随面板提权）；首次「开始执行」
  或背包 OCR 时会弹一次 UAC——那是受限提权辅助进程（仅用于提权拉起 ok-nte 任务
  与向游戏窗口发送输入，操作白名单封闭、随面板退出）。关闭窗口即整套退出
  （正在执行的任务会先安全停止并记账）。
  （2026-11-21 用户裁定：launcher 壳与目录启动器版已移除，本 EXE 为唯一发布形态。）

首次使用：
  - 「角色」页建角色填目标；「库存」页按背包实际填一次库存；
  - 「数据表」页校准"每轮产出"（默认是估计值，务必校准）；
  - 面板里先点「预演（不落盘）」确认无误，再「开始执行」。

数据与日志：
  - 用户数据统一在 %LOCALAPPDATA%\\OKntePlanner\\data（角色/库存/设置/图鉴
    覆盖层/塔吉多凭证/OCR 标定等），本项位置不变。【勿同时双开两个面板】
    ——并发写会互相覆盖。
  - 运行缓存（日志/WebView 缓存）统一在 E:\\OKntePlannerRuntime（2026-11-21
    起，运行缓存迁出 C 盘）：panel.log 与 ok-nte 执行日志在
    E:\\OKntePlannerRuntime\\logs，WebView2 缓存与 Edge 档在
    E:\\OKntePlannerRuntime\\webview，crash.log 在
    E:\\OKntePlannerRuntime\\crash。E 盘不可写时自动回落既有位置（用户数据
    目录/EXE 旁）并在日志告警一次；可用环境变量 OKNTE_RUNTIME_ROOT 指定
    其他位置。
  - 单文件解包目录（_MEI）：E:\\OKntePlannerRuntime\\temp（构建期 PyInstaller
    绝对 --runtime-tmpdir 指定）。目录不存在时 bootloader 启动时自动创建
    （探针实测，含多级父目录）；目录创建失败（如 E 盘不可写）时 bootloader
    明确报错退出——绝不静默回落 C 盘 %TEMP%（探针实测）。解包内容退出自清，
    父目录保留。
  - EXE 旁的 data\\ 仅为出厂数据表模板（materials/yields/requirements 等），
    面板不往里写任何用户数据。
  - 首次启动自动初始化用户数据目录；旧版（数据在 EXE 旁 data\\）首次启动会
    自动整树迁移到用户数据目录，原目录原样保留（只复制不删除）。
  - 【放心】重新打包/更新版本不会动用户数据（用户数据不在 EXE 目录内，
    替换 EXE 目录即可升级）。
  - 手动迁移/合并旧数据：python -m tools.migrate_user_data --from <旧数据
    目录> --apply（可加 --dry-run 演练；目标已存在的文件一律跳过）。
  - 更新出厂数据表：删除用户数据目录下的 .migrated 与对应表文件后重启面板
    （原子 no-clobber 只补缺失文件，其余用户数据不动）。
  - 排查日志：panel.log 在 E:\\OKntePlannerRuntime\\logs\\panel.log；
    crash.log 在 E:\\OKntePlannerRuntime\\crash\\crash.log；ok-nte-live.log 与
    ok-nte-headless-*.log 在 logs 目录（与 panel.log 同目录）。
    （launcher.log 随 launcher 壳移除已消失。）
    端口被占会自动避让并在 panel.log 提示新端口；双击无反应先看上述日志。
  - 提权辅助进程的安全事件审计日志在 E:\\OKntePlannerRuntime\\logs\\
    elev_helper_audit.log（SEC-10：所有提权操作与拒绝均可追溯；E 盘不可写
    回落用户数据目录同名）。
  - 若手动以管理员身份运行面板，数据落管理员账户的 LocalAppData，与普通
    权限运行不共享（建议保持普通权限运行）。

详细文档见源码仓库 docs\\ 目录（上游兼容清单、各阶段验收报告）。
"""


def copy_tree_clean(src, dst):
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _git_short():
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=10, cwd=ROOT, shell=False)
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


def check_dist_data_clean(dist_roots=(DIST_SINGLE,)):
    """打包前数据自检（BUILD-01/SEC-01 闸门；纯检查，绝不删任何文件）。

    W4b-R5：目录版停止生成 → 扫描面只剩 single 档（默认 dist_roots=
    (DIST_SINGLE,)）。各档 data/ 下只允许出厂数据表集（与 copy_data_tables
    写入一一对应）：TABLE_FILES + build_info.json + mysqil/ 整目录
    （公开游戏数据，含随包 .bak）+ sites/source_crosscheck.json（sites 下唯一
    放行文件）。character_cooldowns.json 已移除（SEC-04①/B3，2026-11-18 用户
    裁定①：禁用源站数据清除（详见 AGENTS.md 数据源白名单），文件 git rm 删除，
    保留集 15→14 文件）。其余任何文件——state.json*、backups/、panel.log、
    runtime.json*、oknte_plan.json、ocr_layout.json*、game_data_custom.json、
    edge_profile/、avatars/、tajiduo_*、gacha_history.json、ok-nte-live.log、
    sites/guide_sites.json*、materials.json.pre-manual.bak 等历史 .bak 名等
    个人/运行时数据——收进 offender 相对路径清单返回，调用方据此在清理/重建
    dist 之前阻断打包。dist 根目录文件（使用说明.txt 等）不在本闸门契约范围
    （只扫各档 data/）。
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


def check_dist_product_gates():
    """发布形态硬 gate（W4b-R5 新增，构建完成后调用；与 check_dist_data_clean
    并列的发布形态闸）：
      ① dist/ 下递归枚举 *.exe——PRODUCT_EXE_COUNT 必须==1，且唯一结果必须是
         dist/OKntePlanner_single/异环养成规划器.exe（探针/构建工具自身的临时
         EXE 全部落在 dist 外，绝不进入发布 dist）；
      ② dist/OKntePlanner 必须 DOES_NOT_EXIST（目录版停止生成，构建入口显式
         rmtree 清残留）。
    返回违例描述清单；非空 → 构建 FAIL（main() 退出非零）。"""
    violations = []
    dist_root = os.path.join(ROOT, "dist")
    exes = []
    for base, _dirs, files in os.walk(dist_root):
        for name in files:
            if name.lower().endswith(".exe"):
                exes.append(os.path.join(base, name))
    expected = os.path.normcase(os.path.abspath(SINGLE_EXE))
    got = [os.path.normcase(os.path.abspath(p)) for p in exes]
    if got != [expected]:
        violations.append(
            f"PRODUCT_EXE_COUNT 闸：dist 下 *.exe={got!r}，应恰为 [{expected!r}]")
    if os.path.exists(DIST):
        violations.append(
            f"目录版残留闸：dist/OKntePlanner 必须 DOES_NOT_EXIST，仍存在: {DIST}")
    return violations


def _generate_icon(path):
    import sys
    if ROOT not in sys.path:  # 直接运行 tools/build_exe.py 时 sys.path 不含项目根
        sys.path.insert(0, ROOT)
    from app.icon_gen import generate_icon
    generate_icon(path)


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


def verify_dist(py):
    """dist 验证（W4b-R5 重排）：目录版不再生成、single 档无 app/ 散文件 →
    dist 内源码态自测前提消失，改在源码树自测（cwd=ROOT，同一份 app 代码同一
    钉扎 Python）+ 唯一 EXE 实跑探针（API+首页，_launch_and_probe 既有先例）。"""
    print("== dist 验证 ==")
    checks = []
    exe = SINGLE_EXE
    assert os.path.isfile(exe), f"唯一发布 EXE 缺失: {exe}"
    with open(exe, "rb") as f:
        head = f.read(2)
    assert head == b"MZ", f"EXE 不是合法 PE 文件: {head!r}"
    checks.append("EXE PE 头 (MZ) ✅")
    # 源码树自测：同一份 app 代码同一钉扎 Python（目录版 app/ 散文件已不存在）
    for step, cmd, env_extra in (
        ("自测", [py, "-m", "app.selftest"], {}),
        ("冒烟", [py, "-m", "app.smoke_test"],
         {"OKNTE_PLANNER_PORT": "18770", "OKNTE_PLANNER_NO_BROWSER": "1"}),
    ):
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.update(env_extra)
        r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=2400, shell=False)
        tail = (r.stdout or r.stderr or "").strip().splitlines()[-1:]
        print(f"源码树 {step}: exit={r.returncode} {tail[0] if tail else ''}")
        assert r.returncode == 0, f"源码树 {step} 未通过（exit {r.returncode}）"
        checks.append(f"源码树 {step} 通过 ✅")
    # 唯一 EXE 实跑探针（隔离 LOCALAPPDATA：用户数据/首启 .migrated 全落临时目录）
    api_ok, page_ok, detail, console = _launch_and_probe(exe, 18772, {})
    if api_ok and page_ok:
        checks.append("单文件 EXE 实跑：API+首页 ✅")
    elif api_ok:
        raise RuntimeError(f"单文件 EXE 实跑：API ✅ 首页 ✗ {detail} {console}")
    else:
        raise RuntimeError(f"单文件 EXE 实跑：✗ 未就绪（{detail}）{console}")
    checks.append(_probe_auto_avoid(py))
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
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, shell=False)
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
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=900, shell=False)
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
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=900, shell=False)
    if r.returncode == 0:
        print(f"  构建依赖已装（{specs}，回退 pypi.org）→ {PYI_DEPS}")
        print(f"  PYI_DEPS 版本清单:\n{_dep_versions(py)}")
        return True
    print(f"依赖安装失败（不阻断，运行时将自动降级）:\n"
          f"{r.stdout[-400:]}\n{r.stderr[-400:]}")
    return False


def try_pyinstaller_single(py):
    """构建唯一发布产物 dist/OKntePlanner_single/异环养成规划器.exe（W4b-R5 起
    主档必成：失败直接抛 RuntimeError 构建失败，不再“可选档降级跳过”——唯一
    EXE 缺席时发布形态硬 gate 必然拦截）。"""
    print("== 构建单文件主程序（唯一发布形态）==")
    deps = PYI_DEPS
    work = os.path.join(ROOT, "tools", "_build", "work")
    ico = os.path.join(ROOT, "tools", "_build", "app.ico")
    os.makedirs(os.path.dirname(ico), exist_ok=True)
    _generate_icon(ico)  # 幂等：独立生成应用图标（EXE/任务栏）
    probe = subprocess.run([py, "-m", "pip", "--version"],
                           capture_output=True, timeout=60, shell=False)
    if probe.returncode != 0:
        raise RuntimeError("构建 Python 无可用 pip，PyInstaller 依赖装不上")
    if not _ensure_pyi_deps(py, ["pyinstaller"]):
        raise RuntimeError("pyinstaller 安装失败（唯一发布主档必成，不再降级跳过）")
    # pywebview：原生窗口（自己的 WinForms 窗口与进程）；失败仅降级 Edge 壳
    global HAS_PYWEBVIEW
    HAS_PYWEBVIEW = _ensure_pyi_deps(py, ["pywebview"])
    if os.path.isdir(DIST_SINGLE):  # 全新重建：旧 EXE/残留一律清掉（硬 gate 数 EXE）
        shutil.rmtree(DIST_SINGLE)
    os.makedirs(DIST_SINGLE, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = deps
    collect = ["--collect-all", "webview", "--collect-all", "pythonnet"] \
        if HAS_PYWEBVIEW else []
    # W4b-R5（探针证据 .tmp/b3-w4br5-probe/）：--runtime-tmpdir 绝对值 → 不同
    # CWD 启动 _MEI 全落 E:/OKntePlannerRuntime/temp，目录缺失由 bootloader
    # 自动创建（含父树），创建失败明确报错退出不回落 %TEMP%；--name 中文名
    # 构建实跑可行；launcher 壳撤销，EXE 即主程序（frozen 态由 bootloader 置位）。
    r = subprocess.run(
        [py, "-m", "PyInstaller", "--noconfirm", "--onefile", "--noconsole",
         *collect, "--icon", ico,
         "--runtime-tmpdir", RUNTIME_TMPDIR,
         "--add-data",
         os.path.join(ROOT, "app", "web", "index.html") + os.pathsep + os.path.join("app", "web"),
         "--name", "异环养成规划器", "--distpath", DIST_SINGLE,
         "--workpath", work, "--specpath", os.path.dirname(work) or ".",
         os.path.join("app", "main.py")],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=900, shell=False)
    exe = SINGLE_EXE
    if r.returncode != 0 or not os.path.isfile(exe):
        raise RuntimeError(f"PyInstaller 单文件主程序构建失败:\n"
                           f"{r.stdout[-600:]}\n{r.stderr[-600:]}")
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
    # 探针 EXE 同样带绝对 --runtime-tmpdir（C 盘零落地：_MEI 不落系统 %TEMP%）。
    probe_dir = os.path.join(ROOT, "tools", "_build", "probe")
    os.makedirs(probe_dir, exist_ok=True)
    probe_py = os.path.join(probe_dir, "probe.py")
    Path(probe_py).write_text(
        "from app.state import DATA_DIR\nprint(DATA_DIR)\n", encoding="utf-8")
    r2 = subprocess.run(
        [py, "-m", "PyInstaller", "--noconfirm", "--onefile",
         "--runtime-tmpdir", RUNTIME_TMPDIR,
         "--name", "probe", "--distpath", probe_dir,
         "--workpath", work, "--specpath", probe_dir, probe_py],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=600, shell=False)
    assert r2.returncode == 0, f"探针构建失败:\n{r2.stderr[-400:]}"
    probe_data = os.path.join(probe_dir, "data")
    os.makedirs(probe_data, exist_ok=True)
    Path(probe_data, "materials.json").write_text(
        json.dumps({"placeholder": "probe"}, ensure_ascii=False), encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="oknte_probe_env_",
                                     dir=TMP_ROOT) as env_dir:
        r3 = subprocess.run([os.path.join(probe_dir, "probe.exe")],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=60,
                            env={**os.environ, "LOCALAPPDATA": env_dir}, shell=False)
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
    LOCALAPPDATA 指向临时目录（仓库 .tmp/，C 盘零落地）：面板用户数据/首启
    .migrated 都落隔离目录，绝不在构建机真实用户目录播种或提前关死旧数据的
    首启自动迁移门；api_ok 时断言 .migrated 已写——打包态判定（sys.frozen）由
    PyInstaller bootloader 置位，隔离 LOCALAPPDATA 下未见 .migrated 即面板
    未以打包态运行，此处以真机拉起验证。"""
    import time as _time
    import urllib.request
    exe_dir = os.path.dirname(exe_path)
    pristine = {os.path.join(base, n)
                for base, _dirs, files in os.walk(exe_dir) for n in files}
    out_path = os.path.join(exe_dir, "_probe_out.log")
    out_file = FileIO(out_path, "wb")
    # ignore_cleanup_errors：Edge 壳残留子进程可能锁住临时 LOCALAPPDATA 句柄，
    # 清理抛异常会打断构建收尾（项目基线 Python 3.10+）；dir= 仓库 .tmp/（C 盘零落地）
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True,
                                     prefix="oknte_probe_lad_", dir=TMP_ROOT) as env_dir:
        proc = subprocess.Popen(
            [exe_path], cwd=exe_dir, env={**os.environ,
                                          "OKNTE_PLANNER_PORT": str(port),
                                          "LOCALAPPDATA": env_dir,
                                          **ui_env},
            stdout=out_file, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            shell=False)
        # 强制直连：系统代理/TUN 可能截获 127.0.0.1 请求导致探测假超时。
        # 单次请求超时必须 > /api/bootstrap 的响应耗时（内含 tasklist+PowerShell 进程检测，2~4 秒）。
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        # SEC-05：/api 全部要求 X-Panel-Token 头——令牌由面板写进隔离用户数据目录
        # （与 .migrated marker 同 join 口径，frozen 态 DATA_DIR=LOCALAPPDATA/OKntePlanner/data）；
        # 超时内未生成即判失败（面板未以打包态运行），漏带 token 头会表现为探测红
        # 而非静默绕过
        token_path = os.path.join(env_dir, "OKntePlanner", "data", "panel_token.json")
        _time.sleep(4)  # 先给启动链（EXE 解包→面板）留出时间，再做轮询
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
                       capture_output=True, timeout=30, shell=False)
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
            # 下未见 .migrated = 面板没以打包态运行（frozen 判定失效）
            marker = os.path.join(env_dir, "OKntePlanner", "data", ".migrated")
            assert os.path.isfile(marker), (
                "面板未以打包态运行（隔离 LOCALAPPDATA 下未见 .migrated；"
                "frozen 判定应由 PyInstaller bootloader 置位 sys.frozen）")
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


def _probe_auto_avoid(py):
    """复刻用户真实场景：不带端口 env 启动面板——若 8765 被占（如输入法），
    应自动避让到相邻端口并在该端口就绪。目录版停止生成（W4b-R5）后，源码态
    副本改为从源码树组装（app/ 整树 + 出厂数据表，语义与旧 dist 副本一致）。"""
    import time as _time
    import urllib.request
    with tempfile.TemporaryDirectory(prefix="oknte_autoport_", dir=TMP_ROOT) as tmp:
        d = os.path.join(tmp, "app_root")
        os.makedirs(os.path.join(d, "data"))
        copy_tree_clean(os.path.join(ROOT, "app"), os.path.join(d, "app"))
        copy_data_tables(os.path.join(d, "data"))
        env = {**os.environ, "OKNTE_PLANNER_NO_BROWSER": "1"}
        proc = subprocess.Popen([py, "-m", "app.main"], cwd=d, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, shell=False)
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
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, shell=False)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
        if found:
            note = "8765 被占用，已自动避让" if found != 8765 else "8765 空闲，直接绑定"
            return f"端口冲突自动避让：✅ 面板起在 {found}（{note}）"
        return "端口冲突自动避让：✗ 90 秒内未在任何候选端口(8765-8775)就绪"


def main():
    # 打包前数据自检必须最先执行（先于任何 rmtree 清理/构建）：dist 各档 data/
    # 下只允许出厂数据集，个人/运行时数据先迁移（W4b-R5：扫描面只剩 single 档）
    offenders = check_dist_data_clean()
    if offenders:
        print("== 打包前数据自检未通过：dist data/ 下存在个人/运行时数据 ==")
        for rel in offenders:
            print(f"  {rel}")
        print("处置：先迁移——python -m tools.migrate_user_data --from <对应 dist 的 data 目录>"
              " --apply（可先加 --dry-run 演练）；\n"
              "      或确认无用后手动删除上述文件；若上次构建被中断，残留文件"
              "（自测/冒烟产物）可径直清理后重试。")
        print(f"  待迁移目录: {os.path.join(DIST_SINGLE, 'data')}")
        return 1
    py = embedded_python()
    print(f"验证用 Python: {py}")
    os.makedirs(TMP_ROOT, exist_ok=True)  # C 盘零落地：构建期 tempfile 一律 dir= 仓库 .tmp/
    # W4b-R5：目录版停止生成——显式清掉现存废弃目录（构建后 dist/OKntePlanner
    # 必须不存在，由发布形态硬 gate 把守）
    if os.path.isdir(DIST):
        shutil.rmtree(DIST)
        print(f"  已移除废弃目录版残留: {DIST}")
    try:
        single = try_pyinstaller_single(py)      # 唯一发布 EXE（主档必成）
        checks = verify_dist(py)                 # 源码树自测 + EXE 实跑探活
        violations = check_dist_product_gates()  # 发布形态硬 gate
        if violations:
            print("== 发布形态硬 gate 未通过 ==")
            for v in violations:
                print(f"  {v}")
            return 1
        checks.append("发布形态硬 gate（唯一 EXE + 目录版不存在）✅")
    finally:
        # 清理构建期第三方依赖缓存（vendored PyInstaller/bottle 会被安全扫描判 95 条
        # 高危并拦截 commit；均为库自身模式而非项目代码）。下次构建自动重装。
        if os.path.isdir(PYI_DEPS):
            shutil.rmtree(PYI_DEPS, ignore_errors=True)
            print("  已清理构建期依赖缓存 tools/_build/pyi（下次构建自动重建）")
    print("\n== 构建摘要 ==")
    for line in checks:
        print(" ", line)
    print(f"  发布主产物（唯一）: {single}")
    print(f"  _MEI 运行缓存: {RUNTIME_TMPDIR}（目录不存在由 bootloader 自动创建；"
          f"创建失败明确报错，不回落 %TEMP%）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
