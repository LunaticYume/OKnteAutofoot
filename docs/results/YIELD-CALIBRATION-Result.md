# 阶段8结果：实际掉落校准闭环

**结论：PASS**

## 交付物

- `planner.yield_suggestions()`（planner.py，纯读派生状态不落盘）：对历史中 **status=done 且用户已确认 actual_gain** 的条目，按材料累计 `measured = Σ实际获得 ÷ Σ轮数`，产出 `{configured_yield, confirmed_runs, confirmed_gain, measured_yield, suggested_yield}`。
- `POST /api/history/confirm`（main.py `confirm_history_entry`）：按 day+time+mat_id 精确匹配唯一历史条目，仅允许 done 记录、actual ≥ 0；重复确认覆盖旧值并刷新 confirmed_at；找不到返回 404（不凭空造账）。
- `GET /api/yield-suggestions`。
- **应用通道不新增**：建议的"应用"按钮走既有 `/api/tables/yields`（与手工编辑同一条校验与原子写路径）——**只建议、绝不自动改表**。
- 前端历史页：新增"实际获得"列（done 未确认的记录行内输入+确认；已确认显示数值与时间；非 done 显示 —）；新增"产量校准建议"卡片（当前产量/实测/建议值/应用，建议与当前一致显示"一致"）。
- 冒烟新增"产量校准三端点"步骤；`_StateFileGuard` 扩为同时备份恢复 `yields.json`。

## 状态归属（沿阶段3分类）

`history[].actual_gain/confirmed_at` = bookkeeping，owner 为用户（经确认 API 写入，原子持久化）；建议 = 派生状态（每次现算，不落盘）。

## 测试（真实退出码）

```
py_compile=0；app.selftest 27/27（连续两轮，exit 0）；app.smoke_test 7/7（exit 0）
```

`test_yield_calibration_loop` 覆盖：无确认无建议；两条确认的累计均值与建议值（(10+6)/4=4.0→4）；**配置产量不被自动修改**（仍为 7）；重复确认覆盖（10→8 后 3.5）；非 done 拒绝；不存在记录拒绝；连续两次计算结果一致（纯读确定性）。冒烟覆盖建议结构、产量表保存回写（内容不变）、确认接口 404 路径。

## 顺手修复（测试稳健性，非功能）

- 马拉松用例第 3 天断言从单点采样改为轮询（重试间隙存在短暂 running 窗口，单点采样会时序抖动；连续两轮全绿验证稳定）。

## 未验证 / Manual PASS

- 真实掉落数据下的建议合理性（需真机执行若干次后人工确认几条实际获得再观察建议值）。
- 小样本偏差：建议基于全部已确认条目的累计均值，样本少时偏差大——卡片文案已提示"样本少时先多确认几条"。
