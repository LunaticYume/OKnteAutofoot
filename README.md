# 异环养成规划器（OKnteAutofoot）

第三方 ok-nte 的外置规划器：按**角色练度 → 材料缺口 → 每日体力限额**动态规划每天刷什么、
每次刷多少，自动写入 ok-nte 配置并无界面代跑，全程**不修改 ok-nte 任何源码**，ok-nte
自动更新后本工具依然可用（更新只同步代码文件，配置 `working/configs/*.json` 是用户数据会被保留）。

## 快速开始

1. **发布形态（唯一受支持，独立桌面版）**：双击 `dist\OKntePlanner_single\OKntePlanner.exe`——launcher 壳设好运行缓存后拉起同目录主程序 `OKntePlanner_main.exe`，直接弹出一个**独立应用窗口**（自己的进程与任务栏项，无黑框、无浏览器、无 Edge 壳），关窗口即整套退出。launcher 与主 EXE 必须同目录，勿拆开分发。
   **deprecated（勿用）**：`dist\OKntePlanner\异环养成规划器.exe`（启动器版）——连续多版本空白页，2026-11-21 起弃用：仍随构建生成、仅作中间产物，不作为发布候选/不计 release gate。源码方式 `run_panel.bat`（浏览器打开面板）仍可用于开发调试。
2. **角色页**：新建角色 → 选择对应的异能/弧盘/空幕材料序号 → 填当前/目标练度
   （需求表没数据时，直接在"直接材料需求"里填还差的数量即可）。
3. **库存页**：按背包实际情况填一次现有材料数量，之后由规划器自动累加。
4. **数据表页**：校准"每轮产出"（掉落随世界等级变化，跑几次后按实际掉落回填）。
5. **总览页**：先点 **👁 预演（不落盘）** 看将写入 ok-nte 的配置差异 → 点 **▶ 开始执行**。规划器会：写配置 → 无界面拉起 ok-nte 日常任务 → 监听日志 → 扣库存/记历史 → 自动切换下一个目标，直到额度用尽或全部达标。

## 它是如何工作的（不侵入 ok-nte）

| 环节 | 机制 |
| --- | --- |
| 选择目标 | 写 `working/configs/DailyRoutineTaskConfigs.json` → `daily_anomaly`（任务类型/序号/目标消耗体力，循环模式置"停用"，切换由规划器接管） |
| 执行 | 用 ok-nte 内嵌 Python：`python -m ok run_task DailyRoutineTask -e`（无界面，跑完退出） |
| 反馈 | 解析 `working/logs/ok-script*.log`（OCR 今日已耗体力、所选目标、完成/失败标记） |
| 安全 | 写入前自动备份原配置到 `data/backups/`（保留最近 20 份）；ok-nte 正在运行时拒绝写入；每次启动做配置结构兼容校验，ok-nte 更新改了键名会直接报错而不是写坏文件 |

## 规划规则

- **每日额度** = 每日体力限额 − 今日已耗体力（从日志 OCR 与本地记录取大值，一天内多次点开始也只刷剩余额度；异象追猎预留可选）。
- **目标选择** = 角色按面板顺序（上→下）逐个补齐；角色内按 角色/弧盘经验 → 异能 → 弧盘突破 → 空幕 的规范顺序。
- **每次执行只刷一个目标**（ok-nte 的模型），完成自动切下一个；目标失败重试一次，再失败当日拉黑并换下一个。
- 注意：ok-nte 在**当日活跃度达标（180 体力 / 100 活跃）后会自动跳过**体力本，因此每日限额填超过 ~180 的部分通常刷不出来。

## 持续执行模式

设置页默认开启『持续执行模式』：点一次 **▶ 开始执行** 后规划器会**一直执行**——当日额度用尽或全部目标达标时等待次日自动重置后继续（跨天持续）；遇到 ok-nte 正在运行、游戏内体力不足、失败目标被拉黑后无可执行等繁忙情形时，**每隔『繁忙重试等待』秒（默认 300，可设 0~3600）自动重试**。等待期间总览页会显示等待原因与距下次重试的倒计时，**■ 停止 按钮随时可以立即中断**。兼容性检查失败仍然直接报错结束，绝不盲写配置；关闭该模式则保持一次性行为（当日收工即结束，ok-nte 正在运行时点开始会被拒绝）。

## 需要校准的数据

- `data/yields.json`（面板可改）：每种副本每轮 40 体力的掉落数量，默认是估计值。
- `data/requirements.json`（面板可改）：等级段 → 材料需求表；填好前可用角色的"直接材料需求"替代。
- 库存随时可在面板手动校准，产量误差不会累积。

## 已知边界

