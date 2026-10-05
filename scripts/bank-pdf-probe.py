"""看一份银行 PDF 的**结构**，不暴露任何内容。

为什么需要这个：
    每家银行的 PDF 排版都不一样（列的顺序、日期写法、有没有余额列）。
    要写解析规则就得知道结构，但结构信息不该以「把账单内容贴出来」的方式获取。

这个脚本只输出**骨架**：
    数字 → 9      中文 → 字      字母 → a      空格 → _      标点原样保留

所以「2026-09-03  午餐  -28.50  1234.56」会显示成
    「9999-99-99  字字  -99.99  9999.99」

长度、列数、标点位置、金额的小数位全都保留，但真实值一个都不暴露。
这份输出可以安全地贴给任何人，包括给 AI 帮你调解析规则。

用法：
    <venv>\\Scripts\\python.exe scripts\\bank-pdf-probe.py "账单.pdf"
    <venv>\\Scripts\\python.exe scripts\\bank-pdf-probe.py "账单.pdf" --rows 40
    <venv>\\Scripts\\python.exe scripts\\bank-pdf-probe.py "账单.zip"   ← zip 也行

密码在运行时提示输入，不回显、不保存、不落盘。
（zip 的密码与里面那份 PDF 若是同一套，一次输入就够；
  7-Zip 不读 stdin，所以密码会出现在命令行里 —— 单机自用可接受。）
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from importer.bank_pdf import PdfError, extract_rows  # noqa: E402
from importer.archive import ArchiveError  # noqa: E402

CJK_RANGE = ("\u4e00", "\u9fff")


def mask(text: str) -> str:
    """只暴露结构：数字→9，中文→字，字母→a，空格→_，标点原样。"""
    out = []
    for ch in text:
        if ch.isdigit():
            out.append("9")
        elif ch.isascii() and ch.isalpha():
            out.append("a")
        elif CJK_RANGE[0] <= ch <= CJK_RANGE[1]:
            out.append("字")
        elif ch.isspace():
            out.append("_")
        else:
            out.append(ch)
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="银行 PDF 结构探针（输出全部脱敏）")
    ap.add_argument("pdf", help="PDF 路径（也可以直接给装着它的 zip）")
    ap.add_argument("--rows", type=int, default=30, help="最多显示多少行（默认 30）")
    ap.add_argument("--password", default=None,
                    help="密码。不传则交互提示（推荐：交互输入不会留在命令历史里）")
    args = ap.parse_args()

    if not os.path.isfile(args.pdf):
        print(f"找不到文件：{args.pdf}")
        return 2

    password = args.password
    if password is None:
        try:
            hint = ""
            if args.pdf.lower().endswith(".zip"):
                hint = "（zip 的密码；里面那份 PDF 若同密码，此处一次就够）"
            password = getpass.getpass(f"密码（不回显，不保存）{hint}：")
        except (KeyboardInterrupt, EOFError):
            print("\n已取消。")
            return 2

    try:
        rows, meta = extract_rows(args.pdf, password)
    except ArchiveError as exc:
        # zip 解不开就说 zip 的事，别包装成「读不了这个 PDF」——
        # 那会把「zip 解不开」误报成「PDF 有问题」，方向全错。
        print(f"读不了这个压缩包：{exc}")
        return 2
    except PdfError as exc:
        print(f"读不了这个 PDF：{exc}")
        return 2

    print("=" * 72)
    print(f"文件      : {os.path.basename(args.pdf)}")
    print(f"大小      : {os.path.getsize(args.pdf):,} 字节")
    print(f"页数      : {meta.get('pages')}")
    print(f"加密      : {'是（已用密码解开）' if meta.get('encrypted') else '否'}")
    print(f"抽出      : {len(rows)} 行")
    print(f"单元格数  : {sum(len(r) for r in rows)}")
    print("=" * 72)
    print()
    print("以下是**脱敏骨架**：9=数字  字=中文  a=字母  _=空格，标点原样保留。")
    print("真实金额、商户名、卡号一个都不会显示。")
    print()
    print("-" * 72)
    for i, row in enumerate(rows[: args.rows], start=1):
        cells = [c for c in row if c.strip()]
        print(f"行{i:>3} ({len(cells):>2}格): " + " | ".join(mask(c) for c in cells))
    if len(rows) > args.rows:
        print(f"… 还有 {len(rows) - args.rows} 行未显示（用 --rows 调大）")
    print("-" * 72)

    # 结构统计：帮判断这份账单“长什么样”，同样不含真实值
    import re
    date_like = re.compile(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,2}")
    amt_like = re.compile(r"[¥￥$]?-?\d[\d,]*\.\d{2}")
    n_date = sum(1 for r in rows for c in r if date_like.search(c))
    n_amt = sum(1 for r in rows for c in r if amt_like.search(c))
    cells_per_row = [len([c for c in r if c.strip()]) for r in rows]
    print()
    print("结构统计：")
    print(f"  含日期形态的格 : {n_date}")
    print(f"  含两位小数的格 : {n_amt}")
    print(f"  每行格数分布   : {sorted(set(cells_per_row))}")
    print()
    print("把这些贴给我就能对着调解析规则了——里面没有任何真实值。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
