"""命令入口：python -m app.inspector [--align] [--report PATH]

退出码：0 一致；1 发现不一致/对齐被拒/对齐后仍不一致；2 用法或环境错误。
报告中的 counts / panels / board_topbar 可与当时页面分栏数字逐项对上。
"""
import argparse
import json
import sys

from app.inspection.runner import run_inspection


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.inspector",
        description="借出库只读侧车巡检（默认只出报告；--align 才做守卫式对齐）",
    )
    parser.add_argument(
        "--align", action="store_true",
        help="对齐主库中可自动修复的状态滞后（available+active → on_loan）；"
        "无此开关默认 report 模式，绝不写主库",
    )
    parser.add_argument(
        "--report", metavar="PATH",
        help="把 JSON 报告写到指定路径；不给则只打印到 stdout",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="只输出报告 JSON（stdout），不打印摘要行（stderr）",
    )
    args = parser.parse_args(argv)

    try:
        report = run_inspection(mode="align" if args.align else "report", report_path=args.report)
    except FileNotFoundError as e:
        print(f"[inspector] 环境错误：{e}", file=sys.stderr)
        return 2

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not args.quiet:
        if report["consistent"]:
            print(
                f"[inspector] OK 模式={report['mode']} "
                f"可借栏={report['counts']['available_panel_rows']} "
                f"active={report['counts']['active_loan_rows']} "
                f"on_loan={report['counts']['on_loan_pieces']} "
                f"顶细条可借={report['board_topbar']['available']}",
                file=sys.stderr,
            )
        else:
            ids = report["mismatch_item_ids"]
            print(
                f"[inspector] FAIL 模式={report['mode']} 异常 item_id={ids} "
                f"findings={len(report['findings'])} 详情见报告",
                file=sys.stderr,
            )
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
