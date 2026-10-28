# 阶段18-19 + 图鉴数据页 Result 报告（2026-10-29）

> 对应计划：docs/PLAN-2026-10-29.md 阶段18（认证与文档同步）、阶段19（分发重建）+ 用户新增需求
> 「手动录入新角色/武器 + 拉网查询」页面。门禁：py_compile ✅ / app.selftest 45/45 ✅ /
> app.smoke_test 12/12 ✅（全部为本阶段代码上实测）。

## 一、交付内容

### 阶段18（提交 f09c83e）
1. ROADMAP 登记阶段 18（mysqil 核对 + 16 处后端修复 + 5 处前端存量 bug + UI 大改版 +
   性能缓存）；订正"路线图状态"段落（图标/历史图表/兼容报告已在阶段 16 完成，从剩余 P2 移除）。
2. mysqil_ingest 缺字段防御：属性投票与映射重写对缺 name/attribute 的条目跳过并告警，
   不再因单条异常条目中断整个落库。
3. HEAD（用户提交 1888cad pathlib 迁移）门禁认证：selftest 42/42 + smoke 12/12（认证后
   自测矩阵扩充至 45 例）。

### 图鉴数据页（提交 f402336）
新导航页「图鉴数据」（codex），四区块：
1. **角色录入**：基础字段（名/稀有度/属性/物态/版本/实装开关/专武）手填 +
   「从 wiki 拉取养成预填」（真实网络实测：浔 → ability_2 / 倾诉花种·塞润尼缇×86 /
   名为炫耀的裙袂×11 / 絮语族 / 白当量 390 自动回填）；同名即更新（覆盖层语义天然支持
   1.5 上线把未上线角色翻 live）。
2. **武器录入**：与目录 weapons 条目同构（name/rarity/matter_state/owner/acquisition）。
3. **自定义条目清单**：覆盖层条目 chips，编辑回填表单 + 删除（基础目录不受影响）。
4. **新材料登记**：新 BOSS/周本材料进 materials.json extra_materials（id 白名单
   `[a-z0-9_]+` + 查重）——否则 extra_needs 白名单（planner.ALL_ORDER）不收。

数据与安全设计：
- **覆盖层** `data/game_data_custom.json`（.gitignore 归为用户数据）：`game_data.load()`
  按 name 合并覆盖，基础 game_data.json 永不被写（自测断言字节级不变）。
- **app/wiki_fetch.py** 新模块：纯 stdlib JS 对象字面量解析器（支持反引号字符串——
  真实 DetailedCalc.js 即此形态）；mysqil 映射表（BOSS_MAT_ID 等 5 张）迁入作为唯一真相；
  **SSRF 防线**（仅 https://wiki.mysqil.com 白名单 + 解析 IP 私网/环回/链路本地阻断 +
  禁跟随重定向）与 **realpath 穿越防护**（Mimosa 门禁逐项修复后放行）。
- 端点：POST /api/game-data/custom（upsert_character/upsert_weapon/delete）、
  GET /api/game-data/wiki-fetch、POST /api/game-data/custom-material；
  GET /api/game-data 附 custom 段。写路径均受"运行中禁改"409 与回环校验保护。

### 阶段19（本阶段收尾）
- dist 两档以 f402336 重建（构建日志与时间戳见构建任务输出），包含全部修复与新页面。

## 二、验证

| 项 | 结果 |
|---|---|
| selftest（新增 3 例：覆盖层合并/端点消毒与白名单拒绝/解析器离线夹具） | **45/45** |
| smoke_test（真实拉起面板走 API 主链路） | **12/12** |
| 解析器离线夹具 | 四组 5/7/3/4 类、20 名成员；浔/「零」括号/灵可未覆盖三路径全对 |
| 浏览器全链路 | 图鉴页渲染 ✅ → 真实 wiki 拉取回填 ✅ → 保存出 chip ✅ → qConfirm 删除 + "已删除" ✅ |
| 新端点功能级 | upsert 幂等/同名覆盖/删除回落/重复与非法 id 40x ✅ |
| 基础文件保护 | game_data.json 字节级不变断言 ✅ |

## 三、过程中的重要发现（记录供未来避坑）

1. **测试面板多实例并存**：Windows 上 SO_REUSEADDR 允许多个面板同时绑定 18789，
   `taskkill` 杀 bash 包装不杀 python 子进程 → 请求被随机分发到不同代码状态的旧实例，
   造成"间歇性失败"假象。排查时以 `netstat -ano | grep LISTENING` 清点监听进程为准，
   测试前应全量清杀。
2. **手写百分号编码的坑**：排查中手写 `浔=%E6%B5%9E`（实为"浞"）导致反向排除走了弯路；
   中文参数一律用 `urllib.parse.quote/encodeURIComponent` 生成。
3. Mimosa commit 钩子在本环境持续报 python_ast_unavailable（inconclusive）——
   维持计划书 G12：不据此宣称安全，conclusive 可得时重扫。

## 四、未验证与遗留

- 双击 EXE 的真机开合验证待用户执行（构建产物已更新到 f402336）。
- 图鉴覆盖层的导出/导入纳入（阶段22）；真机写配置演练（计划阶段20）。
- wiki 未覆盖角色（灵可/残虹/黑羽/明音凛/塔吉多）仍需人工录入——图鉴页现已提供录入入口。
