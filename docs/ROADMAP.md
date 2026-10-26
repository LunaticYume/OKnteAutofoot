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

## 路线图状态（2026-10-27 更新）

阶段 1–15 全部完成。当前形态：双击 `dist\OKntePlanner_single\OKntePlanner.exe` = 独立桌面
应用（pywebview 原生窗口）；面板支持 日常/追猎 双计划双按钮（独立额度独立记账）、全量
材料实名（可在数据表改名）、角色/武器库下拉（游戏目录一键导入 27 角色/50 弧盘）、
按目录导入角色自动预填、需求表表单编辑（真实数值预填）、通用材料表（通用量参考/映射/
一键应用）、体力上限 320 口径。剩余 P2 项（应用专属窗口图标、历史图表、多来源材料效率
排序、兼容报告、背包 OCR、逐级分段表与 1.5 新角色数据更新）待排期。
