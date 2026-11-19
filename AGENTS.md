# AGENTS.md — 代理协作约束（本仓库）

## 数据源白名单（2026-11-10 起生效）

### nteguide.com 全面禁用

**nteguide.com 自 2026-11-10 起全面禁用**：数据、图片、链接一律不再采用。

- 原因（用户实测）：nteguide 头像资产存在错误图片——例如 `lacrimosa.webp` 实为
  带对话框的过场剧情截图而非角色头像；多张头像不可用。
- 处置：`app/sites_fetch.py` 主机白名单已移除该域（见下方「出站请求白名单（当前值）」）；
  `fetch_nteguide` 已退役删除；既往快照/头像缓存中的 nteguide 条目已清理
  （快照同步时按 `source=="nteguide"` 一次性剔除）。
- 任何新增出站请求、数据导入、图片引用都**不得**再指向 nteguide.com。

### 现行资料源

| 用途 | 来源 | 备注 |
|------|------|------|
| 角色中文实名 ↔ 拼音 slug 对照 | wiki.mysqil.com `/characters/`（卡片 h3） | 失败回落 `/sitemap-0.xml` + `sites_fetch.MYSQIL_SLUG_NAMES`（25 项） |
| 角色头像 | wiki.mysqil.com `/images/characters/<拼音slug>.webp` | 立绘 2160×2560 竖版优先，横图/404 回落 `<slug>-icon.webp`（256×256） |
| 角色目录 / 弧盘目录 / 材料 / BIS 建议 | ntestrategy.com `/js/shared.js` | 弧盘=本应用武器口径；BIS 仅建议展示 |
| 养成分组 / 逐技能突破 / 卡带 | wiki.mysqil.com（`app/wiki_fetch.py`） | `/tools/cultivation`、角色详情页、`/cassettes/` |
| 角色数据 / 立绘 / 技能 / 都市特技 | everness.info `/zh-Hans/espers/<id>`（Next.js JS 壳，静态抓取需 `_payload` 或资产域直连） | 2026-11-11 用户指定新增；资产域 `api.everness.info/data/assets/UI_Icon/Appearance/<id>/Fashion_<id>_0/2560_Fashion_<id>_0.webp`（2160×2560 竖版立绘）；**曳尔沐 = esper 1090**（mysqil/ntestrategy 未收录） |
| （用户指定加入，结构待探明） | nte.nanoka.cc | 2026-11-11 用户指定新增；首页为 JS 壳、sitemap 404，抓取策略待首台实机探明 |

### 出站请求白名单（当前值）

`app/sites_fetch.py`：
```python
# 2026-11-11 用户指定新增：everness.info（Everness Database，espers/<id> 数据 +
# api.everness.info 资产域，子域由 endswith 规则自动放行）、nte.nanoka.cc
ALLOWED_HOSTS = ("ntestrategy.com", "wiki.mysqil.com", "everness.info", "nte.nanoka.cc")
```
`app/wiki_fetch.py`：仅 `wiki.mysqil.com`。
`app/tajiduo_client.py`：`("user.laohu.com", "bbs-api.tajiduo.com")`（:56）——
`user.laohu.com` 为老虎账号登录链（手机号+短信验证码，LAOHU_BASE_URL :58 真实使用），
`bbs-api.tajiduo.com` 为塔吉多用户中心/个人数据通道；两主机均为用户自行绑定的
账号体系域名，不接受前端传 URL。

安全红线（全部出站点共用口径）：仅 https + 主机白名单 + 解析 IP 拒私网/环回/
链路本地/保留/组播段 + 禁止跟随重定向 + URL 只能来自模块常量（不接受前端传 URL）。
架构不变量（2026-11-14 B2.5 起）：全部受保护出站传输必须经 app/url_guard.open_pinned 钉扎 transport（校验→register→open→unregister），禁止绕过直连；新增出站点一律接入共享防线并补传输层用例（selftest 不变量用例把守）。

## 其他协作约束

