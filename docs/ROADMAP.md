# 路线图（阶段计划，随执行更新）

纪律（六阶段系列延续）：阶段化、每阶段可验收、开始前先审计、已实现的给证据不重做、
禁止顺手重构、每阶段结束跑三项门禁（py_compile / app.selftest / app.smoke_test）+
本阶段测试 + Result 报告 + 未验证风险清单。

## 已完成

| 阶段 | 内容 | 结论 |
|---|---|---|
| 1 | 上游兼容边界与更新抗性 | PASS（docs/results/UPSTREAM-COMPAT-Result.md） |
| 2 | 规划算法正确性与可解释性 | PASS（PLANNER-CORRECTNESS-Result.md） |
| 3 | 状态持久化、崩溃恢复、幂等性 | PASS（RECOVERY-Result.md） |
| 4 | Dry Run / Simulation 模式 | PASS（DRY-RUN-Result.md） |
| 5 | E2E 验收与故障注入（A–J） | PASS，无 Blocking（E2E-ACCEPTANCE-Result.md） |
| 6 | 产品化缺口审计（只审计） | 完成（PRODUCT-GAP-AUDIT.md） |
| 7 | EXE 封装分发（启动器档+单文件档） | PASS（EXE-PACKAGING-Result.md；用户确认打开成功） |
| 8 | 实际掉落校准闭环 | PASS（YIELD-CALIBRATION-Result.md） |
| 9 | 体力恢复模型 + 未来多日预览 | PASS（STAMINA-PREVIEW-Result.md） |
| 10 | 配置导入导出 + schema + 备份回滚 | PASS（IMPORT-EXPORT-Result.md） |
| 11 | 修复 EXE 打不开（端口占用根因 + 可诊断性） | PASS（EXE-PACKAGING-Result.md 阶段11附录） |
| 12 | **桌面化 v2：独立桌面进程**（pywebview 原生窗口，OKntePlanner_single 为主推；v1 Edge 壳降为备用档并加 --no-proxy-server 根治代理截获"拒绝连接"） | PASS（DESKTOP-UI-Result.md v2 章节；用户双击 Manual 见文末） |
| 13 | **材料实名+角色/武器库+需求表 UI+追猎独立计划**（31项外部材料补全、显示名可编辑、库下拉、需求表表单化、追猎双按钮双额度独立记账/AnomalyHunter.json 最小补丁/日常开关 ensure） | PASS（LIBRARIES-HUNTER-Result.md） |
| 14 | **全角色/全武器养成数据落库**（game_data.json：27角色/50弧盘/物态→罐头系与属性→技能书系映射/养成总量预填；一键导入全库与按目录导入角色；需求表真实数值；coin 产量 fail-safe） | PASS（GAME-DATA-Result.md） |
| 15 | **体力上限320 + seelie式角色UI + 通用材料表**（stamina_cap 320/限额999+上游边界提示；满级快捷按钮；通用材料表 tab：通用量参考/角色映射/一键应用，共享映射真相，随导出迁移） | PASS（COMMON-COSTS-Result.md） |
| 16 | **P2 批次一：专属图标（纯stdlib ICO，EXE/任务栏）+ 历史页 14 天 SVG 图表 + 兼容报告一键生成**（含 Git 合并冲突全量修复插曲） | PASS（P2-BATCH1-Result.md） |
| 17 | **mysqil 权威数据落库**（浏览器抓取 DetailedCalc 逐级表：角色7段/技能逐级+天赋/弧盘7段/突破6段；鉴别7每局产量校准 coin=12万；25角色映射重写20名权威+技能书五系全齐含例外；白当量口径统一） | PASS（MYSQIL-DATA-Result.md） |
| 18 | **mysqil 核对 + 全模块审查修复 + UI 大改版 + 性能缓存**（wiki 逐字节核对与落库取列修正 mob_base_total 270→390；后端 16 处修复[3高/6中/7低]+2 处复核不改；前端 5 处存量 bug[未决按钮/GET plan/需求表编辑器/BOOT 竞态/fetch 超时]；侧边栏仪表盘 UI + toast/确认框组件系统；进程检测 5s 缓存首屏 ~6-8s→~3s；1888cad pathlib 迁移门禁认证通过；Mimosa 密封扫描 0 findings 但 inconclusive 已登记 G12） | PASS（2026-10-29-总结报告.md；PLAN-2026-10-29.md） |

## 路线图状态（2026-10-29 更新）

阶段 1–18 全部完成（阶段18：mysqil 权威核对+全模块审查修复+UI 大改版+性能缓存，详见
reports/2026-10-29-总结报告.md 与 docs/PLAN-2026-10-29.md）。当前形态：双击
`dist\OKntePlanner_single\OKntePlanner.exe` = 独立桌面应用（pywebview 原生窗口）；面板为
侧边栏仪表盘 UI（toast/确认框组件系统），支持 日常/追猎 双计划双按钮（独立额度独立记账）、
全量材料实名（可在数据表改名）、角色/武器库下拉（游戏目录一键导入 27 角色/50 弧盘）、
按目录导入角色自动预填、需求表表单编辑（真实数值预填）、通用材料表（通用量参考/映射/
一键应用）、体力上限 320 口径、图鉴数据页（阶段19：手动录入新角色/武器覆盖层 + wiki
养成分组拉取 + 新材料登记）。剩余 P2：背包 OCR、多来源材料效率排序、历史 duration/exit_code、
导出导入纳入图鉴覆盖层文件。WON'T DO 维持：读游戏内存/注入、复杂优化器、多账号并行。
