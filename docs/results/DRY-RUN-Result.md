# 阶段4结果：Dry Run / Simulation Mode

**结论：PASS**

## 交付物

- **共享 patch builder**：`OkNte.build_anomaly_patch()`（oknte_adapter.py）——读取原配置 → deepcopy → 仅更新目标键 → `_validate_patch` 差异白名单校验 → 返回 `{patched, diff}`。**纯读**：不检测进程、不落盘。
- **单一逻辑源**：真实写入 `write_anomaly_config` = is_running 检查 → `build_anomaly_patch` → 原子替换（行为与阶段1完全一致）；Dry Run = `perform_dry_run`（main.py）→ 同一 `compat_check` + 同一 `planner.make_plan` + 同一 `build_anomaly_patch`，仅止步于"校验补丁"。旧的 `dry_write_anomaly_config`（与写路径两套逻辑、无校验）**已删除**。
- **API**：`POST /api/dry-run` 返回 `{ok, compat, oknte_running, plan, patch:{file,diff}, hunter_note, error?}`；diff 为 `[{section,key,old,new}]` 只列差异。运行中豁免"禁改"拦截（纯读）。
- **面板**：总览页新增「👁 预演（不落盘）」按钮与结果卡片——本轮材料/轮数/体力/预计获得/额度变化 + 将修改的文件与字段级 before/after 差异表 + 追猎写入时机说明 + ok-nte 运行中提示。

## Dry Run 走过的完整链路（与真实执行的唯一分叉点）

```
读取用户数据(state) → make_plan(纯读) → compat_check(能力指纹)
→ 读取 ok-nte 当前配置 → build_anomaly_patch(deepcopy+白名单校验) → 展示 diff
                                              └─ 真实执行从这里分叉：is_running 检查 → 原子落盘 → 拉起
```

不写文件、不启动 ok-nte、不改 quota/库存、不留运行账目（runtime.json 不触碰）。

## 已存在、未重做的部分（证据）

| 简报要求 | 现状证据 | 处置 |
|---|---|---|
| 计划摘要与额度变化 | make_plan 的 steps[].reason + plan.detail（阶段2已建） | 复用展示，未重算 |
| 补丁校验 | `_validate_patch`（阶段1） | 直接复用为 builder 的一环 |
| 原子落盘 | `save_json_atomic`/tmp+replace（阶段1/3） | 写路径沿用 |

## 改动清单

`app/oknte_adapter.py`（builder 抽取 + `_patch_diff` + 删旧 dry_write）、`app/main.py`（`perform_dry_run` + `/api/dry-run` + 豁免拦截 + 导入 OkNteError）、`app/web/index.html`（预演按钮与结果卡片）、`app/selftest.py`（100 次不变量用例）、`app/smoke_test.py`（dry-run 接口断言，6/6）。

## 测试（真实退出码）

```
py_compile app/*.py → 0
app.selftest        → 0，通过 16/16
app.smoke_test      → 0，通过 6/6（新增 /api/dry-run 步骤）
```

**100 次预演不变量**（`test_dry_run_hundred_times`）：ok-nte 配置文件 SHA-256 逐次不变；库存不变；今日 used_stamina/planner_spent 不变；runtime 状态不进入 running；100 次 plan+patch 结果与首次完全一致（确定性）。另验证 diff 必含 `目标消耗体力`/`循环模式`/目标键的真实 before/after，且 incompatible（配置损坏）时返回 compat 失败、不生成补丁。

## 未验证的真实环境风险

- 预演对**真实 ok-nte 配置**的 diff 正确性在冒烟中以只读方式经过真实文件（bootstrap/plan/dry-run 均真实路径），但"预演后手动实跑、两边 diff 与落盘内容逐字节一致"的对照未做全量断言（两者共用同一 builder，代码路径上不存在分叉；风险仅剩 builder 落盘序列化差异——同一 json.dump 参数，已由阶段1矩阵覆盖）。
- ok-nte 正在运行时预演仍可执行（纯读），此时 diff 基于磁盘当前值；真实执行会先等其退出，期间配置可能被 ok-nte 自身改写，diff 可能过时——已在卡片上提示。

---
*执行方式说明：子代理因提供方并发限制即时失败，本阶段由主会话按同一简报执行，验收口径不变。*
