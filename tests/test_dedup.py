"""三层去重的回归测试。

为什么必须有这个文件：
    L3（模糊匹配）整整一层曾经是坏的 —— 代码里引用了**从来不存在**的
    `row["ts_dt"]` 键，一执行就抛 IndexError。而当时所有测试都是绿的：
      * fixtures 那两笔在 L2（内容指纹一致）就 return 了，走不到 L3
      * 合成 PDF 的 9 笔金额互不相同，也走不到 L3
    于是「三层去重」实际上只有两层在跑，直到真实数据里出现
    「金额相同、时间相近」的两笔，整个导入当场崩溃。

    教训：一条分支只要没被专门构造的数据走过，就等于没写。
    这个文件专门构造「必须走到 L3」的数据，把三层各自钉死。

用法：<venv>\\Scripts\\python.exe tests/test_dedup.py
返回码 0 表示全部通过。
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from importer.model import Txn  # noqa: E402
from importer.store import Ledger  # noqa: E402

FAILURES: list[str] = []


def fresh_ledger() -> Ledger:
    led = Ledger(sqlite3.connect(":memory:"), ":memory:")
    led.start_batch("test", "test-batch", parsed=0)
    return led


def txn(*, ts, amount, counterparty, txn_id="", source="alipay", item=""):
    return Txn(
        source=source,
        ts=ts,
        amount=Decimal(amount),
        direction="expense",
        counterparty=counterparty,
        item=item,
        txn_id=txn_id,
    )


def check(name: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: 期望 {want}，实际 {got}")
    print(f"  [{'✓' if ok else '✗'}] {name:<52} -> {got}")


BASE = dt.datetime(2026, 9, 3, 12, 0, 0)


def main() -> int:
    print()
    print("=" * 78)
    print("L1：同源 + 同交易单号 → identical（自动跳过，不进库）")
    print("=" * 78)
    led = fresh_ledger()
    led.add(txn(ts=BASE, amount="28.50", counterparty="某某商户", txn_id="T001"))
    v = led.add(txn(ts=BASE, amount="28.50", counterparty="某某商户", txn_id="T001"))
    check("同单号再导入一次", v.verdict, "identical")
    check("  库内笔数（不应增加）", led.conn.execute("SELECT COUNT(*) FROM txn").fetchone()[0], 1)

    print()
    print("=" * 78)
    print("L2：时间(到分)+金额+对方一致，但单号不同 → suspect_exact")
    print("=" * 78)
    led = fresh_ledger()
    led.add(txn(ts=BASE, amount="28.50", counterparty="某某餐饮有限公司", txn_id="T001"))
    v = led.add(txn(ts=BASE, amount="28.50", counterparty="某某餐饮有限公司", txn_id="T999"))
    check("同分同金额同对方、异单号", v.verdict, "suspect_exact")

    print()
    print("=" * 78)
    print("L3：金额相同 + 时间相近 + 对方相似 → suspect_near")
    print("（这一层以前一跑就 IndexError，是本次要钉死的重点）")
    print("=" * 78)

    # 3.1 命中：3 分钟后、同金额、同对方、异单号
    led = fresh_ledger()
    led.add(txn(ts=BASE, amount="50.00", counterparty="某某便利店", txn_id="T001"))
    v = led.add(txn(ts=BASE + dt.timedelta(minutes=3), amount="50.00",
                    counterparty="某某便利店", txn_id="T002"))
    check("3 分钟后 · 同金额 · 同对方", v.verdict, "suspect_near")

    # 3.2 命中：跨渠道（银行流水 vs 支付宝）、对方名称略有差异
    led = fresh_ledger()
    led.add(txn(ts=BASE, amount="88.80", counterparty="某某连锁超市有限公司",
                txn_id="A001", source="alipay"))
    v = led.add(txn(ts=BASE + dt.timedelta(seconds=95), amount="88.80",
                    counterparty="某某连锁超市有限公司（快捷支付）",
                    txn_id="B001", source="bank_pdf"))
    check("跨渠道 · 95 秒后 · 同金额 · 对方相似", v.verdict, "suspect_near")

    # 3.3 不命中：超出 5 分钟时间窗
    led = fresh_ledger()
    led.add(txn(ts=BASE, amount="50.00", counterparty="某某便利店", txn_id="T001"))
    v = led.add(txn(ts=BASE + dt.timedelta(minutes=30), amount="50.00",
                    counterparty="某某便利店", txn_id="T002"))
    check("30 分钟后（超窗）→ 不该判重", v.verdict, "new")

    # 3.4 不命中：对方名字完全不同
    led = fresh_ledger()
    led.add(txn(ts=BASE, amount="50.00", counterparty="某某便利店", txn_id="T001"))
    v = led.add(txn(ts=BASE + dt.timedelta(minutes=3), amount="50.00",
                    counterparty="完全不相干的另一家公司", txn_id="T002"))
    check("3 分钟后 · 同金额 · 对方完全不同 → 不该判重", v.verdict, "new")

    # 3.5 不命中：金额不同
    led = fresh_ledger()
    led.add(txn(ts=BASE, amount="50.00", counterparty="某某便利店", txn_id="T001"))
    v = led.add(txn(ts=BASE + dt.timedelta(minutes=3), amount="51.00",
                    counterparty="某某便利店", txn_id="T002"))
    check("3 分钟后 · 金额不同 → 不该判重", v.verdict, "new")

    print()
    print("=" * 78)
    print("真实场景复现：银行 PDF 里有一笔金额和已有支付宝流水相同")
    print("=" * 78)
    led = fresh_ledger()
    for i, amount in enumerate(["6.50", "115.00", "3200.00"], start=1):
        led.add(txn(ts=BASE + dt.timedelta(minutes=i), amount=amount,
                    counterparty=f"已有商户{i}", txn_id=f"OLD{i}"))
    try:
        v = led.add(txn(ts=BASE + dt.timedelta(minutes=4), amount="115.00",
                        counterparty="已有商户2", txn_id="", source="bank_pdf"))
        check("银行行与已有支付宝行同金额同时在窗内", v.verdict, "suspect_near")
    except Exception as exc:
        FAILURES.append(f"L3 崩了：{type(exc).__name__}: {exc}")
        print(f"  [✗] L3 抛异常：{type(exc).__name__}: {exc}")

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
