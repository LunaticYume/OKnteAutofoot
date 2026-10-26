# 阶段1结果：上游兼容边界与更新抗性

**结论：PASS**

## 交付物

- `docs/UPSTREAM_COMPATIBILITY.md` —— 15 项依赖面清单（假设/位置/后果/检测/风险分层）
- 兼容性指纹（三态）：`OkNte.compat_check()` 返回 `status ∈ {compatible, compatible_with_warning, incompatible}` + `problems[]` + `warnings[]`
- 最小补丁写入：`write_anomaly_config` 升级为 **读取 → deepcopy → 仅更新目标键 → 差异白名单校验 → 原子替换**，任何校验失败在落盘前抛错、原文件字节不变
- 自测新增 `test_compat_fingerprint_patch_matrix`（10 个 fixture 场景）

## 审计发现：已存在、未重做的部分（证据）

| 简报要求 | 现状证据 | 处置 |
|---|---|---|
| Adapter 收敛 | planner 仅经注入对象调 `scan_used_stamina`（planner.py:154-155）；runner/main 仅 import oknte_adapter；前端只走自有 API | 已达标，零改动 |
| 原子写 | tmp + `os.replace`（oknte_adapter.py write_anomaly_config / state.py save / main.py `_save_json_atomic`） | 已达标 |
| 不重排/不删除未知键 | `dict.update` 保序、只改目标键；原用例 test_adapter_compat_write_backup 已断言其他段与循环序列保留 | 已达标 |

## 本阶段实际改动

| 文件 | 改动 |
|---|---|
| `app/oknte_adapter.py` | 指纹三态化（能力检查：python/main.py/ok包/配置解析/daily_anomaly 键与类型/hunter 段/日志目录）；写入前类型校验（与指纹同一判据 `_daily_type_problems`）；deepcopy + `_validate_patch` 差异白名单；**修复缺陷：hunter 体力键写入分支检查了错误键名（`"stamina" in hunter_cfg`），追猎目标消耗体力此前从未真正写入过** |
| `app/runner.py` | incompatible 才终止（原来是 ok=False）；warnings 逐条进执行日志 |
| `app/main.py` | `/api/start` 仅 incompatible 拦截 |
| `app/web/index.html` | 顶栏标签三态化，警告/问题悬停可见具体原因 |
| `app/selftest.py` | FakeOkNte 返回新形状；新增 fixture 矩阵用例（10/10 全过） |

## 门禁与测试（真实退出码）

```
py_compile app/*.py        → 0（COMPILE_OK）
python -m app.selftest     → 0，通过 10/10
python -m app.smoke_test   → 0，通过 5/5
```

矩阵覆盖：正常格式→compatible；未知字段→compatible 且写回保留；缺必需键/类型变化/配置损坏/文件不存在→incompatible 且写入拒绝、文件字节不变；不相关新增段→保留；序号键缺失→compatible_with_warning；追猎段缺失+hunter 请求→拒写；hunter 正常写入（回归旧缺陷）。

## 仍与上游强耦合的位置（按风险排序）

1. **日志文本模式**（RunMonitor 正则 ×7 + 失败标记 + 日期前缀）——无契约的字符串依赖，指纹无法静态证明；缓解：解析不到任何标记 → 判 failed → 拉黑当日目标，绝不盲记账。
2. **CLI 任务名 `DailyRoutineTask`**（硬编码）与 `-e` 参数语义。
3. **配置值枚举**（任务类型/奖励目标/循环模式="停用"）——登记在 data/materials.json，面板可校准。
4. 进程检测假设（ok-nte.exe 名称、pythonw 托管 working/main.py）。

## 未验证的真实环境风险

- 指纹未实跑 CLI（避免副作用），`run_task` 参数在未来版本的实际兼容性只能实跑发现。
- 日志文案若随 ok-nte 版本本地化调整，RunMonitor 会整体失效（表现为全步骤 failed + 拉黑）；需要真实更新事件后核对。
- `is_running` 的 PowerShell 查询在系统策略禁用 CIM 时保守按"运行中"处理（拒绝写入），需真实环境观察误报率。

## ok-nte 更新后首先检查什么

见 `docs/UPSTREAM_COMPATIBILITY.md` 末尾"用户首查清单"（指纹标签 → /api/plan → 配置键 → 日志文案 → 任务名 → backups 回滚）。

---
*执行方式说明：本阶段原计划交子代理执行，因模型并发限制两次失败，改由主会话按同一简报逐条执行，验收口径不变。*