- **git 提交与推送（2026-11-17 用户授权更新，替代 2026-11-13 版）**：每轮修复完成后由代理直接
  `commit` + `push origin main`，**删除"提示用户手动推送"回退条款**——推送凭据失败时代理
  自行经 Windows 凭据管理器 GitHub Desktop 条目（target 形如 `GitHub - ...`，`gho_` token，
  CredRead 进程内使用、不落盘不回显）解决，不改用户 git 配置、不把 token 写入任何文件或命令行。
  **凭据使用边界（硬性）**：该 token 仅可用于对 `github.com`/`api.github.com` 本仓库的
  git 认证与仓库设置 API；仅经环境变量/stdin 管道传递（禁止 URL 内嵌凭据
  `https://<token>@github.com/...` 形式）；禁止写入文件、提交内容、日志或对话输出；
  禁止用于任何其他主机或服务；任何其他出站请求一律不得携带该凭据。
  `audit/`（审计报告，含未修复项披露）、`.tmp/`、`.testarchive/`、`dist/` 不入库；敏感文件永不提交。
  **推送前数据安全三查（SEC-09 教训固化，2026-11-17 历史 filter-repo 清洗 9 路径的直接动因，
  强制执行，缺一不推）**：
  1. `git status --porcelain` 逐行核对——暂存/未暂存只允许本批预期文件，任何计划外文件先查清来源再动；
  2. 推送前 `git log origin/main..HEAD --stat` 扫描全部待推提交的路径，命中下方黑名单任一
     **立即停止推送并报告**，已误提交的不得推送、先在本轮撤销；
  3. 新增会写个人数据/凭据/运行时文件的代码路径，必须**先补 `.gitignore` 再提交代码**
     （ignore 先于代码入库）。
  **data/ 白名单制**：仅现役被跟踪的 13 个数据表/缓存文件（`git ls-files data/` 核对：数据表、
  `mysqil/` 快照、`sites/source_crosscheck.json`）可变更入库；data/ 下任何新文件默认=运行时
  个人产物，禁止提交，除非用户明确裁定为数据表更新且核对无个人字段。
  **永不入库黑名单（历史清洗对象，再次入库=事故）**：`data/state.json`（含 `.bak`）、
  `data/runtime.json`（含 `.bak`）、`data/backups/`、data/ 下一切 `*.bak` 与时间戳快照、
  `data/game_data_custom.json`、`data/panel_token.json`、`data/tajiduo_token.json`、
  `data/elev_helper_config-*.json`、`data/elev_helper_endpoint-*.json`、面板/OCR/headless
  运行日志（`*.log`）、设备指纹与 OCR 夹具文件；新增运行时落盘文件时同步扩充本清单与
  `.gitignore`。
- **不碰 `app/web/index.html`** 之外的约定：纯后端/数据任务不得改动前端页面。
- 写文件一律 `pathlib` / 仓库 `save_json_atomic`；落盘前对被覆盖数据做 `.bak` 备份。
- **测试产物统一管理（2026-11-20 用户裁定，替代 2026-11-13「测试临时目录卫生」与
  「关键测试痕迹保留」两条）**：本软件测试产生的一切内容——验证/验收输出
  （verify_dist/selftest/smoke 结果）、E2E 与 walkthrough 产物（截图/日志/驱动脚本）、
  打包探针输出、故障注入取证、OCR/设备测试夹具、EXE 运行解包残留、临时中间态——
  一律直接写入仓库内 `.testarchive/<日期>-<轮次>/`（gitignore，E: 盘）统一管理；
  **每轮测试结束不必删除清理**，按轮次累积、长期保留。产生证据的测试必须让产物
  直接落进 `.testarchive/`，禁止先写系统临时目录再事后抢救。仍禁止按前缀+时间
  一刀切删除任何测试痕迹；归属不明的一律先归档。
- **C 盘测试零落地（2026-11-20 用户裁定）**：**禁止直接在 C 盘产生测试缓存/临时
  文件**——`tempfile` 调用一律显式传 `dir=` 指向仓库内 `.tmp/` 或 `.testarchive/`
  （E: 盘）；`%TEMP%`/LOCALAPPDATA 等系统临时目录不得用作测试产物/缓存的落地
  位置；测试进程必须正确关闭（不留孤儿子进程/锁住的临时句柄）。C 盘现存历史
  测试残留只做「迁出」：归档（manifest+sha256）后从 C 盘移除，不做原地保留
  （首批见 `.testarchive/2026-11-20-c-drive-sweep/`）。正式验收证据以
  `docs/verification/` 与 `audit/` 报告为准；`.tmp/` 仍限构建与工作流中间态。
- 诊断结论必须引用日志原文与行号；检查没跑过就如实说没跑过，不得伪造通过。
