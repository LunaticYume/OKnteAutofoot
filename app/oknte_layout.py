"""ok-nte 安装布局单一来源（ELEV-04 / W4c-R，2026-11-22）。

真实 ok-nte 安装布局：ok-nte 应用位于 ``<root>/data/apps/ok-nte`` 之下——
内嵌解释器 ``python/``、无界面工作区 ``working/``、规划器配置副本
``configs_planner/``。本模块是这一布局的**唯一定义点**：

- ``app/oknte_adapter.OkNte``（生产侧：launcher/配置副本路径由此派生）；
- ``app/selftest`` 夹具（测试根按真实目录结构搭建，ELEV-04 前夹具误用
  ``<root>/working`` 顶层布局、与白名单一起固化了错误结构——fixture-vs-reality
  缺口的根治）；
- ``app/elev_helper`` 启动白名单（校验侧）因「被提权进程独立拉起、绝不 import
  本仓库其他模块」的依赖面约束（app/elev_helper.py:27 口径）**不能直接 import
  本模块**，以镜像常量 ``elev_helper.LAYOUT_APPS_SUBDIR`` 引用，由
  ``test_elev04_layout_single_source_static`` 静态守卫钉死两侧一致——再漂移时
  门禁必红。``elev_client.launch_runner`` 客户端校验委托 ``elev_helper`` 同一
  函数，天然与 helper 侧同判据。

背景（ELEV-04，2026-11-22 W4c-3 实机复现，severity high）：白名单此前要求
``<root>\\working\\configs_planner``，比真实布局少算 ``data\\apps\\ok-nte`` 一段，
打包态真实 oknte_root 下 ``launch_runner`` 客户端校验必拒→回退直接拉起→
ok-nte 无法完成游戏启动→托管启动链 100% 不可用。

纯标准库、零仓库内 import（本模块自身不得引用 app 包任何模块）。
"""
import os

APPS_SUBDIR = ("data", "apps", "ok-nte")
WORKING_DIR_NAME = "working"
PROFILE_DIR_NAME = "configs_planner"
LAUNCHER_NAME = "run_planner_task.py"
PYTHON_REL = ("python", "python.exe")  # ok-nte 内嵌解释器（无头任务/构建定位共用）


def apps_dir(root):
    """<root>/data/apps/ok-nte（ok-nte 应用安装目录）。"""
    return os.path.join(str(root or ""), *APPS_SUBDIR)


def working_dir(root):
    """<root>/data/apps/ok-nte/working（ok-nte 无界面工作区）。"""
    return os.path.join(apps_dir(root), WORKING_DIR_NAME)


def profile_dir(root):
    """<root>/data/apps/ok-nte/working/configs_planner（规划器配置副本目录）。"""
    return os.path.join(working_dir(root), PROFILE_DIR_NAME)


def launcher_path(root):
    """<root>/data/apps/ok-nte/working/configs_planner/run_planner_task.py。"""
    return os.path.join(profile_dir(root), LAUNCHER_NAME)


def python_exe(root):
    """<root>/data/apps/ok-nte/python/python.exe（ok-nte 内嵌解释器）。

    oknte_adapter.OkNte（无头任务拉起）与 tools/build_exe.embedded_python 的
    state.json 兼容回退按本函数派生（W5A 收编：此前构建侧手拼第三处布局，
    与 ELEV-04 同类的 fixture-vs-reality 漂移风险面）。"""
    return os.path.join(apps_dir(root), *PYTHON_REL)
