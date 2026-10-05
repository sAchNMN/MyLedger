"""渠道识别 + 渠道记忆的回归测试。

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

    修完这两条之后还剩一个可用性问题：用户手动指定一次，下个月还得再指定。
    所以加了「渠道记忆」（sourcehint），键是形状而不是文件名 ——
    银行账单文件名每月都变，按文件名记等于没记。这条也钉住。

用法：<venv>\\Scripts\\python.exe tests/test_source_hint.py
返回码 0 表示全部通过。
"""

from __future__ import annotations

import io
import os
import sqlite3
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from importer import sourcehint  # noqa: E402
from importer.bank_pdf import PdfError, extract_rows  # noqa: E402
from importer.sniff import TABLE_SUFFIXES, open_document, open_source  # noqa: E402
from importer.sources import detect_source  # noqa: E402
from importer.store import Ledger  # noqa: E402

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
        check("形状键是 zip:pdf", sourcehint.shape_key(path), "zip:pdf")


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
        check("裸 PDF 形状键是 ext:pdf", sourcehint.shape_key(path), "ext:pdf")


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


def case_shape_key_variants():
    print("\n[5] 形状键的各种情况")
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = os.path.join(tmp, "账单.csv")
        with open(csv_path, "w", encoding="utf-8", newline="") as fh:
            fh.write("交易时间,金额\n2026-09-01,1.00\n")
        check("csv 的形状键", sourcehint.shape_key(csv_path), "ext:csv")

        noext = os.path.join(tmp, "weird")
        with open(noext, "w", encoding="utf-8") as fh:
            fh.write("x")
        check("无后缀退回按文件名", sourcehint.shape_key(noext), "name:weird")

        mixed = os.path.join(tmp, "mixed.zip")
        with zipfile.ZipFile(mixed, "w") as zf:
            zf.writestr("a.pdf", make_pdf_bytes())
            zf.writestr("b.pdf", make_pdf_bytes())
        check("包里多数后缀说了算", sourcehint.shape_key(mixed), "zip:pdf")


def case_hint_roundtrip():
    print("\n[6] 渠道记忆：指定 → 生效 → 撤销")
    led = Ledger(sqlite3.connect(":memory:"), ":memory:")
    with tempfile.TemporaryDirectory() as tmp:
        path = write_zip(tmp, "SNAP20260930-0001.zip", "bill9.pdf", make_pdf_bytes())
        key = sourcehint.shape_key(path)

        check("还没记时认不出（这里靠启发式能认出，"
              "所以单独测记忆是否压过启发式）",
              detect_source(path), "bank_pdf")

        # 记忆必须压过启发式：故意记一个跟启发式不一样的渠道。
        sourcehint.set_hint(led, key, "alipay")
        check("记忆压过启发式", detect_source(path, sourcehint.hint_map(led)), "alipay")
        check("resolve 读得到", sourcehint.resolve(led, path), "alipay")

        # 下个月文件名变了，形状没变 —— 这才是「记住」的意义
        other = write_zip(tmp, "SNAP20261031-0002.zip", "bill10.pdf", make_pdf_bytes())
        check("换了个流水号文件名，形状键不变", sourcehint.shape_key(other), key)
        check("新文件自动认成记住的渠道",
              detect_source(other, sourcehint.hint_map(led)), "alipay")

        # 但形状不同的文件不受影响
        csv_path = os.path.join(tmp, "账单.csv")
        with open(csv_path, "w", encoding="utf-8", newline="") as fh:
            fh.write("交易时间,金额\n2026-09-01,1.00\n")
        check("不同形状不受这条记忆影响",
              detect_source(csv_path, sourcehint.hint_map(led)),
              detect_source(csv_path))

        # 不传 hints 时退回纯启发式
        check("不传 hints 就走启发式", detect_source(path), "bank_pdf")

        check("撤销成功", sourcehint.clear_hint(led, key), True)
        check("撤销后不生效了",
              detect_source(path, sourcehint.hint_map(led)), "bank_pdf")
        check("撤销不存在的键返回 False", sourcehint.clear_hint(led, "ext:nope"), False)

    led.close()


def case_hint_validation():
    print("\n[7] 渠道记忆的参数校验")
    led = Ledger(sqlite3.connect(":memory:"), ":memory:")
    check_raises("不认识的渠道被拒", ValueError, sourcehint.set_hint,
                 led, "ext:csv", "paypal")
    check_raises("格式不对的键被拒", ValueError, sourcehint.set_hint,
                 led, "csv", "alipay")
    check("合法组合能存", sourcehint.set_hint(led, "ext:csv", "alipay"), "ext:csv")
    # 重复记同一个键是覆盖而不是报错 —— 用户改主意很正常
    check("重复记是覆盖", sourcehint.set_hint(led, "ext:csv", "wechat"), "ext:csv")
    check("覆盖后读到新值", sourcehint.hint_map(led)["ext:csv"], "wechat")
    led.close()


