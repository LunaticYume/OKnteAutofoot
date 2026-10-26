# 阶段2结果：规划算法正确性与"为什么刷这个"解释系统

**结论：PASS**

## 一、统一资源模型（审计后：已可表达，未改结构）

材料数据当前即可表达简报要求的全部字段：

| 字段 | 现有表达（证据） |
|---|---|
| material_id | `data/materials.json targets[].id`（planner.py:25 `TARGETS`） |
| display_name | `targets[].name`（planner.py:109） |
| inventory | `STATE.inventory`（planner.py:92） |
| target | 由角色需求聚合：`character_needs`（planner.py:70，tracks 换算 `_ladder_cost`:53 + extra_needs）→ `snapshot` 聚合 total_needs（planner.py:90-100） |
| deficit | `gap = max(0, need - have)`（planner.py:106、123） |
| source/activity | `targets[].config`（写入 ok-nte daily_anomaly 的副本目标）+ `category`（materials.json） |
| cost_per_run | `materials.json "cost_per_run"` → `COST_PER_RUN`（planner.py:24） |
| expected_yield_per_run | `yields.json per_run[mat_id]` → `yield_of`（planner.py:39），单值用户可校准平均，无概率模型 |

多来源：当前每材料单来源（一个 target 条目即一个副本来源）；**允许多来源**的表达路径是新增 target 条目，初期单值平均已满足，未强加结构变更。效率维度数据在位（yield_per_run + cost_per_run 可计算单位体力产出），未实现优化器。

## 二、多目标优先级（三层确认）

1. **priority（用户手动）**：角色列表顺序即手动优先级（UI 有"列表顺序 = 补齐优先级"文案，index.html 角色页；`_ordered_gaps` 按角色序展开，planner.py:152）——已存在，确认未改。
2. **urgency**：当前无 deadline 功能，**不加**（简报明确）。
3. **efficiency**：同一材料多来源时按单位体力产出排序——当前每材料单来源，排序退化为稳定顺序（材料表规范序）；数据字段（yield_per_run/cost_per_run）已在 step.reason 中表达，保持确定性可解释 heuristic，禁止复杂优化器。

## 三、计划算法（已存在部分未重做，缺陷已修）

**已存在、未重做（证据）**：deficit=max(target−inventory,0)（planner.py:106/123）；轮次向上取整 `math.ceil(gap/yield)`（planner.py:114）；额度=限额−已耗（make_plan 内）；每轮体力 COST_PER_RUN=40；每日限额 daily_stamina_limit；追猎预留扣减；enabled=false 角色跳过（snapshot，planner.py:96-97）；当日黑名单跳过；产量为0=不可执行并警告——以上语义全部保留原实现。

**本阶段修复的缺陷**：
1. **make_plan 副作用**：原 `STATE.ensure_today()`（改写 state+save）与 ok-nte 扫描后直接写 `STATE.today["used_stamina"]` 并 `STATE.save()`。现在：`make_plan(oknte=None, now=None)` 纯读——`_today_view(day)`（planner.py:143）跨天时返回空当日结构不改 STATE；扫描值只取 max 参与本次计算并写入 `detail.stamina_used_note`；**持久化显式归属 runner**：`_reconcile_used_stamina(oknte)`（runner.py:42）在主循环每次规划前落账（runner.py:165），日志可见"已按 ok-nte 日志校准今日已耗体力 → X"。跨天的 STATE 推进归 `/api/bootstrap` 的 ensure_today 与 runner 循环内 ensure_today（显式入口）。
2. **时间未注入**：`now` 参数注入，day 一律由 `STATE.day_key(now)` 计算（含 4 点重置窗口）；测试全部注入固定时间。
3. **no-op 只有字符串**：升级为枚举（见下）。

## 四、no-op 原因枚举与分层

`NO_OP_REASONS`（planner.py:133），plan 返回 `no_op_reason`（枚举）+ `reason`（人类可读中文文案，UI 直接显示）+ `detail`（结构化）：

| 枚举 | 中文文案 | 触发（确定性判定顺序） |
|---|---|---|
| no_enabled_targets | 没有启用的角色（或启用角色没有材料需求） | 1. 无启用角色或全部无需求 |
| all_targets_completed | 所有目标已达标，今日无需补齐 | 2. 角色视角缺口全为 0 |
| daily_limit_reached | 今日体力额度已用完 | 3. 预留后可规划额度 ≤ 0 |
| insufficient_stamina | 剩余体力不足一轮（40） | 4. 0 < 额度 < 40 |
| invalid_material_config | 有缺口的材料产量未校准（为0）… | 5a. 过滤后无步骤且存在产量0（同时出 warnings） |
| waiting_for_reset | 有缺口的材料今日均已拉黑… | 5b. 过滤后无步骤且全部被拉黑 |

