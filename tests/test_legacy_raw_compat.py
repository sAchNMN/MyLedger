"""老库兼容与生产模式隐私回归（ML-010 的后半）。

为什么必须有这个文件：

    改 raw 的存储格式是件危险的事 —— 已经入库的数据还是老格式，
    而 fixups.recompute 靠 raw 重推收支方向。改错了的症状是
    「跑一次 recompute，一批支出从报表里消失了」，而且没有任何报错。

    所以这里构造三种真实存在过的 raw 形态：

        1. 老格式：raw["原始"] 是整行列表，方向列按下标取
        2. 中间态：列数变了，下标越界，退回按值搜
        3. 更早的：raw 干脆是 {}（schema 的默认值就是 '{}'）

    第 3 种是这次顺手发现的坑：原来的代码在「查不到方向值」时会继续往下走，
    parse_direction("") 返回**中性** —— 等于把一笔真实的支出从统计里抹掉。
    查不到就别猜，保持原样，这才是安全的方向。

    另外这里也做一次全库扫描：导入一份「报表用不到的列里塞满标记串」的
    合成账单，然后把所有表的所有列拼起来找这些串。

用法：<venv>\\Scripts\\python.exe tests/test_legacy_raw_compat.py
返回码 0 表示全部通过。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from importer import fixups  # noqa: E402
from importer.model import Txn  # noqa: E402
from importer.pipeline import import_file  # noqa: E402
from importer.store import Ledger  # noqa: E402

FAILURES: list[str] = []

BASE = dt.datetime(2026, 9, 3, 12, 0, 0)

ACCT_MARKER = "acct_TEST_9911"
CAT_MARKER = "cat_TEST_8822"
EXTRA_MARKER = "extra_TEST_7733"
MARKERS = (ACCT_MARKER, CAT_MARKER, EXTRA_MARKER)

# 报表用不到的列（对方账号 / 交易分类 / 扩展列）里塞标记串
HEADER = (
    "交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,"
    "收/付款方式,交易状态,交易订单号,商家订单号,备注,扩展列"
)
PREAMBLE = ["支付宝交易记录明细查询", "共2笔记录", "特别提示：本回单仅供参考"]
ROWS = [
    f"2026-09-03 12:31:05,{CAT_MARKER},合成便利店,{ACCT_MARKER},日用品,支出,"
    f"28.50,余额宝,交易成功,SYN-L-0001,MO-SYN-0001,普通备注,{EXTRA_MARKER}",
    f"2026-09-04 09:10:00,{CAT_MARKER},合成早餐店,{ACCT_MARKER},包子豆浆,支出,"
    f"19.90,花呗,交易成功,SYN-L-0002,MO-SYN-0002,普通备注,{EXTRA_MARKER}",
]


def check(name: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: 期望 {want!r}，实际 {got!r}")
    print(f"  [{'✓' if ok else '✗'}] {name:<46} -> {got!r}")


def txn(offset_min: int, amount: str, counterparty: str, txn_id: str,
        source: str = "alipay") -> Txn:
    return Txn(source=source, ts=BASE + dt.timedelta(minutes=offset_min),
               amount=Decimal(amount), direction="expense",
               counterparty=counterparty, item="日用品", txn_id=txn_id)


def make_ledger(rows: list[Txn]) -> Ledger:
    ledger = Ledger(sqlite3.connect(":memory:", check_same_thread=False), ":memory:")
    ledger.start_batch("alipay", "合成账单.csv", parsed=len(rows))
    for row in rows:
        ledger.add(row)
    return ledger


def set_legacy_raw(ledger: Ledger, txn_id: int, cells, source: str | None = None,
                   direction: str = "expense") -> None:
    """把某一行改造成老格式：raw["原始"] 放整行，方向只存在那一行里。"""
    raw: dict = {"来源": "直接读取", "编码": "utf-8-sig"}
    if cells is not None:
        raw["原始"] = cells
    ledger.conn.execute("UPDATE txn SET raw = ? WHERE id = ?",
                        (json.dumps(raw, ensure_ascii=False), txn_id))
    ledger.conn.execute("UPDATE txn SET direction = ? WHERE id = ?",
                        (direction, txn_id))
    if source:
        ledger.conn.execute("UPDATE txn SET source = ? WHERE id = ?", (source, txn_id))
    ledger.conn.commit()


def direction_of(ledger: Ledger, txn_id: int) -> str:
    return ledger.conn.execute(
        "SELECT direction FROM txn WHERE id = ?", (txn_id,)).fetchone()["direction"]


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="myledger-legacy-")
    try:
        return run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(tmp: str) -> int:
    quiet = lambda *_: None  # noqa: E731

    print()
    print("=" * 78)
    print("老格式 1：raw[\"原始\"] 还在，方向列按下标取")
    print("=" * 78)
    ledger = make_ledger([txn(1, "50.00", "合成便利店", "SYN-LG-0001")])
    cells = ["2026-09-03 12:01:00", "日用百货", "合成便利店", "acct***", "日用品",
             "收入", "50.00", "余额", "交易成功", "SYN-LG-0001", "MO-1", ""]
    set_legacy_raw(ledger, 1, cells, direction="expense")
    fixups.recompute(ledger, apply=True, emit=quiet)
    check("按原始行里的方向列重推成收入", direction_of(ledger, 1), "income")

    print()
    print("=" * 78)
    print("老格式 2：列数变了，下标越界 → 退回按值搜")
    print("=" * 78)
    ledger = make_ledger([txn(2, "60.00", "合成超市", "SYN-LG-0002")])
    # 只有 3 个格子，第 5 列根本不存在；方向值混在中间
    set_legacy_raw(ledger, 1, ["合成超市", "支出", "60.00"], direction="income")
    fixups.recompute(ledger, apply=True, emit=quiet)
    check("按值搜到的方向生效", direction_of(ledger, 1), "expense")

    print()
    print("=" * 78)
    print("老格式 3：raw 是 {}（schema 默认值）—— 查不到就别猜")
    print("=" * 78)
    ledger = make_ledger([txn(3, "70.00", "合成药店", "SYN-LG-0003")])
    set_legacy_raw(ledger, 1, None, direction="expense")
    fixups.recompute(ledger, apply=True, emit=quiet)
    check("支出还是支出（不许被猜成中性）", direction_of(ledger, 1), "expense")

    print()
    print("=" * 78)
    print("微信：方向列是空值 —— 空值本身就是「中性」的意思")
    print("=" * 78)
    ledger = make_ledger([txn(4, "80.00", "合成商户", "SYN-LG-0004", source="wechat")])
    wx_cells = ["2026-09-03 12:04:00", "商户消费", "合成商户", "日用品",
                "", "80.00", "零钱", "支付成功", "SYN-LG-0004", "MO-4", ""]
    set_legacy_raw(ledger, 1, wx_cells, direction="expense")
    fixups.recompute(ledger, apply=True, emit=quiet)
    check("空的方向列仍然是中性", direction_of(ledger, 1), "neutral")

    print()
    print("=" * 78)
    print("非法来源：没有方向列概念的来源（银行）必须保持原样")
    print("=" * 78)
    ledger = make_ledger([txn(5, "90.00", "合成商户", "SYN-LG-0005", source="bank_pdf")])
    set_legacy_raw(ledger, 1, None, direction="income")
    fixups.recompute(ledger, apply=True, emit=quiet)
    check("银行来源的方向不被改", direction_of(ledger, 1), "income")

    print()
    print("=" * 78)
    print("新格式照常工作：raw 里有「方向列」")
    print("=" * 78)
    ledger = make_ledger([txn(6, "20.00", "合成商户", "SYN-LG-0006")])
    ledger.conn.execute(
        "UPDATE txn SET raw = ?, direction = 'expense' WHERE id = 1",
        (json.dumps({"来源": "直接读取", "方向列": "收入"}, ensure_ascii=False),))
    ledger.conn.commit()
    fixups.recompute(ledger, apply=True, emit=quiet)
    check("新格式的方向列生效", direction_of(ledger, 1), "income")

    print()
    print("=" * 78)
    print("生产模式全库扫描：一份合成账单导入后，所有表的所有列")
    print("=" * 78)
    bill = os.path.join(tmp, "合成账单.csv")
    with open(bill, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("\n".join(PREAMBLE + [HEADER] + ROWS) + "\n")

    ledger = Ledger(sqlite3.connect(":memory:"), os.path.join(tmp, "ledger.db.enc"))
    import_file(bill, ledger, source="alipay", archive=False, emit=quiet)
    tables = [r[0] for r in ledger.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")]
    dump: list[str] = []
    for table in tables:
        for row in ledger.conn.execute(f"SELECT * FROM {table}"):
            dump.append(str(tuple(row)))
    blob = "\n".join(dump)
    check("扫描的表不止 txn", len(tables) >= 3, True)
    check("全库找不到对方账号标记", ACCT_MARKER in blob, False)
    check("全库找不到交易分类标记", CAT_MARKER in blob, False)
    check("全库找不到扩展列标记", EXTRA_MARKER in blob, False)
    check("一个标记串都没有", [m for m in MARKERS if m in blob], [])

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
