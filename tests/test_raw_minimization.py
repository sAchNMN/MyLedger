"""raw 字段最小化的测试（ML-010）。

为什么必须有这个文件：

    Txn.raw 以前把**整行原样**存进数据库：

        raw={"来源": origin, "编码": encoding, "数据行": row_no, "原始": row}

    账本因此成了「账单的一份完整副本 + 一份解析结果」。
    账单里那些报表根本用不到的列 —— 对方账号、交易分类原文、
    以及银行/平台随手加出来的扩展列 —— 全部被一并永久保存下来。

    更麻烦的是它的性质：raw 看起来像「调试信息」，所以没人会去清理它；
    而它实际上是最完整的那份原始数据。

    这里把 raw 的允许内容钉死：来源信息 + 行号 + **形状摘要**。
    形状摘要（字段数、非空字段数、某列有没有值）足以定位
    「格式变了」和「切错列了」这两类故障，却读不出任何一笔消费。

    唯一保留的原始单元格值是「方向列」：fixups 靠它重推收支方向，
    而它的取值只有「支出 / 收入 / 不计收支」这几种固定词。

用法：<venv>\\Scripts\\python.exe tests/test_raw_minimization.py
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

from importer.fixups import _derive_direction  # noqa: E402
from importer.pipeline import import_file  # noqa: E402
from importer.sources import parse_alipay, parse_bank_ocr  # noqa: E402
from importer.store import Ledger  # noqa: E402

FAILURES: list[str] = []

# 只出现在「报表用不到的列」里的标记串。
# 对方账号、交易分类、扩展列 —— 解析器不会把它们映射到任何 Txn 字段，
# 所以它们出现在库里，只可能是 raw 把整行抄了进去。
ACCT_MARKER = "acct_TEST_9911"
CAT_MARKER = "cat_TEST_8822"
EXTRA_MARKER = "extra_TEST_7733"
MARKERS = (ACCT_MARKER, CAT_MARKER, EXTRA_MARKER)

# 比标准支付宝表头多一列「扩展列」：模拟平台改版随手加出来的列
HEADER = (
    "交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,"
    "收/付款方式,交易状态,交易订单号,商家订单号,备注,扩展列"
)
PREAMBLE = ["支付宝交易记录明细查询", "共2笔记录", "特别提示：本回单仅供参考"]

ROWS = [
    f"2026-09-03 12:31:05,{CAT_MARKER},合成商户甲,{ACCT_MARKER},午餐,支出,"
    f"28.50,余额宝,交易成功,SYN-X-0001,MO-SYN-0001,普通备注,{EXTRA_MARKER}",
    f"2026-09-04 09:10:00,{CAT_MARKER},合成商户乙,{ACCT_MARKER},打车,支出,"
    f"19.90,花呗,交易成功,SYN-X-0002,MO-SYN-0002,普通备注,{EXTRA_MARKER}",
]


def check(name: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: 期望 {want!r}，实际 {got!r}")
    print(f"  [{'✓' if ok else '✗'}] {name:<46} -> {got!r}")


def write_csv(path: str) -> str:
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("\n".join(PREAMBLE + [HEADER] + ROWS) + "\n")
    return path


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="myledger-raw-")
    try:
        return run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(tmp: str) -> int:
    path = write_csv(os.path.join(tmp, "合成账单.csv"))

    print()
    print("=" * 78)
    print("raw 里不再有整行原文")
    print("=" * 78)
    result = parse_alipay(path)
    check("解析出 2 笔", len(result.txns), 2)
    for index, txn in enumerate(result.txns, start=1):
        check(f"第 {index} 笔的 raw 没有「原始」键", "原始" in txn.raw, False)

    raw = result.txns[0].raw
    check("保留了来源说明", raw.get("来源"), "直接读取")
    check("保留了编码（排障要看）", bool(raw.get("编码")), True)
    check("保留了表头行号", raw.get("表头行"), 4)
    check("保留了数据行号", raw.get("数据行"), 5)
    check("保留了方向列（fixups 要用）", raw.get("方向列"), "支出")

    print()
    print("=" * 78)
    print("形状摘要：能定位故障，读不出内容")
    print("=" * 78)
    check("字段数", raw.get("字段数"), 13)
    check("非空字段数", raw.get("非空字段数"), 13)
    check("金额列有值", raw.get("金额列有值"), True)
    check("时间列有值", raw.get("时间列有值"), True)

    blob = json.dumps([t.raw for t in result.txns], ensure_ascii=False)
    check("raw 里没有对方账号标记", ACCT_MARKER in blob, False)
    check("raw 里没有交易分类标记", CAT_MARKER in blob, False)
    check("raw 里没有扩展列标记", EXTRA_MARKER in blob, False)
    check("raw 里一个标记串都没有", [m for m in MARKERS if m in blob], [])

    print()
    print("=" * 78)
    print("银行截图路线同样如此")
    print("=" * 78)
    tsv = os.path.join(tmp, "银行.tsv")
    with open(tsv, "w", encoding="utf-8", newline="") as fh:
        # OCR 产物是真 TSV（bank-ocr.ps1 写制表符），这里必须一样
        fh.write(f"2026-09-05\t合成商户丙\t63.20\t{EXTRA_MARKER}\n")
    bank = parse_bank_ocr(tsv)
    check("解析出 1 笔", len(bank.txns), 1)
    check("没有「原始」键", "原始" in bank.txns[0].raw, False)
    check("有形状摘要", bank.txns[0].raw.get("字段数"), 4)
    check("标记串没进去",
          EXTRA_MARKER in json.dumps(bank.txns[0].raw, ensure_ascii=False), False)

    print()
    print("=" * 78)
    print("落库之后：整张 txn 表的文本里都不该有这些标记")
    print("=" * 78)
    ledger = Ledger(sqlite3.connect(":memory:"), os.path.join(tmp, "ledger.db.enc"))
    outcome = import_file(path, ledger, source="alipay", archive=False,
                          emit=lambda _: None)
    check("导入成功", outcome.status, "success")
    check("入库 2 笔", outcome.inserted, 2)

    dump = "\n".join(
        str(tuple(row)) for row in ledger.conn.execute("SELECT * FROM txn"))
    check("txn 表里没有对方账号标记", ACCT_MARKER in dump, False)
    check("txn 表里没有扩展列标记", EXTRA_MARKER in dump, False)

    # 「交易分类」这一列的处置在 ML-011 变过：以前是**整列丢掉**，
    # 现在它是 platform_category 这个正式字段（平台已经判好的分类，
    # 比关键词规则准，见 importer/platformmap.py）。
    #
    # 所以标记串出现在 platform_category 里是**预期行为**，不是泄漏。
    # 这里要守的是另一条、也是更重要的那条：它只能待在那个字段里，
    # 不许出现在 raw、不许出现在任何别的列。
    check("交易分类原文被存进正式字段 platform_category",
          ledger.conn.execute(
              "SELECT platform_category FROM txn ORDER BY id LIMIT 1"
          ).fetchone()["platform_category"], CAT_MARKER)
    raw_only = "\n".join(
        str(tuple(row)) for row in
        ledger.conn.execute("SELECT raw FROM txn"))
    check("raw 里仍然没有交易分类标记（raw 只留白名单形态）",
          CAT_MARKER in raw_only, False)
    others = "\n".join(
        str(tuple(row)) for row in ledger.conn.execute(
            "SELECT source, txn_id, merchant_order_id, ts, amount, direction,"
            " counterparty, item, method, status, note, category, fingerprint,"
            " review, review_note, note FROM txn"))
    check("除 platform_category 外没有别的列带这个标记",
          CAT_MARKER in others, False)

    print()
    print("=" * 78)
    print("fixups 靠 raw 重推方向的能力不能被削掉")
    print("=" * 78)
    row = ledger.conn.execute("SELECT * FROM txn ORDER BY id LIMIT 1").fetchone()
    check("从新格式的 raw 能重推出方向", _derive_direction(row), "expense")

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
