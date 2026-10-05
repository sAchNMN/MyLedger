"""打印 PDF 里每个字的**原始坐标**（脱敏），用来诊断列对齐。

为什么需要这个
--------------
`bank-pdf-columns.py` 只能告诉你「每行有几个格子」，看不到格子的
**横向位置**。而列对齐的正确做法完全取决于位置：

    表头第 i 列的中点 = 列心 -> 数据行的字归到最近的列心

如果你看到「数据行 7 格、表头 11 列」，有两种完全不同的原因，
处置方式也完全不同：
  A. `extract_rows` 压根没调用对齐（代码路径问题）
  B. 对齐调用了，但**列心算错了** —— 比如表头里「字」「字」「字」
     这三个 1 字宽的列把列心挤歪了，于是数据行的字归错列、
     几个字被串进同一格

**只看格数分不出这两种。** 这个脚本直接打印每个字的
`(行号, 列号近似, 脱敏文本, 左边界, 右边界)`，
一眼就能算出正确的列心该在哪。

隐私
----
数字→9、中文→字、字母→a（与 `bank-pdf-probe.py` 同一套脱敏，
所以同一个字在两个脚本的输出里长得一样，方便对照）。
真实金额、商户名、卡号一个都不显示。

用法：
    <venv>\\Scripts\\python.exe scripts\\bank-pdf-words.py "账单.pdf"
    <venv>\\Scripts\\python.exe scripts\\bank-pdf-words.py "账单.zip" --rows 14
"""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

_spec = importlib.util.spec_from_file_location(
    "bank_pdf_probe", os.path.join(HERE, "bank-pdf-probe.py"))
_probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_probe)
mask = _probe.mask

from importer.archive import ArchiveError  # noqa: E402
from importer.bank_pdf import PdfError, _require_pymupdf  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description="PDF 字坐标探针（输出脱敏，可安全外发）")
    ap.add_argument("pdf", help="PDF 路径（也可以给装着它的 zip）")
    ap.add_argument("--rows", type=int, default=14, help="最多显示多少行")
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

    pymupdf = _require_pymupdf()

    # 取 PDF 字节：裸文件直接读，zip 走 archive（自动降级到 7-Zip）
    import zipfile
    if zipfile.is_zipfile(args.pdf):
        from importer.sniff import open_document
        try:
            data, _ = open_document(args.pdf, password, (".pdf",))
        except ArchiveError as exc:
            print(f"读不了这个压缩包：{exc}")
            return 2
        doc = pymupdf.open(stream=data, filetype="pdf")
    else:
        try:
            doc = pymupdf.open(args.pdf)
        except Exception as exc:
            print(f"读不了这个 PDF：{exc}")
            return 2

    try:
        if getattr(doc, "needs_pass", False):
            if not password or not doc.authenticate(password):
                print("PDF 密码不对。")
                return 2

        for page_no, page in enumerate(doc, start=1):
            words = page.get_text("words")
            if not words:
                print(f"第 {page_no} 页没有文字层（扫描件？）")
                continue

            # 按 y 坐标分行（容差 3pt），行内按 x 排序
            rows: dict = {}
            for w in words:
                x0, y0, x1, y1, text = w[0], w[1], w[2], w[3], w[4]
                key = round(y0 / 3.0)
                rows.setdefault(key, []).append((y0, x0, x1, text))

            print("=" * 96)
            print(f"第 {page_no} 页    字总数 {len(words)}    行数 {len(rows)}")
            print("=" * 96)
            print("行   序  左边界   右边界   宽  文本")
            print("-" * 96)

            for r, key in enumerate(sorted(rows, key=lambda k: rows[k][0][0])[: args.rows]):
                items = sorted(rows[key], key=lambda t: t[1])
                for i, (_y, x0, x1, text) in enumerate(items):
                    print(f"{r + 1:<4} {i:>2}  {x0:>7.1f}  {x1:>7.1f}  "
                          f"{x1 - x0:>4.0f}  {mask(text)}")
                if r == 0:
                    print("   （上面这行若列数最多，它就是列锚的候选）")
                print()
    finally:
        doc.close()

    print("=" * 96)
    print("怎么读这份输出：")
    print("  1. 找**列数最多**的那一行 —— 它是列锚（通常是表头）。")
    print("  2. 记下它每一格的 **中点**（左边界+右边界)/2 —— 那是列心。")
    print("  3. 后面每一行的每个字，归到**最近的列心**。")
    print("  4. 如果某一行归完后列数与列锚不同，说明有字被串进同一格，")
    print("     或者有列一个字都没有（那一列要留空字符串，不能跳过）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
