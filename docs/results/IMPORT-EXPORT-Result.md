# 阶段10结果：配置导入导出 + schema version + 备份回滚

**结论：PASS**

## 交付物

- **schema version**（state.py）：`STATE_SCHEMA=1` + `_migrate` 迁移钩子；旧文件（无 schema 键）按 v1 兼容读取；每次 save 自动盖章。导出/导入以此为版本契约。
- **导出** `GET /api/export`：`{schema, exported_at, settings, characters, inventory, yields, requirements}`——只含用户配置，不含历史与运行时；前端一键下载 JSON。
- **导入** `POST /api/import`（`apply_import`）：**全部段落先校验/脱敏，任何不合法整体拒绝（绝不留半套数据）**；导入前自动备份当前用户数据到 `data/backups/state.import.<时间戳>.json`；state/yields/requirements 统一原子落盘后热重载。前端文件选择 + 覆盖确认（显示角色数）+ 结果回显（含备份路径）。
- **备份回滚** `GET /api/backups` + `POST /api/backups/restore`：列出 ok-nte 日常配置自动备份（新→旧）；整文件恢复为**刻意的全量回滚语义**（区别于最小补丁）——ok-nte 运行中拒绝、备份损坏/缺 daily_anomaly 段拒绝、名字过 `os.path.basename` 防路径逃逸、**恢复前先把当前配置再备份一份（回滚可逆）**、原子替换；前端二次确认。

## 测试（真实退出码）

```
py_compile=0；app.selftest 29/29（两轮 exit 0）；app.smoke_test 9/9（exit 0）
重建 dist：dist 内自测 29/29 + 冒烟 9/9；单文件 EXE 重建 + 冻结数据目录探针 ✅
```

`test_export_import_and_rollback`：save 盖章 schema=1；导出六段结构完整；合法导入整体生效（库存/设置/表落盘 + 自动备份存在）；三类非法导入（schema 不符/characters 空/requirements 缺 tracks）**整体拒绝且 STATE 深拷贝不变**；备份列表排序、整文件恢复内容精确、恢复前留现状（2 份备份）、运行中拒绝、`../evil.json` 路径逃逸拒绝、不存在拒绝。冒烟含 HTTP 层导出→改值导入→bootstrap 验证生效→坏 schema 400→备份列表。

## 状态归属

schema = 配置状态（state.py 拥有，save 盖章）；导入前备份 = bookkeeping（一次性，data/backups）；备份回滚只碰 ok-nte 配置文件，不碰 STATE。

## 未验证 / Manual PASS

- 从旧版本（无 schema 键的历史 state.json）首次升级：单测覆盖兼容读取路径，但真实旧文件字段组合未逐一回放。
- 大文件导入（角色/需求表极大时的体积上限 2MB 请求体限制已在 `_body` 钳制，超限返回 400）。
- 备份回滚后 ok-nte GUI 首次打开的表现（回滚内容含旧循环模式等字段时行为以 ok-nte 为准）。