**分层说明**：`upstream_incompatible` 不在 planner 枚举中——runner 在 `_run_loop` 开头 compat_check 为 incompatible 时直接 error 终止（runner.py:123-126），不会进入规划；预览层由 `/api/bootstrap` 的 compat 三态标签单独呈现。判定顺序本身也是行为修正：目标全部达标且额度恰好用尽时，旧版误报"额度已用完"，新版正确报 all_targets_completed。

## 五、结构化 reason（planner 生成，UI 不重算）

- **每步** `steps[].reason`：material / display_name / target / inventory / deficit / yield_per_run / runs_needed / cost_per_run / budget_before / runs_planned / **text**（中文整句：角色、目标数、库存、缺口、每轮产出与消耗、补齐轮数、剩余额度、本次轮数与预计产出）。
- **计划整体** `detail`：code / text / day / stamina_limit / used_stamina / stamina_used_note（已耗体力取本地还是 ok-nte 扫描及其数值）/ hunter_reserved / budget_total / budget_remaining / targets_with_gap / steps_count / planned_runs / planned_stamina / skipped_blacklisted / skipped_zero_yield。
- 守恒不变量：`planned_stamina + budget_remaining == budget_total`（测试断言）。

## 六、改动清单

| 文件 | 改动 |
|---|---|
| app/planner.py | NO_OP_REASONS 枚举；`_today_view` 纯读当日视图；`_ordered_gaps` 附带 need/have/yield；`make_plan(oknte=None, now=None)` 重写：纯读/确定性/now 注入/枚举 no-op/步骤与整体结构化 reason；删除内部 STATE 写入 |
| app/runner.py | 新增 `_reconcile_used_stamina`（扫描落账，显式归属执行器）；主循环规划前调用并日志可见；`make_plan(oknte, now=datetime.now())` |
| app/selftest.py | 既有 test_planner_snapshot_and_plan 升级（固定注入 NOW、补 no_op_reason 断言，原语义断言全部保留不放松）；新增 4 用例：test_planner_algorithm_matrix / test_planner_step_and_plan_reason / test_planner_determinism_and_purity / test_runner_reconcile_used_stamina |
| app/smoke_test.py | /api/plan 断言升级：必含 no_op_reason；空计划必须带枚举+文案；每步必带 reason.text |
| app/web/index.html | 总览页（最小改动）：计划表新增"为什么刷"列显示 reason.text（悬停全文）；空计划显示枚举 tag+文案；新增额度构成行（detail：已耗来源/限额/追猎预留/可规划/安排/剩余/拉黑跳过） |

/api/plan 原有字段（day/used_stamina/budget/steps/warnings/reason）全部保留，新增字段向后兼容。

## 七、测试结果（真实退出码）

```
python -m py_compile app/*.py   → 0（COMPILE_OK）
python -m app.selftest          → 0，通过 14/14（原 10 用例 + 新 4 用例）
python -m app.smoke_test        → 0，通过 5/5
```

新用例覆盖：目标已完成 / 缺1个 / 非整除向上取整(26/7→4轮) / 体力刚好够(80=2轮) / 体力差1(79→1轮) / 多目标排序(角色优先) / 同优先级顺序(材料表序) / 每日额度耗尽 / 预留体力(总消耗≤预留后预算) / disabled角色 / 全disabled→no_enabled_targets / 黑名单 / 全拉黑→waiting_for_reset / 产量为0→invalid_material_config+警告 / insufficient_stamina(39<40) / 全部完成。
不变量（invariant）：库存≥目标绝不安排；总消耗≤限额−已耗−预留；同输入两次 make_plan 完全相同；第二次不改 STATE 且 state.json 字节不变（含 oknte 扫描路径）；扫描值不回写 STATE；planner 调用前后 STATE.data 深拷贝一致；额度守恒 planned_stamina+budget==budget_total；now 注入决定 day（03:00<4点重置→前一日窗口）。
数据零污染：_StateGuard 备份/恢复 data/state.json（运行后确认无遗留）；ok-nte 目录只读、全部用 tempfile 伪造。

## 八、未验证风险

1. **多来源 efficiency 排序未实测**：当前每材料单来源，"同材料多来源按单位体力产出排序"仅有数据表达（reason 字段），无实现与测试——引入第二来源时需补排序用例。
2. **扫描与本地记录的并发竞争**：make_plan 纯读扫描与 runner 落账之间存在毫秒级窗口（执行间隙 ok-nte 日志增长），表现为下一次计划多算 used，方向保守（少刷不多刷），未在真实环境观察。
3. **跨天预览视图**：跨天后 `/api/plan` 预览按空当日计算（纯读不推进 STATE），直到 bootstrap/runner 显式 ensure_today 才落新一天——面板先调 bootstrap（会推进），实际不可见差异，但纯 API 消费者需知晓。
4. **ok-nte 真实 OCR 误读**：扫描取当日日志最大值，OCR 单次异常偏大值会永久抬高 used（clamp 到限额内，最多当日少刷）；需真实环境观察。
5. urgency/deadline 分层未实现（简报明确：无 deadline 功能不加）。
