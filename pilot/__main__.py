"""命令行入口：生成轮次复盘、下一轮计划与证据文件。"""
from __future__ import annotations

from argparse import ArgumentParser
from datetime import date
from pathlib import Path

from .data import DATA_DIR, Tables
from .report import build_reports


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description="生成试点复盘与下一轮投放计划")
    parser.add_argument(
        "--data-dir", type=Path, default=DATA_DIR, help="交换表目录（默认项目 data）"
    )
    parser.add_argument(
        "--out", type=Path, default=Path("reports"), help="输出目录（默认 reports）"
    )
    parser.add_argument("--plan-date", type=date.fromisoformat, default=None,
                        help="计划日期 YYYY-MM-DD；默认取资料截止日")
    options = parser.parse_args(argv)

    tables = Tables(options.data_dir)
    evidence = build_reports(tables, options.out, plan_date=options.plan_date)
    for round_name, payload in evidence["rounds"].items():
        status = payload["status"]
        print(f"轮次 {round_name}: {status}")
        if status == "planned":
            print(f"  名单: {', '.join(payload['allocation']['communities'])}")
        else:
            for comparison in payload["comparisons"]:
                label = comparison["label"]
                if comparison["status"] == "not_identifiable":
                    print(f"  {label}: 不可识别")
                elif comparison["status"] == "progress_only":
                    print(f"  {label}: 仅进度")
                else:
                    print(
                        f"  {label}: 风险差 {comparison['resident_rd']:+.3f} "
                        f"(p={comparison['ri_p_resident']:.3f})"
                    )
    print(f"报告目录: {options.out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