- 规划器运行期间请**完全退出 ok-nte**（含托盘），否则其内存态会把外部改动覆盖回去。
- 代跑使用 ok-nte 自己的无界面入口，游戏分辨率/语言等要求与 ok-nte 相同。
- 稀有突破材料部分来自周本/大世界（异象追猎、采集），体力本只能覆盖异象界域产出，规划器不会替代这部分。

## 目录说明

```
run_panel.bat      一键启动（UAC 自提权）
tools/build_exe.py 打包为 EXE（dist/OKntePlanner_single 单文件档=发布形态，launcher 壳 + 主 EXE；dist/OKntePlanner 启动器档 deprecated，仅中间产物）
app/               Python 源码（纯标准库，无第三方依赖）
  main.py            Web 面板服务 (127.0.0.1:8765)
  oknte_adapter.py   ok-nte 对接层：配置/CLI/日志
  planner.py         缺口计算与当日计划
  runner.py          单循环执行器
  state.py           状态持久化
data/               源码方式的数据位 + 出厂数据表（materials/yields/requirements/
                     game_data/mysqil 等；EXE 方式运行时用户数据不在此处，见下）
  materials.json     副本目标定义（与 ok-nte AnomalyTask 一一对应）
  yields.json        每轮产出表（可校准）
  requirements.json  养成需求表（可校准）
  state.json         角色/库存/历史（源码方式）
  backups/           ok-nte 配置自动备份
```

> **EXE 方式运行时的数据位置**：用户数据（角色/库存/设置/图鉴覆盖层/塔吉多凭证/OCR
> 标定等）统一在 `%LOCALAPPDATA%\OKntePlanner\data`——两档 EXE 共用同一份，
> 可整体备份/迁移；**勿同时双开两个 EXE**（并发写会互相覆盖）。EXE 旁 `data\` 仅为
> 出厂数据表模板。旧版（数据在 EXE 旁 `data\`）首次启动自动整树迁移且原目录原样保留；
> 手动迁移/合并可用 `python -m tools.migrate_user_data --from <旧数据目录> --apply`。
> 以其他管理员账户提权运行时，数据落提权账户的 LocalAppData（与原账户不共享）。
>
> **运行缓存位置（W4b-R3，2026-11-21 起迁出 C 盘）**：打包态的日志与 WebView 缓存
> 统一在 `E:\OKntePlannerRuntime`——`logs\`（panel.log、ok-nte-live.log、
> ok-nte-headless-*.log、elev_helper_audit.log）、`webview\`（WebView2 缓存与 Edge
> 档 edge_profile）、`crash\`（crash.log）。E 盘不可写时自动回落既有位置（用户数据
> 目录/EXE 旁）并在日志告警一次；可用环境变量 `OKNTE_RUNTIME_ROOT` 指定其他位置。
> 单文件档解包目录为 EXE 旁 `runtime_tmp`（launcher 壳设 TEMP/TMP 所致，解包内容
> 退出自清；目录建不出来时回落系统 %TEMP%，launcher.log 有告警行）。

## EXE 双击打不开 / 闪退排查

1. **UAC 弹窗**要点"是"（面板需要与 ok-nte 同级的管理员权限）。
2. **杀毒/SmartScreen 拦截**：EXE 为本机构建的未签名程序，被拦截时加入信任/白名单。
3. **窗口一闪而过**：看日志——`launcher.log` 在 EXE 旁（单文件档 launcher 壳：记录运行缓存设置与主程序退出码；deprecated 启动器档：记录解析到的 Python 与退出码）；`panel.log` 与 `ok-nte-live.log`/`ok-nte-headless-*.log` 在 `E:\OKntePlannerRuntime\logs`（E 盘不可写回落 `%LOCALAPPDATA%\OKntePlanner\data`）；`crash.log` 在 `E:\OKntePlannerRuntime\crash`（回落 EXE 旁）；`elev_helper_audit.log`（提权审计）在 logs 目录（回落用户数据目录）。启动器任何退出路径都会暂停窗口显示原因。
4. **端口占用**（8765 被旧面板实例占用）：关闭旧实例，或改 `state.json` 的 `port`——EXE 方式改 `%LOCALAPPDATA%\OKntePlanner\data\state.json`，源码方式仍改 `data\state.json`。
5. **找不到 Python**：放一份完整 Python 3.10+ 到 EXE 目录 `python\` 子目录，或安装 ok-nte（复用其内嵌 Python），或把 python.exe 加入 PATH。启动器依次探测：EXE 旁 `python\` 子目录 → EXE 旁 `data\state.json`（旧布局）与用户数据目录 `state.json` 里的 `oknte_root` → 默认安装位置 → PATH。
