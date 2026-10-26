# 阶段13结果：材料实名补全 + 角色/武器库 + 需求表 UI + 追猎独立计划

**结论：PASS**

## 交付物（按用户四项需求）

### 1. 角色库 / 武器库 + 下拉选择
- state v2（schema=2，`_migrate` 兼容 v1）：`char_library[]` / `weapon_library[]`（手动增删，去重≤32字/≤200项）
- 角色页顶部两张库管理卡（输入+添加+标签删除）；角色行：名字改**下拉**（角色库；自定义名保留并标注"不在库中"）、新增**武器下拉**（武器库，可空）
- 库仅作为下拉名册（用户已确认），不参与需求计算；角色行武器选择随角色保存

### 2. 材料实名 + 突破材料补全（materials.json v2）
- 19 个体力本目标全部补 `display_name`（调研实名预填）+`source`（副本名）：技能书5系（军衔/雏鸟/帽子/同频/水波线）、罐头5系（戏剧芯/液态梦/苹果核/螺旋乐/冷点心=弧盘/武器突破）、空幕=兔子洞6节点（钟表把戏/雕塑长廊/经纬织机/守卫萝卜/心智地图/铁轨之夜）、猎人攻略/染剂/甲硬币
- 新增 `extra_materials[]`（31 项，只做库存/缺口追踪）：**BOSS掉落7种**（倾诉花种/水月色拨片/妄想彼端的一页/上膛骑士火花塞/海之泪/斑斓的票根/护巢残片，标注 BOSS 与 60体力/局）、**异象巡礼周本6种**、**小怪素材4系12种**（絮语/掠影/数符/幻妄）、井屑3种、无梦果核/谕石
- **所有显示名/来源可在『数据表→材料名称与来源』行内编辑**（`/api/tables/materials`），技能书/罐头系 1~5 号与游戏内的对应顺序未定论 → 预填调研名 + 面板显著提示"请按游戏内核对后改名"

### 3. 需求表 UI 化
- 数据表页 textarea 替换为**表单编辑器**：每条轨道一张卡（轨道名/上限可改），等级段行（from/to + 材料行：通用键6种[角色经验/弧盘经验/甲硬币/技能书/罐头/空幕·按角色所选系] + 全量实名材料下拉 + 数量，可增删段/材料行），保存重建 requirements.json（原子写）

### 4. 追猎独立计划（双按钮，ok-nte 两侧互斥）
- **事实基础**（ok-nte 源码核实）：追猎可完全独立跑 `run_task AnomalyHunter -e`；独立配置 `AnomalyHunter.json`（合法键仅 追猎目标+领取奖励次数）；60体力/局；7 BOSS 与角色突破材料一一对应；连续失败3次自停
- **规划**：`make_hunter_plan`（纯读/注入时钟）——额度=追猎每日额度(anomaly_hunter_stamina，语义改"追猎每日体力额度")−今日 hunter_spent；次数=fixed_runs覆盖 或 ceil(所选BOSS掉落材料缺口/每局产量8)，封顶 额度//60；no-op 枚举复用；BOSS 失败当日拉黑（`hunt:{boss}`）
- **执行**：runner `start(mode)` 双模式共用执行器（同时只跑一个）；追猎循环=写 hunter 配置（备份/白名单校验/原子写，未知键保留）→ `run_task AnomalyHunter -e` → RunMonitor 解析追猎汇总（`共计消耗体力: N`）→ 记账（inventory+gain、today.hunter_spent、history mode=hunter）→ 继续直至额度/缺口/指定次数耗尽
- **日常按钮**执行前 `ensure_routine_item("daily_anomaly", True)` 最小补丁 DailyRoutineTask.json（条目缺失/结构变更只警告不写）——修复了"无界面跑日常但日常子任务未启用则什么都不执行"的隐性缺口
- **总览页**：双计划卡（日常体力计划 / 追猎计划：BOSS下拉+缺口+自动局数+覆盖次数）+ 双开始按钮（▶ 开始日常 / 🎯 开始追猎）+ 状态行显示当前模式；日常计划不再扣追猎预留（独立额度独立记账，共用游戏体力池的口径差异已在面板注明）

## 改动清单

`data/materials.json`(v2)、`data/yields.json`(v2+hunter_yield)、`app/state.py`(schema 2)、`app/oknte_adapter.py`（write_hunter_config/ensure_routine_item/run_command_hunter/_backup_json 通用化/compat 警告）、`app/planner.py`（统一目录/EXTRA/HUNTER_BOSSES/display_name/make_hunter_plan/日常去预留/broken 目标警告）、`app/runner.py`（双模式/_hunter_loop/_execute_hunter_step/_settle_hunter/_launch_and_monitor 抽取/ensure_routine_item 调用）、`app/main.py`（/api/libraries、/api/hunter/plan、/api/tables/materials、yields hunter_yield、start mode、export/import 含库）、`app/web/index.html`（全部页面）、`tests/fake_oknte.py`（hunter 模式）、`app/selftest.py`、`app/smoke_test.py`。

## 测试（真实退出码）

```
主工程：py_compile=0；selftest 35/35（新增 目录完整性/追猎计划数学/hunter配置与日常开关补丁/追猎E2E）；smoke 10/10（新增 库+追猎计划步骤）
dist 重建：dist 内自测+冒烟通过；双变体实跑 API+首页 ✅（原生窗口/Edge 档不变）
```

追猎 E2E：fake ok-nte 以真实追猎日志格式（`共计消耗体力: 120`）全链路跑通——写入 AnomalyHunter.json（海囚×2）→ fake 退出 → 记账 hunter_spent=120、库存海之泪+16、history mode=hunter；同执行器互斥验证。

## 未验证 / Manual PASS

- 真实追猎一次（需游戏在场）：验证 ok-nte 对 `领取奖励次数` 的实际语义（按 50 级每局 8 个掉落校准 hunter_yield）
- 技能书/罐头系 1~5 号与游戏内列的对应顺序：用户按游戏内核对后在『数据表→材料名称与来源』改名
- 日常按钮首次实跑：验证 ensure_routine_item 对真实 DailyRoutineTask.json 的补丁效果
