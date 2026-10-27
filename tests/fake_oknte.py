"""E2E 测试用的 fake ok-nte 子进程（测试资产，不属于生产代码）。

由 runner 以 [python, tests/fake_oknte.py] 拉起，cwd=伪造的 ok-nte working 目录。
行为由环境变量控制：
  FAKE_MODE   done（默认，完整成功日志）/ short（体力不足）/ skipped（当日已达标）
              / fail（任务运行失败，退出码1）/ hang（常驻 FAKE_HANG 秒不退出）
  FAKE_DELAY  启动后先睡的秒数（默认0.05）
  FAKE_DELAY2 成功路径领取奖励前的秒数（默认0.05）
  FAKE_HANG   hang 模式常驻秒数（默认60）
  FAKE_LOG    要追加日志的文件（working/logs/ok-script.log 的绝对路径）

日志行格式与 ok-nte 真实格式一致（oknte_adapter.RunMonitor/scan_used_stamina 的解析目标）。
轮数从 runner 写入的配置（daily_anomaly.目标消耗体力）推导，模拟真实行为闭环。
"""
import json
import os
import sys
import time
from datetime import datetime

CONFIG_REL = os.path.join("configs", "DailyRoutineTaskConfigs.json")


def _emit(log, text):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log, "a", encoding="utf-8", newline="\n") as f:
        f.write(f"{stamp},100 INFO TaskExecutor DailyRoutineTask:{text}\n")


def main():
    mode = os.environ.get("FAKE_MODE", "done")
    log = os.environ["FAKE_LOG"]
    stamina = 0
    try:
        with open(os.path.join(os.getcwd(), CONFIG_REL), encoding="utf-8") as f:
            stamina = int(json.load(f)["daily_anomaly"]["目标消耗体力"])
    except (OSError, ValueError, KeyError, TypeError):
        stamina = 0
    units = stamina // 40 if stamina > 0 else 0

    if mode == "hunter":
        # 独立追猎（AnomalyHunter）：读 AnomalyHunter.json 的 领取奖励次数（0=打光体力），
        # 60 体力/局，结束输出"共计消耗体力: N"汇总（RunMonitor 的解析目标）。
        reward = 0
        try:
            with open(os.path.join(os.getcwd(), "configs", "AnomalyHunter.json"),
                      encoding="utf-8") as f:
                reward = int(json.load(f).get("领取奖励次数", 0))
        except (OSError, ValueError, TypeError):
            reward = 0
        try:
            with open(os.path.join(os.getcwd(), CONFIG_REL), encoding="utf-8") as f:
                hud = int(json.load(f)["daily_anomaly"]["目标消耗体力"]) // 40
        except (OSError, ValueError, KeyError, TypeError):
            hud = 0
        runs = reward if reward > 0 else hud
        runs = max(runs, 1)
        spent = runs * 60
        time.sleep(float(os.environ.get("FAKE_DELAY", "0.05")))
        _emit(log, "开始任务: 异象追猎")
        _emit(log, f"追猎完成：成功 {runs} 次，失败 0 次，共计消耗体力: {spent}")
        return 0

    time.sleep(float(os.environ.get("FAKE_DELAY", "0.05")))
    _emit(log, "开始任务: 异象界域")
    if mode == "short":
        _emit(log, "体力不足，退出任务")
        return 0
    if mode == "skipped":
        _emit(log, "跳过每日体力任务")
        return 0
    if mode == "fail":
        _emit(log, "任务运行失败: 注入的失败")
        return 1
    if mode == "hang":
        time.sleep(float(os.environ.get("FAKE_HANG", "60")))
        return 0
    _emit(log, f"info_set 体力消耗目标 {stamina}")
    _emit(log, "选择项目序号: 第 1 个项目")
    _emit(log, f"双倍次数: {units // 2}, 单倍次数: {units % 2}")
    time.sleep(float(os.environ.get("FAKE_DELAY2", "0.05")))
    _emit(log, "任务执行完毕")
    return 0


if __name__ == "__main__":
    sys.exit(main())
