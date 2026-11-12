"""用户数据迁移 CLI：把旧数据目录（旧版 EXE 旁 data\\ 等）整树合并进统一用户数据目录。

用法（仓库根；tools 为命名空间包，无 __init__.py，与 python -m app.selftest 同机制）：
    python -m tools.migrate_user_data --from <目录> [--from <目录2> ...] [--dry-run] --apply

契约（任务硬性要求 2）：
- --from 必需、可重复、按出现顺序逐个源处理（no-clobber 下先处理的源在同路径
  文件上获胜）；任一源目录不存在 → 打印错误并以退出码 2 结束（校验先行，
  绝不写盘）。
- --apply 必需；缺失时打印用法并以退出码 2 结束、绝不写盘。--dry-run 可选、
  必须与 --apply 连用（无 --apply 单传 --dry-run 属用法错误，exit 2）；
  --dry-run --apply=演练：完成全部校验与逐源计划打印，但不写任何文件。
- 动作：复用 app.state._copy_tree_no_clobber 逐文件原子 no-clobber 整树复制
  合并进 app.state.USER_DATA_DIR（%LOCALAPPDATA%\\OKntePlanner\\data）——目标
  已存在即跳过；只复制，绝不移动或删除源目录任何文件，绝不覆盖
  USER_DATA_DIR 已存在文件（含并发半写留下的 .tmp 由下次 no-clobber 自愈）。
- 输出：仅逐类计数（每源+合计：新建目录/复制文件/跳过已存在/失败）与路径名，
  绝不打印文件内容或任何敏感值；失败项只列相对路径。
- 退出码：成功 0；用法错误或源目录不存在 2；复制中任何失败 1。
- 本 CLI 不写 .migrated 标记（取舍）：首启 ensure_user_data 以 no-clobber 自愈
  补齐 EXE 旁出厂数据表缺失文件；且一旦 CLI 写下 .migrated，用户旧 EXE 旁数据
  的首启自动迁移会被幂等门跳过——标记只由 ensure_user_data 管理（单一事实源）。
"""
import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)  # 仓库根（app 包所在），使任意 cwd 下均可运行

try:  # Windows 控制台中文输出保险（与 tools/build_exe.py 同款）
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

USAGE_HINT = ("用法: python -m tools.migrate_user_data --from <目录> [--from <目录2> ...]"
              " [--dry-run] --apply")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m tools.migrate_user_data",
        description="把旧数据目录整树 no-clobber 合并进统一用户数据目录"
                    "（%LOCALAPPDATA%\\OKntePlanner\\data；只复制不删除、不覆盖已有文件）",
        add_help=True)
    parser.add_argument("--from", dest="sources", action="append", required=True,
                        metavar="目录",
                        help="源数据目录（可重复，按出现顺序处理；先处理的源在同路径文件上获胜）")
    parser.add_argument("--apply", action="store_true",
                        help="执行迁移（必需；缺失即用法错误，绝不写盘）")
    parser.add_argument("--dry-run", action="store_true",
                        help="演练：完成校验与计划打印但不写任何文件（必须与 --apply 连用）")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.apply:
        # 缺 --apply（含无 --apply 单传 --dry-run）属用法错误：绝不写盘
        print("用法错误：--apply 必需（--dry-run 必须与 --apply 连用）", file=sys.stderr)
        print(USAGE_HINT, file=sys.stderr)
        return 2
    # 惰性导入：用法校验通过后才加载 app.state（源码态导入无写盘副作用）
    from app import state as state_mod

    # 校验先行：任一源目录不存在 → 打印错误并 exit 2，绝不写盘
    missing = [s for s in args.sources if not os.path.isdir(s)]
    if missing:
        for src in missing:
            print(f"错误：源目录不存在: {src}", file=sys.stderr)
        return 2

    user_dir = state_mod.USER_DATA_DIR
    mode = "演练（--dry-run，不写任何文件）" if args.dry_run else "执行"
    print(f"目标用户数据目录: {user_dir}")
    print(f"模式: {mode}")
    totals = {"dirs": 0, "copied": 0, "skipped": 0, "failed": 0}
    for src in args.sources:  # 按出现顺序逐源处理（no-clobber 下先到先得）
        stats = state_mod._copy_tree_no_clobber(src, user_dir, dry_run=args.dry_run)
        print(f"源 {src}: 新建目录 {stats['dirs']} / 复制文件 {stats['copied']} / "
              f"跳过已存在 {stats['skipped']} / 失败 {stats['failed']}")
        for rel in stats["errors"]:  # 失败项只列相对路径，绝不打印内容
            print(f"  失败: {rel}")
        for key in totals:
            totals[key] += stats[key]
    print(f"合计: 新建目录 {totals['dirs']} / 复制文件 {totals['copied']} / "
          f"跳过已存在 {totals['skipped']} / 失败 {totals['failed']}")
    if totals["failed"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
