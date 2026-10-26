# 上游兼容边界：ok-nte 依赖面清单

> 回答的问题：**如果 ok-nte 明天自动更新，规划器最可能在哪里坏？**
> 原则：检查我们依赖的**能力**，而不是比对版本号。ok-nte 更新只同步代码文件（repo → working），
> `working/configs/*.json` 是用户数据会被保留——但**键结构、CLI、日志文本**都可能随版本变化。

## 依赖面总表

| # | Dependency | 当前假设 | 使用位置 | 失效后果 | 是否检测 | 风险 |
|---|---|---|---|---|---|---|
| 1 | ok-nte 安装根目录 | 用户设置 `oknte_root` 指向有效安装 | `state.py` DEFAULT_SETTINGS；`oknte_adapter.py` OkNte.__init__ 派生全部路径 | 全部功能失效 | ✅ 指纹（间接：各路径检查） | 低（仅搬家才变） |
| 2 | 内嵌 Python 路径 `data/apps/ok-nte/python/python.exe` | PyAppify 布局不变 | `oknte_adapter.py` python_exe、run_command | 无法无界面执行 | ✅ 指纹 incompatible | 低 |
| 3 | 无界面 CLI `python -m ok run_task <任务名> -e` | ok 框架 CLI 存在且参数不变 | `oknte_adapter.py` TASK_CLI_ARGS | 无法执行任务 | ⚠️ 指纹只查 `working/main.py` 与 `working/ok/__init__.py` 存在，**不实跑 CLI** | 中 |
| 4 | 任务名 `DailyRoutineTask` | 类名/任务名不变 | `oknte_adapter.py` TASK_CLI_ARGS 硬编码 | CLI 找不到任务 | ❌ 未检测 | 中 |
| 5 | 配置文件 `working/configs/DailyRoutineTaskConfigs.json` | 文件名与位置不变 | `oknte_adapter.py` ROUTINE_CONFIG_NAME | 无法读写目标 | ✅ 指纹 incompatible | 低 |
| 6 | `daily_anomaly` 段 + 必需键（任务类型/目标消耗体力/循环模式）及类型 | 段与键名、类型不变 | `oknte_adapter.py` REQUIRED_DAILY_KEYS（指纹+写入双重校验） | fail closed，拒写 | ✅ 指纹 incompatible + 写入前再校验 | 中 |
| 7 | 目标键（具体奖励目标/异能材料序号/弧盘材料序号/空幕序号） | 键名被 ok-nte 读取 | `data/materials.json` targets[].config；写入差异白名单 INTENDED_DAILY_KEYS | 写入无效目标（ok-nte 忽略或报错） | ⚠️ 缺键时 compatible_with_warning | 中 |
| 8 | 配置值枚举（任务类型4选1、具体奖励目标3选1、循环模式="停用"） | 枚举字符串不变 | `data/materials.json`；`循环模式`写入"停用" | 写入非法值，任务行为未知 | ❌ 未检测（面板可校准 materials.json） | 中 |
| 9 | `daily_anomaly_hunter` 段（目标消耗体力/追猎目标） | 段存在且为对象 | `oknte_adapter.py` write_anomaly_config | 启用追猎预留时拒写（fail closed） | ✅ 缺失/类型错 → compatible_with_warning | 低 |
| 10 | 进程名 `ok-nte.exe`、`python.exe`/`pythonw.exe` + 命令行含 working 路径 | 启动器进程名与托管方式不变 | `oknte_adapter.py` is_running（tasklist + PowerShell CIM） | 误判运行状态：误报→拒绝写入；漏报→写后被覆盖 | ❌ 未检测（检测本身即该功能） | 中 |
| 11 | 日志文件名 `ok-script.log` / `ok-script.YYYY-MM-DD.log` | 框架日志命名与按日轮转不变 | `oknte_adapter.py` today_log_files、LogTail | 无法解析执行结果 → 步骤被误判 failed | ⚠️ 指纹警告日志目录缺失 | **高** |
| 12 | 日志文本模式（7 个正则 + 失败标记 + 行首日期前缀） | 中文日志文案不变：`开始任务:`、`选择项目序号: 第 N 个项目`、`双倍次数: X, 单倍次数: Y`、`info_set 体力消耗目标 N`、`ocr found used stamina N`、`任务执行完毕`、`跳过每日体力任务`、`体力不足`、`任务运行失败:` 等 | `oknte_adapter.py` RunMonitor、scan_used_stamina | 解析不到结果 → 误判失败/拉黑目标、当日已耗体力丢失 | ❌ 未检测 | **高**（最脆弱面） |
| 13 | `app.json` 的 `current_version` | 仅用于展示与变更提示 | `oknte_adapter.py` version()；runner 记录 known_oknte_version | 无功能影响 | — | 低（刻意不依赖） |
| 14 | 每轮体力 40 | 游戏常量（异象界域） | `data/materials.json` cost_per_run | 计划轮数错误 | ❌ 未检测（面板可改） | 低 |
| 15 | 系统命令：tasklist / taskkill / PowerShell（CIM、NotifyIcon） | Windows 自带 | `oknte_adapter.py`、`runner.py` | 进程检测/结束、通知失效 | ❌ | 低 |

## 分层结论

- **代码层已收敛**：所有上游交互集中在 `app/oknte_adapter.py`。`planner` 经参数注入的 oknte 对象只调 `scan_used_stamina`；`runner` 只调 adapter 方法；`main` 只调 `compat_check`/`is_running`；前端不直连。**无散布访问，本阶段未做也无须重构。**
- **数据层耦合集中在 `data/materials.json`**：上游配置键与枚举值以数据形式登记，面板可编辑——这是刻意的"数据而非代码"适配点。
- **最脆弱面是日志文本（#12）与 CLI 任务名（#4）**：它们无结构化契约，指纹无法静态证明。缓解：RunMonitor 解析不到任何已知标记时步骤判 failed 并进入当日拉黑（不会盲记账）；连续失败用户会在面板看到原因。ok-nte 更新后若任务行为异常，**优先核对日志文案**。

## ok-nte 更新后用户首查清单

1. 面板顶栏指纹标签：绿色=能力全部就绪；黄色=警告（悬停看原因）；红色=不兼容（规划器拒绝写配置，不会写坏）。
2. 用总览页"刷新计划"看 `/api/plan` 是否正常产出（覆盖日志读取路径）。
3. 若红/黄：打开 ok-nte GUI 确认日常任务仍可手动跑通 → 对照本文表格逐项核对（先 #6/#7 配置键，再 #12 日志文案，最后 #4 任务名）。
4. `data/backups/` 保存最近 20 份原配置，可随时回滚。