def case_hint_survives_encryption():
    print("\n[8] 记忆要跟着加密账本一起落盘（重新读磁盘核对）")
    from importer import secure_store
    with tempfile.TemporaryDirectory() as tmp:
        vault = os.path.join(tmp, "vault")
        os.makedirs(vault, exist_ok=True)
        with secure_store.open_encrypted(vault, save_on_exit=True) as led:
            sourcehint.set_hint(led, "zip:pdf", "bank_pdf")
        # 换一个进程/连接重新打开：同连接看到的是内存状态，不能算数
        with secure_store.open_encrypted(vault, save_on_exit=False) as led:
            check("记忆落进了密文", sourcehint.hint_map(led), {"zip:pdf": "bank_pdf"})


def case_scan_failure_is_loud():
    print("\n[9] 假的 PDF 仍要报错，不能静默")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "broken.pdf")
        with open(path, "wb") as fh:
            fh.write(b"%PDF-1.4 this is not really a pdf")
        check_raises("坏 PDF 抛 PdfError", PdfError, extract_rows, path)


def case_old_ledger_gets_table():
    print("\n[10] 老账本（没有 source_hint 表）打开时要自动补上")
    from importer import secure_store
    with tempfile.TemporaryDirectory() as tmp:
        vault = os.path.join(tmp, "vault")
        os.makedirs(vault, exist_ok=True)
        # 先用「老版本」的 SCHEMA 建一个库：把 source_hint 那段抠掉。
        import importer.store as store_mod
        old_schema = "\n".join(
            block for block in store_mod.SCHEMA.split("\n\n")
            if "source_hint" not in block
        )
        self_check = "CREATE TABLE IF NOT EXISTS source_hint"
        check("老 SCHEMA 里确实没有 source_hint", self_check in old_schema, False)

        conn = sqlite3.connect(":memory:")
        conn.executescript(old_schema)
        # 直接构造 Ledger(init_schema=False)，模拟 secure_store 那条路
        led = Ledger(conn, ":memory:", init_schema=False)
        check("打开即补上了表",
              "source_hint" in {r[0] for r in conn.execute(
                  "SELECT name FROM sqlite_master WHERE type='table'")}, True)
        check("补表后能直接记", sourcehint.set_hint(led, "ext:csv", "wechat"), "ext:csv")
        check("补表后能读回", sourcehint.hint_map(led), {"ext:csv": "wechat"})
        led.close()

        # 加密库那条路同样要能补（它是 init_schema=False 的另一个调用方）
        with secure_store.open_encrypted(vault, save_on_exit=True) as led2:
            sourcehint.set_hint(led2, "zip:pdf", "bank_pdf")
        with secure_store.open_encrypted(vault, save_on_exit=False) as led3:
            check("加密库也补上了并能读", sourcehint.hint_map(led3),
                  {"zip:pdf": "bank_pdf"})


def case_zip_path_equals_bare_path():
    print("\n[11] zip 装 PDF 必须和裸 PDF 走出一模一样的结果")
    # 这是本次修复的核心不变量：多包了一层不该改变任何解析结果。
    # 之前 zip 会被 pymupdf 安静地开成 0 页空文档，解析结果与裸 PDF 不同，
    # 而且报的是「扫描件」—— 报错方向完全错，用户会去查错的东西。
    from importer.sources import parse_bank_pdf
    with tempfile.TemporaryDirectory() as tmp:
        payload = make_pdf_bytes()
        bare = os.path.join(tmp, "b.pdf")
        with open(bare, "wb") as fh:
            fh.write(payload)
        zipped = write_zip(tmp, "b.zip", "b.pdf", payload)

        rows_bare, meta_bare = extract_rows(bare)
        rows_zip, meta_zip = extract_rows(zipped)
        check("抽出的行完全一致", rows_zip, rows_bare)
        check("页数一致", meta_zip["pages"], meta_bare["pages"])
        check("有文字层一致", meta_zip["text_layer"], meta_bare["text_layer"])

        res_bare = parse_bank_pdf(bare)
        res_zip = parse_bank_pdf(zipped)
        check("解析笔数一致", len(res_zip.txns), len(res_bare.txns))
        check("渠道一致", res_zip.source, res_bare.source)
        check("都不该被误判成扫描件", meta_zip["text_layer"], True)


def main() -> int:
    case_zip_of_pdf_detected()
    case_zip_of_pdf_parses()
    case_bare_pdf_still_works()
    case_open_source_still_rejects_pdf_zip()
    case_shape_key_variants()
    case_hint_roundtrip()
    case_hint_validation()
    case_hint_survives_encryption()
    case_scan_failure_is_loud()
    case_old_ledger_gets_table()
    case_zip_path_equals_bare_path()

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
