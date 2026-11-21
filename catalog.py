"""查看本地交换资料，不改变台账。"""
from argparse import ArgumentParser
import csv
import json
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"


def read_table(name):
    """读取一张已登记的原始交换表。"""
    path = DATA / (name + ".csv")
    with path.open(encoding="utf-8", newline="") as handle:
        yield from csv.DictReader(handle)


def main():
    parser = ArgumentParser(description="查看交换资料")
    parser.add_argument("--table", choices=sorted(path.stem for path in DATA.glob("*.csv")),
                        help="查看指定资料的原始记录")
    options = parser.parse_args()
    if options.table:
        content = list(read_table(options.table))
    else:
        content = {path.stem: sum(1 for _ in read_table(path.stem))
                   for path in sorted(DATA.glob("*.csv"))}
    print(json.dumps(content, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
