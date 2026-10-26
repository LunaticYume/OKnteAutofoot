# 阶段5结果：真实环境 E2E 验收与故障注入

**总结论：Automated PASS（无 Blocking Issue）**

做法：自制可配置 fake ok-nte 子进程（`tests/fake_oknte.py`），由 runner 以真实方式拉起（写配置→Popen→LogTail→RunMonitor→记账全链路真实，仅"游戏操作"为假、日志格式与 ok-nte 完全一致）；测试通过 `_E2EOkNte`（真实 OkNte 子类，仅替换 `run_command`）注入。全程不碰真实 ok-nte 目录、不启动游戏。

## Automated PASS（逐场景证据）

| 场景 | 结论 | 关键断言（用例 test_e2e_*） |
|---|---|---|
| A 正常执行 | ✅ PASS | 恰好启动 1 次子进程；配置写入 具体奖励目标=甲硬币/80体力/停用；记账 used=80、库存+14、history runs=2；runtime 生命周期收口 accounted |
| B ok-nte 已运行 | ✅ PASS | force_busy 下零启动；waiting 原因含"ok-nte 正在运行" |
| C 停止 | ✅ PASS | waiting 停止 <5 秒返回 done；running 停止后子进程 pid 确认死亡（契约见下） |
| D 体力不足 | ✅ PASS | waiting 原因"体力不足"；重试重写配置字节级幂等；不记账不加库存 |
| E 额度耗尽→次日恢复 | ✅ PASS | 日限 40：第 1 天 1 轮后等待"额度"；次日额度恢复自动续跑并"达标"，库存补齐 |
| F 跨 reset hour | ✅ PASS | 注入时钟：03:59→前一天、04:00→当天（两个 planning day） |
| G 上游配置被改 | ✅ PASS | 删 任务类型 → error"兼容性检查失败"；原配置一字节不动；零启动 |
| H 异常退出防 busy loop | ✅ PASS | 持续体力不足下多次重试，相邻两次拉起间隔 ≥ busy_retry_seconds(2s)（实测断言 ≥1.5s） |
| I planner 数据异常 | ✅ PASS | 负需求/负库存不崩溃；yield=0、cost_per_run=0、缺 config 均 fail closed 为 invalid_material_config/警告跳过，绝不除零 |
| J 3 日长跑 | ✅ PASS | 注入时钟跨 3 个 planning day：2 目标 3 次执行（含当日目标切换）→ 全部达标后跨天零拉起；循环健康等待（无死锁）；库存精确补齐 {coin:21, exp_char:7}；history ≤500；state.json 持续可加载；拉起间隔无高速循环 |

**停止契约（文档化）**：本设计为 **B——停止即终止子进程**（stop → taskkill /T /F 树杀 ok-nte 子进程），非"允许当前轮完成"。这是既有行为的明确化；如需 A 契约（温和停止）属于未来需求，未在本阶段引入。

## 本阶段发现并修复的缺陷（最小修复，共 3 处）

1. **[高危·记账] RunMonitor 双倍语义错误**（oknte_adapter.py）：ok-nte 的 `双倍次数: X, 单倍次数: Y` 中一次双倍领取消耗 **2 个** 40 体力单位（AnomalyTask: `double_count = units // 2`），原解析按 X+Y 计单位数，导致实际体力消耗被**低估最多 50%**（80 体力的任务只记 40）。E2E fake 忠实复刻真实日志格式后暴露。修复：`units = 2×双倍 + 单倍`；既有解析用例期望同步修正（2双+3单=7 单位=280 体力）。
2. **[中危·健壮性] planner 除零与执行层 KeyError**：材料表被手改为 `cost_per_run=0` 时 `budget // COST_PER_RUN` 除零崩溃；目标缺 `config` 键时执行层 KeyError。修复：两处 fail closed（invalid_material_config + 面板可见警告），不改任何既有语义。
3. **[测试资产] LogTail 基线依赖文件存在**：fake 根目录日志文件不存在时首段日志被当历史跳过——测试装配预创建日志文件（生产无此问题：ok-nte 的日志文件始终存在）。

## Manual PASS（必须真机验证，建议顺序）

1. 关闭 ok-nte → 面板「👁 预演」确认 diff 合理 → 「▶ 开始执行」→ 观察真实拉起、游戏内实际刷本、日志解析（重点：真实日志的"双倍/单倍"行与记账体力是否一致——本阶段修复的语义需真机复核一次）。
2. 真实 ok-nte 更新后：面板指纹标签 + 预演（对照 docs/UPSTREAM_COMPATIBILITY.md 首查清单）。
3. 真实体力不足/额度耗尽场景的等待与自动恢复（游戏内体力真实恢复节奏）。

## NOT TESTED（环境限制）

- 真实游戏端到端（游戏不可启动，见 Manual PASS）。
- 单次运行超时路径（max_run_minutes ≥1 分钟 = 60 秒实测过慢；kill 树逻辑已被 C 场景以 hang 模式等效覆盖）。
- 面板进程在子进程运行中被真实强杀（崩溃恢复矩阵以文件级截断 + pid 模拟等价覆盖，见 RECOVERY-Result）。
- PowerShell CIM 在受限组策略下的 is_running 误报率。

## Blocking Issues

**无。** 上述 3 处缺陷已修复并回归（26/26）。

---
*门禁：py_compile=0；selftest 26/26（exit 0）；smoke 6/6（exit 0）。执行方式：子代理因提供方并发限制失败，本阶段由主会话按同一简报执行。*
