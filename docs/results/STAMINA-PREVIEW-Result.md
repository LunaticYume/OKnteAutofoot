# 阶段9结果：体力恢复模型与未来多日预览

**结论：PASS**

## 交付物

- **体力恢复模型**（设置新增：`stamina_cap` 240 / `stamina_regen_per_hour` 10 / `stamina_now`+`stamina_now_at` 服务端盖章）：`planner.projected_stamina(now)` 纯读推算 `min(上限, 填写值+速率×经过小时数)`；未填/无效返回 None。
- **未来 N 天预演** `planner.preview_days(days, now)`（纯读、确定性、注入时钟、不改 STATE）：第 0 天额度 = 限额−今日已耗（与 make_plan 同口径，含追猎预留），第 1..N-1 天 = 限额；虚拟库存按预计产出逐日累加、与 make_plan 同一贪心逐日排步；返回逐日安排/剩余/未达标数、首个全部达标日 `complete_on`、体力估计、警告（恢复速率×24 < 限额、速率为 0）。
- API：`GET /api/preview?days=3`；设置消毒接受三个新键（stamina_now 填写即盖章、空值清除、非法忽略；cap/regen 钳位）。
- 前端：设置页"体力恢复模型"卡片；总览页"未来 3 天预览"卡片（逐日表 + 达标日 + 警告 + 当前体力估计）。
- **明确边界**：模型只用于展示与预演，**不改变执行逻辑**——真实体力不足仍由 ok-nte 检测后进入等待重试（fail-safe 不变）。

## 测试（真实退出码）

```
py_compile=0；app.selftest 28/28（连续两轮 exit 0）；app.smoke_test 8/8（exit 0）
```

`test_stamina_projection_and_preview`：未填无估计；2 小时 +20、30 小时触顶；day0 额度与 2 轮安排、day1 补齐、`complete_on` 精确、day2 零安排零缺口；追猎预留扣减 + 恢复不足警告；**两次预演结果一致且 STATE 深拷贝不变（纯度）**；设置消毒（盖章/清除/非法保留原值/cap 钳位 999999→999）。

## 未验证 / Manual PASS

- 真实游戏体力恢复速率（默认 10/小时按常见 6 分钟 1 点假设）需用户按游戏实际校准一次。
- 预演的"每日限额可全额消耗"假设在攒体力玩法下会偏乐观——警告已覆盖该情形，真实节奏以 ok-nte 执行反馈为准。
