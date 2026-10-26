# 阶段3结果：运行状态持久化、崩溃恢复和幂等性

**结论：PASS**

## 交付物

- `data/runtime.json`：runner 拥有的唯一运行时持久化（原子写，state/execution/last_execution）
- 启动对账 `Runner.recover_on_startup()`：面板启动时只读检测，绝不拉起/杀掉任何进程
- `execution_id` 生命周期：planned → launched → exited → accounted（accounted 以清空槽位表示，last_execution 留档）
- 未决执行（needs_reconciliation）：阻塞一切新执行，等待用户手动对账（`POST /api/reconcile`），面板总览页提供处理入口
- `app/state.py`：全项目唯一 JSON 原子写 `save_json_atomic`（tmp → 主文件轮转 .bak → 替换）+ 容错读 `load_json`（损坏留档 .corrupt、回退 .bak）
- 自测新增 `test_recovery_crash_matrix`（9 组崩溃场景）

## 状态分类表

| 状态 | 例子 | 拥有者/修改者 | 持久化 | 崩溃后恢复 |
|---|---|---|---|---|
| 配置状态 | settings / characters / inventory / yields / requirements | 用户（Web API 写入） | data/state.json 等，原子写 | 文件即真相；截断时 .bak 回退 |
| 派生状态 | deficit / plan / reason | planner 纯读计算 | 否 | 重算即得 |
| runtime 状态 | running/waiting/stopping、当前 execution | runner（唯一写者） | data/runtime.json，原子写 | 启动对账：见下表 |
| bookkeeping | today.used_stamina/planner_spent/blacklist/entries、history | runner（_settle/_reconcile_used_stamina）与对账 API | data/state.json | 文件即真相；未决执行补零值审计留痕 |

## 启动恢复决策表（recover_on_startup）

| 上次遗留 | 判定 | 结果 |
|---|---|---|
| 无 runtime 文件（首次启动） | — | 干净 stopped，无提示 |
| execution.phase=planned | 未证实拉起过 | 安全清除，无未决 |
| execution.phase=launched/exited 且 pid 存活（OpenProcess 探测，ACCESS_DENIED 按存活保守处理） | 面板崩时任务仍在跑 | 未决执行（挂起）+ interrupted；提示等它结束后处理 |
| execution.phase=launched/exited 且 pid 已死 | 结果未知（是否耗体力/出材料未知） | 未决执行 + interrupted；**阻塞 start 直到用户对账** |
| state=running/waiting/stopping 且无 execution | 执行间隙/等待中崩溃 | interrupted（可直接重新开始，不卡死） |
| runtime.json 损坏且无 .bak | 无法确知 | interrupted + 提示人工核对日志与背包（不猜测） |
| runtime.json 损坏但有 .bak | load_json 回退 | 按上表正常判定 |

## 双刷防护策略（核心问题："执行成功但记账前崩溃，下次会不会双刷？"）

不会。执行成功记账发生在 `_settle`（state.json）→ 随后才清空 execution 槽位（runtime.json）。在"子进程已退出但未记账"窗口内崩溃，下次启动必然看到 phase∈{launched, exited} + pid 已死 → 未决执行 → **start 被拒绝**（`存在未对账的历史执行…请先处理`），绝不在未知状态上自动重跑。子进程仍存活时同样未决 + 阻塞，且 `resolve_pending` 拒绝在进程存活时处理（防止过早解除阻塞）。对账动作绝不猜数字：`inventory_done`=用户已自行核对并校准库存（库存页），`discard`=放弃记账；两者都只补一条 **status=reconciled 的零值历史**（审计留痕），不把 expected_gain 当实际产出。

## 已存在、未重做的部分（证据）

| 简报要求 | 现状证据 | 处置 |
|---|---|---|
| 原子写统一 | state.py `save_json_atomic`/`load_json`（本阶段建成）；main.py:13 改为从 state 导入唯一实现 | 完成（main.py 旧私有 helper 已删） |
| 面板重启后检查真实进程 | runner.py `_pid_alive`（ctypes OpenProcess，无子进程开销） | 完成 |
| 手动重新同步库存 | 库存页（index.html）原本即可随时校准 | 已达标，未改 |

## 改动清单

`app/state.py`（统一原子写/容错读）、`app/runner.py`（runtime.json + execution_id 生命周期 + recover_on_startup + resolve_pending + pending 阻塞 start）、`app/main.py`（唯一原子写导入、`POST /api/reconcile`、启动时 recover 并打印）、`app/web/index.html`（总览页未决执行卡片）、`app/selftest.py`（恢复矩阵用例）。

## 测试（真实退出码）

```
py_compile app/*.py → 0
app.selftest        → 0，通过 15/15（含新增恢复矩阵）
app.smoke_test      → 0，通过 5/5
```

矩阵覆盖：首次启动；launch 前崩（planned 安全清除）；launch 后崩+pid 存活（未决+阻塞+活着不许对账）；exited 未记账（未决→discard 留零值审计历史→可重新开始）；waiting 中崩；stopping 中崩；runtime 损坏（有 .bak 走回退、无 .bak 上报 corrupt+interrupted）；state.json 截断回退 .bak 且损坏留档 .corrupt；双刷阻塞复检。

## 未验证的真实环境风险

- 真实断电/强杀（任务管理器 kill -9）未实测——测试用文件级截断与 pid 模拟覆盖等价逻辑，但内核级 torn-write 行为依赖 NTFS 下 os.replace 的原子性（Windows 上 4.4KB 内 JSON 单次 replace 原子，风险极低）。
- pid 复用：面板崩溃数小时后 pid 可能被无关进程占用 → `_pid_alive` 误报存活 → 未决一直挂起等用户处理（保守方向，不会双刷；用户可在对账卡片看到 pid 手动确认）。
- 恢复不重新接管仍存活的子进程日志监听（避免复杂化）：它结束后按"exited 未记账"处理，路径收敛。

---
*执行方式说明：子代理两次因提供方过载中断（已完成主体实现），状态分类/测试矩阵/本文档由主会话按同一简报补齐，验收口径不变。*
