"""解析失败记录的结构测试（ML-007）。

为什么必须有这个文件：

    解析失败的行以前长这样：

        {"row": 18, "reason": "金额解析失败：'商户_TEST 123.45'", "raw": "<整行前 120 字符>"}

    `raw` 是**整行原文**，`reason` 里又内嵌了原始单元格的值。
    后台执行器会把子进程的标准输出整段追加进 vault\\logs\\monthly-YYYYMM.log，
    而日志会被备份、同步、翻查 —— 于是一笔解析失败就把商户名、金额、单号、
    备注原封不动地写进了长期文本文件。

    这里把「失败记录能带什么」钉死：只允许行号 + 字段名 + 错误码。
    测试用的样本里塞满了标记串（商户_TEST / 123.45 / TEST-001 / TEST_NOTE），
    然后整份结果做一次字符串扫描 —— 任何一个标记串出现都算失败。

用法：<venv>\\Scripts\\python.exe tests/test_parse_issues.py
返回码 0 表示全部通过。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from importer import pipeline  # noqa: E402
from importer.model import describe_issue  # noqa: E402
from importer.pipeline import import_file  # noqa: E402
from importer.sources import _parse_bank_rows_auto, parse_alipay, parse_bank_ocr  # noqa: E402
from importer.store import Ledger  # noqa: E402

FAILURES: list[str] = []

# 这些串只出现在「失败那一行」里。它们一个都不许出现在解析结果中。
MARKERS = ("商户_TEST", "123.45", "TEST-001", "TEST_NOTE", "acct_TEST", "商品_TEST")

ALIPAY_HEADER = (
    "交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,"
    "收/付款方式,交易状态,交易订单号,商家订单号,备注"
)

PREAMBLE = [
    "支付宝交易记录明细查询",
    "共3笔记录",
    "特别提示：本回单仅供参考",
]

# 第 1 行正常；第 2 行金额是坏值；第 3 行时间是坏值。坏行里塞满标记串。
BAD_AMOUNT_ROW = (
    "2026-09-04 09:10:00,餐饮美食,商户_TEST,acct_TEST,商品_TEST,支出,"
    "不是数字,TEST-001,TEST-002,TEST-003,TEST-004,TEST_NOTE"
)
BAD_TS_ROW = (
    "不是时间,餐饮美食,商户_TEST,acct_TEST,商品_TEST,支出,"
    "123.45,TEST-001,TEST-002,TEST-003,TEST-004,TEST_NOTE"
)
GOOD_ROW = (
    "2026-09-05 20:00:00,日用百货,合成商户丙,ghi***@example.com,日用品,支出,"
    "63.20,余额,交易成功,SYN-P-0003,MO-SYN-0003,"
)


def check(name: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: 期望 {want!r}，实际 {got!r}")
    print(f"  [{'✓' if ok else '✗'}] {name:<46} -> {got!r}")


def write_csv(path: str, rows: list[str], header: str = ALIPAY_HEADER) -> str:
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("\n".join(PREAMBLE + [header] + rows) + "\n")
    return path


def scan_for_markers(blob: str) -> list[str]:
    return [m for m in MARKERS if m in blob]


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="myledger-issues-")
    try:
        return run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(tmp: str) -> int:
    print()
    print("=" * 78)
    print("坏行：金额/时间解析失败")
    print("=" * 78)
    path = write_csv(os.path.join(tmp, "坏行.csv"), [GOOD_ROW, BAD_AMOUNT_ROW, BAD_TS_ROW])
    result = parse_alipay(path)
    check("正常行仍解析出来", len(result.txns), 1)
    check("两行被记为失败", len(result.skipped), 2)
    check("金额坏行的错误码",
          result.skipped[0].get("reason_code"), "invalid_amount")
    check("金额坏行标出字段", result.skipped[0].get("field"), "amount")
    check("时间坏行的错误码",
          result.skipped[1].get("reason_code"), "invalid_datetime")
    check("时间坏行标出字段", result.skipped[1].get("field"), "ts")
    check("行号是文件里的真实行号（1 基）",
          [r["row"] for r in result.skipped], [6, 7])
    check("失败记录里没有 raw 键",
          [k for r in result.skipped for k in r if k == "raw"], [])

    print()
    print("=" * 78)
    print("敏感内容扫描：整份结果里不许出现那一行的任何字段")
    print("=" * 78)
    blob = json.dumps(result.skipped, ensure_ascii=False) + json.dumps(
        [t.txn_id for t in result.txns], ensure_ascii=False)
    check("标记串一个都不出现", scan_for_markers(blob), [])

    print()
    print("=" * 78)
    print("表头缺失：找不到表头行")
    print("=" * 78)
    path = os.path.join(tmp, "没表头.csv")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("这不是账单\n商户_TEST 123.45 TEST-001 TEST_NOTE\n")
    result = parse_alipay(path)
    check("错误码", result.skipped[0].get("reason_code"), "missing_header")
    check("没有交易", len(result.txns), 0)
    check("敏感内容不泄漏",
          scan_for_markers(json.dumps(result.skipped, ensure_ascii=False)), [])

    print()
    print("=" * 78)
    print("表头缺必需列：认得出是表头，但没有金额列")
    print("=" * 78)
    no_amount_header = "交易时间,交易类型,交易对方,商品,交易单号,商户单号,备注"
    row = "2026-09-04 09:10:00,商户消费,商户_TEST,商品_TEST,TEST-001,TEST-002,TEST_NOTE"
    path = write_csv(os.path.join(tmp, "缺列.csv"), [row], header=no_amount_header)
    result = parse_alipay(path)
    check("错误码", result.skipped[0].get("reason_code"), "missing_required_column")
    check("点明缺的是哪个字段", result.skipped[0].get("fields"), ["amount"])
    check("不再回显实际表头（表头也可能带脏数据）",
          "实际表头" in json.dumps(result.skipped, ensure_ascii=False), False)
    check("敏感内容不泄漏",
          scan_for_markers(json.dumps(result.skipped, ensure_ascii=False)), [])

    print()
    print("=" * 78)
    print("银行截图自动识别路径：同样不许带原始行")
    print("=" * 78)
    tsv = os.path.join(tmp, "银行.tsv")
    with open(tsv, "w", encoding="utf-8", newline="") as fh:
        fh.write("2026-09-05 合成商户丙 63.20\n")
        fh.write("09-15 商户_TEST TEST-001 TEST_NOTE\n")      # 有日期没金额
        fh.write("2026-02-30 商户_TEST 123.45 TEST-001\n")     # 日期不合法
    result = parse_bank_ocr(tsv)
    codes = [r.get("reason_code") for r in result.skipped]
    check("有日期没金额 → invalid_amount", "invalid_amount" in codes, True)
    check("日期不合法 → invalid_datetime", "invalid_datetime" in codes, True)
    check("失败记录里没有 raw 键",
          [k for r in result.skipped for k in r if k == "raw"], [])
    check("敏感内容不泄漏",
          scan_for_markers(json.dumps(result.skipped, ensure_ascii=False)), [])
    check("成功那笔仍解析出来", len(result.txns), 1)

    print()
    print("=" * 78)
    print("打印出来的文字：只能有行号 + 字段 + 错误类型")
    print("=" * 78)
    check("金额坏行的人话", describe_issue({"row": 18, "reason_code": "invalid_amount",
                                          "field": "amount"}),
          "第 18 行：金额字段格式无效")
    check("时间坏行的人话", describe_issue({"row": 7, "reason_code": "invalid_datetime",
                                          "field": "ts"}),
          "第 7 行：时间字段格式无效")
    check("缺列的人话", describe_issue({"row": 4, "reason_code": "missing_required_column",
                                      "fields": ["amount"]}),
          "第 4 行：表头缺少必需列（金额）")
    check("不认识的码原样打出来（不猜内容）",
          describe_issue({"row": 9, "reason_code": "some_new_code"}),
          "第 9 行：some_new_code")

    path = write_csv(os.path.join(tmp, "打印扫描.csv"), [GOOD_ROW, BAD_AMOUNT_ROW, BAD_TS_ROW])
    lines: list[str] = []
    ledger = Ledger(sqlite3.connect(":memory:"), os.path.join(tmp, "ledger.db.enc"))
    outcome = import_file(path, ledger, source="alipay", archive=False, emit=lines.append)
    printed = "\n".join(lines)
    check("导入成功（坏行只影响自己）", outcome.status, "success")
    check("打印里有行号和错误类型", "第 6 行：金额字段格式无效" in printed, True)
    check("打印里没有 raw 字样", "raw" in printed, False)
    check("打印文本里一个标记串都没有", scan_for_markers(printed), [])

    # ------------------------------------------------------------------
    # 「0 笔」必须能区分两种原因
    #
    # 真实事故：用户导入银行 PDF（zip 里那份），AES 解密已经修好了，
    # 但日志只有两句「解析成功 0 笔 / 跳过 0 行」，用户完全不知道该干什么。
    #
    # 那 0 笔有两种天差地别的来源：
    #   A. 文件里连一行流水都没有（导出残缺）→ 重新导出就行
    #   B. 文件里有行，但一个字段都没认出来    → 要调解析规则
    # 以前两者报同一句话，处置方式却是相反的。
    # 病根：`_parse_bank_rows_auto` 里 `if not dates: continue` 把行静默丢了，
    # 连 skipped 都不记 —— 于是 B 情况下 skipped 也是空的，
    # 和 A 完全无法区分。
    # ------------------------------------------------------------------
    print()
    print("=" * 78)
    print("0 笔：区分「文件没数据」和「规则没认出来」")
    print("=" * 78)

    # B：有金额没日期（8 位紧凑写法，日期 token 认不出来）
    compact = [["20260903", "POS消费", "商户_TEST", "-38.50", "1234.56"],
               ["20260904", "ATM取现", "商户_TEST", "-200.00", "1034.56"]]
    res_b = _parse_bank_rows_auto(compact, "x.pdf", source="bank_pdf")
    check("B: 解析 0 笔", len(res_b.txns), 0)
    check("B: 两行都记进了 skipped（不再是静默丢弃）", len(res_b.skipped), 2)
    check("B: 原因码是 no_date",
          sorted({s["reason_code"] for s in res_b.skipped}), ["no_date"])
    check("B: sample 是骨架不是原文",
          res_b.skipped[0]["sample"],
          "99999999 | aaa字字 | 字字_aaaa | -99.99 | 9999.99")
    check("B: sample 里没有标记串", scan_for_markers(str(res_b.skipped)), [])

    # A：只有表头，没有流水行
    res_a = _parse_bank_rows_auto([["交易日期", "交易摘要", "交易金额"]], "x.pdf",
                                  source="bank_pdf")
    check("A: 解析 0 笔", len(res_a.txns), 0)
    check("A: skipped 也是空的（与 B 可区分）", len(res_a.skipped), 0)

    # 报错文案必须把两者分开说
    ledger = Ledger(sqlite3.connect(":memory:"), os.path.join(tmp, "ledger2.db.enc"))
    lines: list[str] = []

    class _R:
        """假装成一个「有行但全认不出」的解析结果，走一遍 pipeline 的报错分支。"""
        txns = []
        declared_count = None
        skipped = res_b.skipped
        path = "账单.zip"
        summary = lambda self: ""

    orig = pipeline.PARSERS
    pipeline.PARSERS = {"bank_pdf": lambda p, pw=None: _R()}
    try:
        outcome = pipeline.import_file(os.path.join(tmp, "账单.zip"), ledger,
                                       source="bank_pdf", archive=False,
                                       emit=lines.append)
    finally:
        pipeline.PARSERS = orig
    printed_b = "\n".join(lines)
    check("导入失败", outcome.ok, False)
    check("B 的报错说「有行但没认出来」", "解析规则没认出来" in printed_b, True)
    check("B 的报错指向了结构探针", "bank-pdf-probe" in printed_b, True)
    check("B 的报错不含标记串", scan_for_markers(printed_b), [])

    lines2: list[str] = []
    class _R2(_R):
        skipped = []
    pipeline.PARSERS = {"bank_pdf": lambda p, pw=None: _R2()}
    try:
        pipeline.import_file(os.path.join(tmp, "账单2.zip"), ledger, source="bank_pdf",
                             archive=False, emit=lines2.append)
    finally:
        pipeline.PARSERS = orig
    printed_a = "\n".join(lines2)
    check("A 的报错说「连一行流水都没有」", "连一行像流水的行都没有" in printed_a, True)
    check("A 的报错建议重新导出", "重新导出" in printed_a, True)
    check("A 与 B 的报错文案不同", printed_a != printed_b, True)

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
