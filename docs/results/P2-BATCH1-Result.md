# 阶段16结果：P2 批次一（专属图标 + 历史图表 + 兼容报告）

**结论：PASS**

## 交付物

### 1. 应用专属 EXE/窗口图标（纯标准库，无任何依赖）
- `app/icon_gen.py`：像素级绘制 BMP-in-ICO（32×32 + 16×16，深蓝底 + 青色圆环——"环"应异环），构建脚本与 dist 自测共用同一实现
- csc 启动器 `/win32icon:`、PyInstaller `--icon`——两个 EXE 的文件图标与任务栏图标变为应用专属
- 自测断言：ICO 魔数、目录项数/尺寸、目录偏移与长度自洽（逐字节校验）

### 2. 历史图表（SVG，纯前端零依赖）
- 历史页顶部新增「最近 14 天」双系列条形图：按 day 聚合 history（体力消耗=青、预计产出=绿），悬停显示当日明细（体力/产出/执行次数）；明细表格保留

### 3. 兼容报告一键生成
- `GET /api/compat-report`（`build_compat_report`）：markdown 报告——ok-nte 版本与三态指纹结果（problems/warnings 逐条）、依赖面首查清单（源自 UPSTREAM_COMPATIBILITY.md 要点）、校准状态（coin 未校准警告/追猎产量/通用材料表数值）、执行器与未决执行状态、生成时间
- 总览页「⬇ 导出兼容报告」按钮 → 下载 `OKntePlanner-compat-<日期>.md`
- 用途：ok-nte 更新后一键自查 + 可直接把报告发给别人求助

## 测试（真实退出码）

```
主工程：py_compile=0；selftest 42/42（新增 icon 结构逐字节校验/compat-report 内容）；smoke 12/12（新增 compat-report 200+关键字）
dist 重建：dist 内自测+冒烟通过；双变体实跑 API+首页 ✅；单文件 11.9MB（含图标重建）
```

## 构建期发现并修复（3 处，均为本阶段新代码自身问题）

1. icon 生成器最初内联在 build_exe.py，dist 自测无法加载（dist 无 tools/）→ 抽到 `app/icon_gen.py` 共用。
2. `_generate_icon` 委托时 sys.path 不含项目根（直接运行 tools/build_exe.py）→ 补 ROOT 注入。
3. 单文件档引用了 build_dist 的局部变量 `ico` → 改为独立生成（幂等）。

## 排除项（P2 中不做的，如实声明）

- **多来源材料效率排序**：材料当前均为单来源，无多来源数据可排（待游戏数据补充）
- **背包 OCR**：需自建游戏捕获/识别栈，违背"最小侵入 ok-nte"与零依赖纪律
- **1.5 角色数据**：11/5 上线后按阶段14 流程更新 game_data.json
- **逐级分段表**：官方未公开，待游戏内逐级实测后填入需求表分段

## Manual PASS

- 双击 EXE 查看任务栏/文件图标是否为新图标（Windows 图标缓存可能需要重命名文件夹或重启资源管理器刷新）
- 历史页图表在有历史数据后的观感
