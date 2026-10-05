"""渠道识别：装着 PDF 的 zip。

为什么要有这个文件：
    真实事故 —— 用户把银行寄来的账单（一个装着加密 PDF 的 zip，
    文件名是 `26100501103672708650.zip` 这种流水号）丢进投递目录，
    程序报「识别不出渠道」。两个独立的坑叠在一起：

      1. `detect_source` 只认文件名关键词和 CSV 表头。
         流水号文件名两样都不沾，zip 里也没有表头可认，必然认不出。
      2. 就算手动 `--source bank_pdf`，`bank_pdf.extract_rows` 会把 zip
         直接扔给 pymupdf.open()。实测它**不报错**，而是安静地开出一个
         0 页的空文档 —— 于是后面走到「抽不到文字层」，被误判成扫描件。
         报错信息指向了完全错误的方向。

    修法：`sniff.open_document(path, password, suffixes)` 抽出统一的
    「读源文件」逻辑，`open_source` 只找表格后缀、`bank_pdf.extract_rows`
    只找 `.pdf`，各自传入自己要的 suffixes。裸文件路径完全不变。

    渠道记忆（手动指定一次以后自动认）是另一件事，
    见 `tests/test_source_hint.py`。

用法：<venv>\\Scripts\\python.exe tests/test_zip_pdf.py
返回码 0 表示全部通过。
"""

from __future__ import annotations

import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from importer.bank_pdf import PdfError, extract_rows  # noqa: E402
from importer.sniff import TABLE_SUFFIXES, open_document, open_source  # noqa: E402
from importer.sources import detect_source  # noqa: E402

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n         得到 {got!r}\n         期望 {want!r}")
        FAILURES.append(label)


def check_raises(label: str, exc_type, fn, *a, **kw) -> None:
    try:
        fn(*a, **kw)
    except exc_type:
        print(f"  ok   {label}")
    except Exception as exc:
        print(f"  FAIL {label}\n         抛的是 {type(exc).__name__}，不是 {exc_type.__name__}")
        FAILURES.append(label)
    else:
        print(f"  FAIL {label}\n         根本没抛异常")
        FAILURES.append(label)


def make_pdf_bytes() -> bytes:
    """造一份带文字层的合成 PDF。

    列名用 ASCII 而不是中文：合成 PDF 里没有中文字体，中文会被渲染成
    一串黑点，断言「表头里有交易日期」这种就会变成永远失败的假测试。
    这里要验的是「zip → PDF → 二维表」这条管道通不通，不是字体。
    真实中文表头的识别由 bank_pdf 自己的列名别名表负责。
    """
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    rows = [
        ("TXN_DATE", "CHANNEL", "REMARK", "COUNTERPARTY", "INCOME", "EXPENSE", "BALANCE"),
        ("2026-09-01", "POS", "SHOPPING", "ACME STORE", "", "35.20", "1200.00"),
        ("2026-09-02", "ATM", "WITHDRAW", "ATM CASH", "", "200.00", "1000.00"),
        ("2026-09-03", "TRANSFER", "SALARY", "ACME CORP", "5000.00", "", "6000.00"),
    ]
    y = 100
    for row in rows:
        x = 50
        for cell in row:
            page.insert_text((x, y), cell, fontsize=9)
            x += 110
        y += 18
    data = doc.tobytes()
    doc.close()
    return data


def write_zip(directory: str, outer: str, inner: str, payload: bytes) -> str:
    """造一个「装着 inner 的 zip」，外层文件名是 outer。

    两个名字分开给，是因为这个 bug 的本体就发生在这里：
    真实文件外层叫 `26100501103672708650.zip`（流水号），
    内层才是 `26100501103672708650.pdf`。测试里要是偷懒让内外同名，
    就造不出「外层没有渠道线索」这个前提，测了等于没测。
    """
    path = os.path.join(directory, outer)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(inner, payload)
    return path


def case_zip_of_pdf_detected():
    print("\n[1] 装着 PDF 的 zip 要认成 bank_pdf")
    with tempfile.TemporaryDirectory() as tmp:
        path = write_zip(tmp, "26100501103672708650.zip", "26100501103672708650.pdf", make_pdf_bytes())
        check("流水号文件名的 zip 认成 bank_pdf", detect_source(path), "bank_pdf")


def case_zip_of_pdf_parses():
    print("\n[2] 装着 PDF 的 zip 要能真的解出表格")
    with tempfile.TemporaryDirectory() as tmp:
        path = write_zip(tmp, "statement.zip", "statement.pdf", make_pdf_bytes())
        rows, meta = extract_rows(path)
        check("页数是 1（不是 0）", meta["pages"], 1)
        check("有文字层", meta["text_layer"], True)
        check("抽出了表头行", "TXN_DATE" in "".join(rows[0]), True)
        check("抽出了三行流水", len([r for r in rows if r and r[0].startswith("2026")]), 3)


def case_bare_pdf_still_works():
    print("\n[3] 裸 PDF 不能被改坏")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bank.pdf")
        with open(path, "wb") as fh:
            fh.write(make_pdf_bytes())
        check("裸 PDF 认成 bank_pdf", detect_source(path), "bank_pdf")
        rows, meta = extract_rows(path)
        check("裸 PDF 页数是 1", meta["pages"], 1)


def case_open_source_still_rejects_pdf_zip():
    print("\n[4] 表格链路不许把 PDF 当表格吃")
    with tempfile.TemporaryDirectory() as tmp:
        path = write_zip(tmp, "statement.zip", "statement.pdf", make_pdf_bytes())
        # open_source 只找表格。这是刻意的：PDF 走 bank_pdf 那条路，
        # 让它混进表格链路会在 decode_bytes 里炸出一堆乱码行。
        check_raises("open_source 拒绝装 PDF 的包", ValueError,
                     open_source, path, None)
        # 但 open_document 显式要 PDF 时就该拿到
        data, origin = open_document(path, None, (".pdf",))
        check("open_document 能取出 PDF 字节", data[:4], b"%PDF")
        check("来源说明写明在包内", "压缩包内" in origin, True)
        check("表格后缀列表不含 pdf", ".pdf" in TABLE_SUFFIXES, False)


def case_scan_failure_is_loud():
    print("\n[9] 假的 PDF 仍要报错，不能静默")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "broken.pdf")
        with open(path, "wb") as fh:
            fh.write(b"%PDF-1.4 this is not really a pdf")
        check_raises("坏 PDF 抛 PdfError", PdfError, extract_rows, path)


def main() -> int:
    case_zip_of_pdf_detected()
    case_zip_of_pdf_parses()
    case_bare_pdf_still_works()
    case_open_source_still_rejects_pdf_zip()
    case_scan_failure_is_loud()

    print()
    print("=" * 78)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
