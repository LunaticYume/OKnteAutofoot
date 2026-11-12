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

## 其他协作约束

- **git 提交与推送（2026-11-13 用户授权更新）**：每轮修复完成后由代理直接
  `commit` + `push origin main`；推送前必须确认 `.gitignore` 覆盖全部个人数据/
  凭据/运行时文件，且 `audit/`（审计报告，含未修复项披露）不入库；敏感文件永不
  提交；推送若因凭据交互授权失败，提示用户在终端执行一次。
- **不碰 `app/web/index.html`** 之外的约定：纯后端/数据任务不得改动前端页面。
- 写文件一律 `pathlib` / 仓库 `save_json_atomic`；落盘前对被覆盖数据做 `.bak` 备份。
- 诊断结论必须引用日志原文与行号；检查没跑过就如实说没跑过，不得伪造通过。
