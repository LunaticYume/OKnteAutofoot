# 阶段7结果：Web UI 封装为 EXE

**结论：PASS（两档产物均构建并验证）**

## 产物

| 档位 | 路径 | 形态 | 验证 |
|---|---|---|---|
| 主产物（启动器档） | `dist/OKntePlanner/异环养成规划器.exe` | csc 编译的原生启动器（requireAdministrator 清单，控制台保留日志）+ 同目录 `app/ tests/ data/(表模板) 使用说明.txt`；启动器定位 Python（随包 `python\` → state.json 的 oknte_root → ok-nte 默认路径）后以 `python -m app.main` 拉起面板 | PE 头 MZ ✅；清单含 requireAdministrator ✅；**dist 内自测 26/26 + 冒烟 6/6（真实退出码 0）**；dist 保持模板干净（验证产生的 state/runtime/backups 自动清除） |
| 可选档（单文件） | `dist/OKntePlanner_single/OKntePlanner.exe`（7.7 MB）+ 随行 `data\` 表模板 | PyInstaller onefile，内嵌 ok-nte 的 Python 3.12 运行时 + 本应用，`--uac-admin` | 构建成功 ✅；**冻结环境数据目录探针 ✅**（数据落在 EXE 旁而非 `_MEIPASS` 临时目录） |

构建入口：`tools/build_exe.py`（项目根，任一 Python 3.10+ 运行；dist 验证自动使用本机 ok-nte 内嵌 Python）。可选档联网经腾讯 pip 镜像安装 PyInstaller 到 `tools/_build/`（**不向 ok-nte 的 Python 安装任何包**），任何失败只跳过、不阻断主产物。

## 本阶段的必要改动

1. `app/state.py`：**冻结环境感知**——`sys.frozen` 时 ROOT 取 `sys.executable` 所在目录，否则维持源码布局。修复 PyInstaller onefile 下用户数据会被写进随退出即删的临时解包目录的隐患（探针用例已固化该行为）。
2. `tools/build_exe.py`：构建/验证脚本（含 dist 内全量门禁与"模板干净"清理）。
3. dist 组装包含 `tests/`（selftest 的 E2E 依赖 `tests/fake_oknte.py`，dist 的自测能力是交付物的一部分）。

## "尽量封装"的边界（如实说明）

- **启动器档不是自包含 EXE**：仍需外部 Python（默认复用 ok-nte 内嵌 3.12；放入随包 `python\` 目录即完全脱依赖）。这是零第三方构建依赖、且不污染 ok-nte 的权衡。
- **单文件档是自包含的**（自带运行时），但实为"单文件 + 随行 data\ 表目录"；且 EXE 带 admin 标志，自动化验证只到探针级——完整拉起面板需真机双击（UAC）。
- 首次构建时发现并修复：dist 缺 `tests/` 导致 dist 自测 5 例失败（已纳入组装清单）。

## 门禁（真实退出码）

```
主工程：py_compile=0；app.selftest 26/26（exit 0）
dist：  app.selftest 26/26（exit 0）；app.smoke_test 6/6（exit 0）
```

## 未验证 / Manual PASS

- 真机双击两个 EXE（UAC 弹窗、浏览器自动打开、退出行为）——自动化无法触发 UAC。
- 单文件档在无 ok-nte 机器上的完整运行（本机构建机始终有 ok-nte）。
- 杀毒软件对 PyInstaller onefile 的误报可能（常见现象，如遇白名单即可）。

---

# 阶段11附录：修复"dist 下 EXE 均无法打开"（2026-10-25）

## 根因（按确定性排序）

1. **【用户实际踩中的主因】端口 8765 被百度输入法占用**（`BaiduPinyin.exe` 绑定 0.0.0.0:8765，`netstat`/`Get-Process` 实证）：旧版面板绑定失败直接异常退出，启动器窗口一闪而过——表现即"双击打不开"。两个 EXE 都是同一面板的两种启动方式，所以"都打不开"。
2. 启动器无任何诊断：子进程异常退出/端口占用直接结束，黑框闪退；.cs 为无 BOM UTF-8，csc（GBK 默认）下中文乱码。
3. 单文件档构建缺 `--add-data`，面板页没打进包（首页必 404）；且 PyInstaller 把入口脚本存为根级 `main`，冻结态 `__file__` 指向 `_MEIPASS/main.py`，即使补了资源 WEB_DIR 也差一层 `app`。

## 修复

| 项 | 内容 |
|---|---|
| 端口冲突自动避让 | `create_server(port, auto_range=20)`：8765 被占自动试 8766~8785，成功后写回设置并控制台醒目提示；整段耗尽才报可操作错误。**已在真实场景复测：面板自动起在 8766 ✅** |
| 启动器可诊断 | UTF-8 带 BOM + `/codepage:65001`（中文不乱码）；任何退出路径暂停窗口（消灭闪退）；写 `launcher.log`（解析到的 Python/退出码/异常栈）；Python 探测链补 PATH 回退（where python） |
| 面板 crash.log | main() 顶层兜底写 `crash.log`（frozen 落 EXE 旁），控制台+文件双通道 |
| 单文件资源 | `--add-data app/web/index.html → app/web`；main.py WEB_DIR 冻结感知（`sys._MEIPASS/app/web`，带存在性回退） |
| 构建自动验证 | 非管理员 shell 自动构建 asInvoker 测试变体真实拉起（API+首页双断言，失败保留现场 `tools/_build/probe_failure`）；**用户场景复测**（无端口 env、8765 被占 → 验证自动避让）；管理员 shell 时直接实跑正式双 EXE |
| 文档 | README 与 dist《使用说明.txt》新增"双击打不开排查"五步（UAC/杀软/日志/端口/Python） |

## 排查中纠正的误判（如实记录）

- 构建内变体探测一度全部"未就绪"，手工复现却正常——根因是**探测脚本自身**：`/api/bootstrap` 含 tasklist+PowerShell 进程检测（2~4 秒），探测单次超时只给 2 秒，每次都在服务端响应前放弃（服务端日志 ConnectionAbortedError 印证）。超时改为 10~15 秒后双变体全绿。EXE 本体自始至终正常。

## 验证（真实退出码）

```
主工程：py_compile=0；selftest 30/30（新增端口避让用例）；smoke 9/9
构建摘要：PE头✅ 清单✅ dist自测✅ dist冒烟✅
  启动器(asInvoker) 实跑 API+首页 ✅
  单文件(asInvoker) 实跑 API+首页 ✅
  端口冲突自动避让 ✅（8765 被百度输入法占用 → 面板自动起在 8766）
```

## Manual PASS（用户下一步）

双击 `dist\OKntePlanner\异环养成规划器.exe`：UAC 点"是"→ 窗口保持打开 → 浏览器自动打开面板（8765 被输入法占用时会提示改用 8766）。若再遇问题，看同目录 `launcher.log` / `crash.log`，并按 README"EXE 双击打不开排查"处理（杀软白名单等）。

> **✅ 2026-10-25 用户真机确认：双击打开成功（Manual PASS 通过）。** 阶段 11 就此收口——EXE 分发链路（启动器档 + 单文件档）全部验证完毕。
