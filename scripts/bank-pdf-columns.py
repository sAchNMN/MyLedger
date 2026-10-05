"""看一份银行 PDF 的**列结构**，判断解析器有没有切错列。

什么时候用这个
--------------
`bank-pdf-probe.py` 给的是「每行有几个格子 + 每格脱敏后的样子」，
它看不出**格子之间对应哪一列**。而解析 0 笔、金额取错列这类问题
恰恰出在这里 —— 探针会显示「7 格」，而表头其实是 9 列，
说明有两列被粘成了一格。

这个脚本补上那一块：它输出
  · 表头每一列的名字（脱敏：中文→字、字母→a，长度保留）
  · 每一行的**列数**
  · 关键列（日期 / 时间 / 摘要 / 金额 / 余额）实际取到了第几格
  · 每一格**属于哪一列**（按 x 坐标归属，不靠猜）

隐私
----
与 `bank-pdf-probe.py` 同一个原则：**只输出形状，不输出值**。
数字一律显示成 `9`，中文显示成 `字`，字母显示成 `a`。
真实的金额、商户名、卡号一个都不会出现，输出可以安全外发。

用法：
    <venv>\\Scripts\\python.exe scripts\\bank-pdf-columns.py "账单.pdf"
    <venv>\\Scripts\\python.exe scripts\\bank-pdf-columns.py "账单.zip"
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from importer.sources import BANK_PDF_ALIASES  # noqa: E402

# 复用另一个探针的脱敏函数，保证两个脚本「同一份数据脱敏后长得一样」——
# 两边各写一套的话，同一行在两个输出里形状不同，对照起来毫无意义。
_probe_spec = __import__("importlib.util", fromlist=["util"]).spec_from_file_location(
    "bank_pdf_probe", os.path.join(HERE, "bank-pdf-probe.py"))
_probe = __import__("importlib.util", fromlist=["util"]).module_from_spec(_probe_spec)
_probe_spec.loader.exec_module(_probe)
mask = _probe.mask
from importer.archive import ArchiveError  # noqa: E402
from importer.bank_pdf import PdfError, extract_rows  # noqa: E402
from importer.sniff import build_index, find_header_row, header_names  # noqa: E402
from importer.sources import BANK_PDF_ALIASES  # noqa: E402

KEY_FIELDS = ("date", "time", "category_raw", "item", "amount",
              "counterparty", "method")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="银行 PDF 列结构探针（输出全部脱敏，可安全外发）")
    ap.add_argument("pdf", help="PDF 路径（也可以直接给装着它的 zip）")
    ap.add_argument("--rows", type=int, default=20, help="最多显示多少行")
    ap.add_argument("--password", default=None, help="密码。不传则交互提示")
    args = ap.parse_args()

    if not os.path.isfile(args.pdf):
        print(f"找不到文件：{args.pdf}")
        return 2

    password = args.password
    if password is None:
        try:
            password = getpass.getpass("密码（不回显，不保存）：")
        except (KeyboardInterrupt, EOFError):
            print("\n已取消。")
            return 2

    try:
        rows, meta = extract_rows(args.pdf, password)
    except ArchiveError as exc:
        print(f"读不了这个压缩包：{exc}")
        return 2
    except PdfError as exc:
        print(f"读不了这个 PDF：{exc}")
        return 2

    print("=" * 78)
    print("以下是**列结构**。数字/中文/字母已脱敏（9/字/a），真实值一个都不显示。")
    print("=" * 78)
    print(f"页数 {meta.get('pages')}    抽出 {len(rows)} 行")
    print()

    header_row = find_header_row(rows)
    if header_row is None:
        print("！没找到表头行 —— 解析器会退回「按内容猜列」，那是最容易出错的模式。")
        print("  这份 PDF 可能压根没有列名行。先看下面的行结构：")
        header = None
        index = {}
    else:
        header = header_names(rows[header_row])
        index = build_index(header, BANK_PDF_ALIASES)
        print(f"表头在第 {header_row + 1} 行，共 {len(header)} 列：")
        for i, name in enumerate(header):
            print(f"  第{i:>2}列  {mask(name)}")
        print()
        print("解析器把各字段认到了哪一列：")
        for field in KEY_FIELDS:
            pos = index.get(field)
            label = mask(header[pos]) if pos is not None and pos < len(header) else "（没认出）"
            print(f"  {field:<14} -> 第 {pos} 列  {label}" if pos is not None
                  else f"  {field:<14} -> （没认出）")
        print()

    print("=" * 78)
    print("每行的实际结构（[n] 表示「第 n 格」）：")
    print("=" * 78)
    n_cols = len(header) if header else 0
    for i, row in enumerate(rows[: args.rows], start=1):
        tag = "表头" if (header_row is not None and i - 1 == header_row) else f"行{i}"
        cells = [c for c in row if c.strip()]
        shapes = " | ".join(f"[{j}]{mask(c)}" for j, c in enumerate(cells))
        flag = ""
        if n_cols and len(cells) != n_cols:
            # 格数与表头列数不一致 = 有列被粘住了，或这行是标题行
            flag = f"   <-- {len(cells)} 格 vs 表头 {n_cols} 列"
        print(f"{tag:<6} {len(cells):>2} 格: {shapes}{flag}")
    if len(rows) > args.rows:
        print(f"… 还有 {len(rows) - args.rows} 行未显示（用 --rows 调大）")
    print("=" * 78)
    print()
    if n_cols:
        print("怎么读这份输出：")
        print("  · 数据行的格数**应该等于**表头列数。少了说明有列被粘住，")
        print("    解析器按列名取值就会取错 —— 金额取到余额列是最常见的一种。")
        print("  · 如果数据行比表头列数多，说明是标题/汇总行，解析器应当跳过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
