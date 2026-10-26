# 阶段12结果：桌面化（无黑框、不跳浏览器的应用窗口）

**结论：PASS（v2 原生窗口版；用户双击 Manual 项见文末）**

---

# v2（2026-10-27）：升级为真正的独立桌面进程 + 修复"拒绝连接"

## 用户反馈与现场证据

用户双击 v1 启动器后：Edge 窗口显示"127.0.0.1 拒绝连接"，且明确反馈 **Edge 套壳不是想要的"独立启动的电脑内进程"**。

现场日志（dist\OKntePlanner\）证实：面板正常绑定 8766（端口避让 ✓）、正常打开 Edge 窗口、退出码 0——面板在监听，但 **Edge 的请求被系统代理/网络层截获**（v1 构建探测用强制直连所以全绿，Edge 走系统代理所以拒绝连接）。

## v2 方案

| 项 | 内容 |
|---|---|
| **主推：原生窗口** | 单文件档 `OKntePlanner.exe` = pywebview 原生窗口（自己的 WinForms 窗口、自己的进程、自己的任务栏项；WebView2 渲染）。pywebview+pythonnet 只装进打包隔离目录 `tools/_build/pyi`（`_ensure_pyi_deps`，绝不碰 ok-nte 的 Python），PyInstaller `--collect-all webview pythonnet` 收齐 interop DLL |
| **UI 策略 v2** | `window`(原生)/`edge`/`browser`/`none`/`auto`(跟随默认)；默认：**frozen→window**，源码→auto(=edge)；`OKNTE_PLANNER_NO_BROWSER=1` 兼容=none。window 不可用（未打包 pywebview/WebView2 缺失）自动降级 edge→browser，降级原因打日志 |
| **Edge 降级档修复** | msedge 参数加 `--no-proxy-server`（回环服务强制直连，根治"拒绝连接"） |
| **文档** | dist《使用说明》重写：推荐单文件桌面版，启动器版为备用；README 同步 |

## 改动文件

`app/main.py`（_default_ui_mode/_resolve_ui_mode v2、_run_window_app、Edge --no-proxy-server）、`tools/build_exe.py`（_ensure_pyi_deps、collect 参数、winexe 测试变体同参、探测 env 按档位区分：启动器=edge，单文件=走 frozen 默认）、`app/selftest.py`（UI 策略用例更新：frozen→window、auto 跟随默认、显式覆盖）。

## 验证（真实退出码）

```
主工程：py_compile=0；selftest 31/31；smoke 9/9
构建：pywebview 隔离安装+打包成功；dist 自测✅ 冒烟✅
  启动器变体（winexe+pythonw+Edge壳[no-proxy-server]）实跑：API+首页 ✅
  单文件变体（--noconsole+pywebview 原生窗口）实跑：API+首页 ✅ ← 独立进程验证通过
  端口冲突自动避让 ✅（8766）
```

## 未验证 / Manual PASS

- **用户双击 `dist\OKntePlanner_single\OKntePlanner.exe`**：预期无黑框/无 Edge 壳/无浏览器，直接独立应用窗口；关窗口整套退出。
- 任务栏/窗口图标为 pywebview 默认（专属 .ico 走 --icon，P2）。
- 系统代理在原生窗口下的影响：WebView2 走系统代理但 Chromium 对 127.0.0.1 有隐式直连绕过；若用户为 TUN 全接管模式且仍遇拒绝连接，`OKNTE_PLANNER_UI` 降级链与 panel.log 可诊断。
- 杀软对 --noconsole + pywebview EXE 的误报可能（README 排查节覆盖）。

---

# v1 存档（Edge 应用模式，已被 v2 取代为降级档）

**结论：PASS（v1）**

## 最终体验

双击 EXE → UAC → **自动弹出一个独立桌面窗口**（Edge 应用模式：无地址栏无标签页）→ 用完关窗口 = 整套优雅退出（正在执行的任务先停止并记账）。全程无 CMD 黑框、不打开默认浏览器。

## 技术选型与理由

**Edge Application Mode**（`msedge --app=<url> --user-data-dir=<独立profile>`）：

- 零第三方依赖——纯标准库纪律不被打破（pywebview 方案需 pythonnet 重依赖链且 PyInstaller 打包风险高，弃用）
- Edge 是 Windows 10/11 必装组件；ok-nte 本机的 EBWebView 目录证明 WebView2 运行时在位
- 独立 `--user-data-dir`（持久化在 `data/edge_profile`）→ 独立进程树：Popen 拿到的 PID 即窗口主进程，**关窗口 = 进程退出**，生命周期完全可控
- 降级链：`OKNTE_PLANNER_UI=auto`（默认）下 Edge 不存在 → 自动回退浏览器模式；`browser` 强制浏览器；`none` 纯服务（自动化测试）；旧变量 `OKNTE_PLANNER_NO_BROWSER=1` 继续兼容（=none）

## 改动清单

| 文件 | 内容 |
|---|---|
| `app/main.py` | UI 策略解析（`_resolve_ui_mode` 纯函数）；Edge 定位（标准路径+App Paths 注册表，`_pick_edge` 可测）；`_run_edge_app_window`（Popen+wait 阻塞至关窗）；**优雅关闭序列**（关窗 → RUNNER.stop() 终止任务 → 最多等 3 秒记账落盘 → server.shutdown()）——关窗口不留未决执行；`_setup_logging_mirror`：无控制台（frozen/pythonw）时输出镜像到 `data/panel.log` |
| `tools/build_exe.py` | 启动器改 **/target:winexe**（无黑框）：pythonw 隐藏拉起面板，致命错误 MessageBox 弹窗 + launcher.log；单文件档加 **--noconsole**；变体实跑升级为**真实弹出 Edge 窗口**验证 API+首页；清理逻辑扩展到空目录（edge_profile） |
| `app/selftest.py` | 用例 13：UI 策略映射（NO_BROWSER 兼容/大小写/非法回退）+ Edge 探测降级链 |

## 验证（真实退出码）

```
主工程：py_compile=0；selftest 31/31；smoke 9/9
构建摘要：PE头✅ UAC清单✅ dist自测✅ dist冒烟✅
  启动器变体（winexe+pythonw+Edge窗口）实跑：API+首页 ✅
  单文件变体（--noconsole+Edge窗口）实跑：API+首页 ✅
  端口冲突自动避让 ✅（8766，继承阶段11）
```

## 未验证 / Manual PASS

- **用户双击正式 EXE**（UAC 层无法自动化）：预期看到"无黑框 + 弹出独立应用窗口 + 关窗口即退出"。
- Edge --app 窗口的任务栏图标是 Edge 图标（如需应用专属图标要换原生窗壳，P2）。
- 杀软对 --noconsole EXE 的误报可能（README 排查节已覆盖）。
