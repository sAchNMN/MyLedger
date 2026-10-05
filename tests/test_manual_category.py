"""手工指定分类 + 未分类盘点的回归测试。

为什么要有这个文件：
    「手工指定」和「按规则重算」是两套优先级不同的判定，最容易出的错是
    **优先级搞反** —— 用户手工纠正完分类，跑一次 recompute 就被规则改回去，
    表面看功能是好的（命令跑通了、输出也正常），实际等于没做。
    这种错单靠手点一遍测不出来，必须用构造好的数据钉住。

    另外一条纪律来自本项目真实事故：**落盘类操作，验证必须重新读磁盘**。
    同连接查询看到的是内存状态，曾经因此把「没写进文件」报成了成功。
    所以本文件里凡是涉及写库的用例，都重新打开一遍再核对。

用法：<venv>\\Scripts\\python.exe tests/test_manual_category.py
返回码 0 表示全部通过。
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tempfile
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from importer import fixups, secure_store  # noqa: E402
from importer.categorize import (  # noqa: E402
    DECIDED_BY_MANUAL,
    DECIDED_BY_RULE,
    _remember,
    categorize,
    clear_manual,
    manual_map,
    set_manual,
)
from importer.model import Txn  # noqa: E402
from importer.store import Ledger  # noqa: E402

FAILURES: list[str] = []
BASE = dt.datetime(2026, 9, 3, 12, 0, 0)
_Counter = {"n": 0}


def fresh_ledger() -> Ledger:
    led = Ledger(sqlite3.connect(":memory:"), ":memory:")
    led.start_batch("test", "test-batch", parsed=0)
    return led


def quiet(*_a, **_k) -> None:
    """吞掉输出，测试时只关心返回值。"""


def ingest(led: Ledger, *, counterparty: str, item: str = "", amount: str = "10.00",
           direction: str = "expense", source: str = "alipay",
           raw: dict | None = None, ts: dt.datetime | None = None) -> Txn:
    """按和真实流水线一样的顺序入库：先分类，再写库。"""
    _Counter["n"] += 1
    txn = Txn(
        source=source, ts=ts or BASE, amount=Decimal(amount), direction=direction,
        counterparty=counterparty, item=item, txn_id=f"T{_Counter['n']:04d}",
        raw=raw if raw is not None else {},
    )
    categorize([txn], led)
    led.add(txn)
    return txn


def check(name: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: 期望 {want}，实际 {got}")
    print(f"  [{'✓' if ok else '✗'}] {name:<56} -> {got}")


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# 一个能被规则命中的商户（RULES 里有「餐饮」），用来验证「人工压过规则」
RULE_MERCHANT = "某某餐饮管理有限公司"
# 规则认不出来的商户（关键词表里没有任何词命中），用来验证未分类盘点
UNMATCHED_A = "某某网络科技有限公司"
UNMATCHED_B = "某某信息咨询有限公司"
BASE_RAW = {"原始": ["", "", "", "", "", "支出"]}


def case_basic() -> None:
    section("1. set_manual / clear_manual 的基本行为")
    led = fresh_ledger()
    key = set_manual(led, " 某某餐饮管理有限公司 ", "休闲娱乐")
    check("商户名被归一化", key, RULE_MERCHANT)
    check("写进去的 decided_by 是 manual",
          led.conn.execute("SELECT decided_by FROM merchant_category WHERE merchant=?",
                           (RULE_MERCHANT,)).fetchone()["decided_by"], DECIDED_BY_MANUAL)
    check("manual_map 读得出来", manual_map(led), {RULE_MERCHANT: "休闲娱乐"})

    try:
        set_manual(led, "某某", "不存在的分类")
        check("非法分类应当报错", "没报错", "ValueError")
    except ValueError:
        check("非法分类被拒绝", "ValueError", "ValueError")

    try:
        set_manual(led, "   ", "餐饮")
        check("空商户名应当报错", "没报错", "ValueError")
    except ValueError:
        check("空商户名被拒绝", "ValueError", "ValueError")

    check("撤销返回 True（确实删了一行）", clear_manual(led, RULE_MERCHANT), True)
    check("撤销后 manual_map 为空", manual_map(led), {})
    check("再撤销一次返回 False（没东西可删）", clear_manual(led, RULE_MERCHANT), False)


def case_manual_beats_rule_on_import() -> None:
    section("2. 新导入的交易：人工指定压过规则")
    led = fresh_ledger()
    txn = ingest(led, counterparty=RULE_MERCHANT, item="午餐")
    check("先确认规则本来会判成餐饮", txn.category, "餐饮")

    set_manual(led, RULE_MERCHANT, "休闲娱乐")
    again = ingest(led, counterparty=RULE_MERCHANT, item="午餐")
    check("指定之后，新导入的同一商户用人工分类", again.category, "休闲娱乐")


def case_remember_guard() -> None:
    section("3. 守卫：规则/AI 不许覆盖人工指定")
    led = fresh_ledger()
    set_manual(led, RULE_MERCHANT, "休闲娱乐")
    # 直接调内部写缓存函数，模拟「规则命中后又去写缓存」这条路径
    _remember(led, RULE_MERCHANT, "餐饮", DECIDED_BY_RULE)
    row = led.conn.execute(
        "SELECT category, decided_by FROM merchant_category WHERE merchant = ?",
        (RULE_MERCHANT,)).fetchone()
    check("人工分类没被规则改掉", row["category"], "休闲娱乐")
    check("decided_by 仍是 manual", row["decided_by"], DECIDED_BY_MANUAL)


def case_recompute_priority() -> None:
    section("4. recompute：人工指定 > 规则 > 原值")
    led = fresh_ledger()
    ingest(led, counterparty=RULE_MERCHANT, item="午餐")
    check("初始为规则判定的餐饮",
          led.conn.execute("SELECT category FROM txn WHERE counterparty=?",
                           (RULE_MERCHANT,)).fetchone()["category"], "餐饮")

    set_manual(led, RULE_MERCHANT, "休闲娱乐")
    fixups.recompute(led, apply=True, emit=quiet)
    check("recompute 之后仍是人工指定的休闲娱乐",
          led.conn.execute("SELECT category FROM txn WHERE counterparty=?",
                           (RULE_MERCHANT,)).fetchone()["category"], "休闲娱乐")

    clear_manual(led, RULE_MERCHANT)
    fixups.recompute(led, apply=True, emit=quiet)
    check("撤销之后再 recompute 退回规则的餐饮",
          led.conn.execute("SELECT category FROM txn WHERE counterparty=?",
                           (RULE_MERCHANT,)).fetchone()["category"], "餐饮")


def case_recompute_keeps_value_and_revert_resets() -> None:
    section("5. recompute 保留原值；撤销人工指定则真的退回规则")
    led = fresh_ledger()
    ingest(led, counterparty=UNMATCHED_A, item="")
    # 模拟「历史遗留 / 本地 AI 曾经给过分类」的值：规则重现不出这个结果
    led.conn.execute("UPDATE txn SET category = '订阅服务' WHERE counterparty = ?",
                     (UNMATCHED_A,))
    led.conn.commit()
    fixups.recompute(led, apply=True, emit=quiet)
    got = led.conn.execute("SELECT category FROM txn WHERE counterparty=?",
                           (UNMATCHED_A,)).fetchone()["category"]
    check("recompute：规则认不出时保留原值，不洗成未分类", got, "订阅服务")

    # 而「撤销人工指定」是个明确的动作：退回规则；规则也认不出就落到未分类
    set_manual(led, UNMATCHED_A, "日用百货")
    fixups.apply_manual(led, UNMATCHED_A, "日用百货", apply=True, emit=quiet)
    clear_manual(led, UNMATCHED_A)
    fixups.apply_manual(led, UNMATCHED_A, None, apply=True, emit=quiet)
    got = led.conn.execute("SELECT category FROM txn WHERE counterparty=?",
                           (UNMATCHED_A,)).fetchone()["category"]
    check("撤销后真的变回未分类（不是纹丝不动）", got, "未分类")


def case_apply_manual_scope() -> None:
    section("6. apply_manual 只改一个商户（影响面收窄）")
    led = fresh_ledger()
    ingest(led, counterparty=UNMATCHED_A, amount="10.00")
    ingest(led, counterparty=UNMATCHED_B, amount="20.00")

    # 先试算：不能改数据
    set_manual(led, UNMATCHED_A, "订阅服务")
    result = fixups.apply_manual(led, UNMATCHED_A, "订阅服务", apply=False, emit=quiet)
    check("试算报告 1 笔待改", result["changed"], 1)
    check("试算没有写库",
          led.conn.execute("SELECT category FROM txn WHERE counterparty=?",
                           (UNMATCHED_A,)).fetchone()["category"], "未分类")

    result = fixups.apply_manual(led, UNMATCHED_A, "订阅服务", apply=True, emit=quiet)
    check("apply 报告 1 笔已改", result["applied"], True)
    check("A 改成订阅服务",
          led.conn.execute("SELECT category FROM txn WHERE counterparty=?",
                           (UNMATCHED_A,)).fetchone()["category"], "订阅服务")
    check("B 一点没动",
          led.conn.execute("SELECT category FROM txn WHERE counterparty=?",
                           (UNMATCHED_B,)).fetchone()["category"], "未分类")

    result = fixups.apply_manual(led, UNMATCHED_A, "订阅服务", apply=True, emit=quiet)
    check("再跑一次是幂等的（0 改动）", result["changed"], 0)


def case_direction_roundtrip() -> None:
    section("7. 非消费分类只压支出，撤销后方向能还原（双向）")
    led = fresh_ledger()
    ingest(led, counterparty=RULE_MERCHANT, item="午餐", amount="28.50", raw=BASE_RAW)
    row = led.conn.execute("SELECT category, direction FROM txn WHERE counterparty=?",
                           (RULE_MERCHANT,)).fetchone()
    check("初始：餐饮 / 支出", (row["category"], row["direction"]), ("餐饮", "expense"))

    set_manual(led, RULE_MERCHANT, "转账还款")
    fixups.apply_manual(led, RULE_MERCHANT, "转账还款", apply=True, emit=quiet)
    row = led.conn.execute("SELECT category, direction FROM txn WHERE counterparty=?",
                           (RULE_MERCHANT,)).fetchone()
    check("指定成非消费分类后，支出被压成中性",
          (row["category"], row["direction"]), ("转账还款", "neutral"))

    clear_manual(led, RULE_MERCHANT)
    fixups.apply_manual(led, RULE_MERCHANT, None, apply=True, emit=quiet)
    row = led.conn.execute("SELECT category, direction FROM txn WHERE counterparty=?",
                           (RULE_MERCHANT,)).fetchone()
    check("撤销后回到 餐饮 / 支出（方向从原始行重推，不是单向压死）",
          (row["category"], row["direction"]), ("餐饮", "expense"))


def case_unmatched_aggregation() -> None:
    section("8. 未分类盘点：按商户聚合、支出收入分开算")
    led = fresh_ledger()
    ingest(led, counterparty=UNMATCHED_A, amount="10.00")
    ingest(led, counterparty=UNMATCHED_A, amount="20.00")
    ingest(led, counterparty=UNMATCHED_A, amount="30.00")
    ingest(led, counterparty=UNMATCHED_B, amount="100.00", direction="income")
    ingest(led, counterparty=RULE_MERCHANT, item="午餐", amount="77.00")  # 已分类，不该出现

    totals = led.unmatched_totals()
    check("未分类笔数（不含已分类那笔）", totals["n"], 4)
    check("涉及商户数", totals["merchants"], 2)
    check("未分类支出合计", round(totals["expense"], 2), 60.0)
    check("未分类收入合计", round(totals["income"], 2), 100.0)

    rows = led.unmatched_merchants()
    check("商户按笔数降序", [r["merchant"] for r in rows], [UNMATCHED_A, UNMATCHED_B])
    check("A 的笔数与支出", (rows[0]["n"], round(rows[0]["expense"], 2)), (3, 60.0))
    check("B 的笔数与收入", (rows[1]["n"], round(rows[1]["income"], 2)), (1, 100.0))

    rows = led.unmatched_merchants(month="2026-09")
    check("按月过滤仍然命中", len(rows), 2)
    rows = led.unmatched_merchants(month="2026-08")
    check("查一个没有数据的月份返回空", rows, [])


def case_apply_manual_skips_confirmed_dup() -> None:
    section("9. apply_manual 不碰已确认的重复笔")
    led = fresh_ledger()
    ingest(led, counterparty=UNMATCHED_A, amount="10.00")
    dup = ingest(led, counterparty=UNMATCHED_A, amount="10.00")
    # Txn 对象上没有 id（那是入库时数据库给的），按 txn_id 反查。
    dup_id = led.conn.execute(
        "SELECT id FROM txn WHERE txn_id = ? ORDER BY id DESC LIMIT 1",
        (dup.txn_id,)).fetchone()["id"]
    led.conn.execute("UPDATE txn SET review='confirmed_dup' WHERE id=?", (dup_id,))
    led.conn.commit()

    result = fixups.apply_manual(led, UNMATCHED_A, "订阅服务", apply=True, emit=quiet)
    check("只改了非重复那 1 笔", result["changed"], 1)

    cats = [r["category"] for r in led.conn.execute(
        "SELECT category FROM txn WHERE counterparty=? ORDER BY id", (UNMATCHED_A,))]
    check("非重复那笔改了", cats[0], "订阅服务")
    check("重复那笔原样不动", cats[1], "未分类")

    # 再跑一次：0 改动。如果没过滤重复，这里会永远是 1，看着像「改不动」。
    again = fixups.apply_manual(led, UNMATCHED_A, "订阅服务", apply=True, emit=quiet)
    check("重跑幂等（0 改动）", again["changed"], 0)


def case_encrypted_roundtrip() -> None:
    section("10. 落盘后重新读磁盘核对（加密存储的真实路径）")
    with tempfile.TemporaryDirectory(prefix="mlk-manual-test-") as vault:
        with secure_store.open_encrypted(vault) as led:
            ingest(led, counterparty=UNMATCHED_A, amount="12.30")
            set_manual(led, UNMATCHED_A, "日用百货")
            fixups.apply_manual(led, UNMATCHED_A, "日用百货", apply=True, emit=quiet)

        # 关键：另开一个进程内的新连接，把密文解出来重新看
        with secure_store.open_encrypted(vault, save_on_exit=False) as led:
            row = led.conn.execute(
                "SELECT category FROM txn WHERE counterparty = ?", (UNMATCHED_A,)).fetchone()
            check("分类改动确实落进了密文", row["category"], "日用百货")
            check("人工指定也落进了密文", manual_map(led), {UNMATCHED_A: "日用百货"})
            check("盘点里不再有它", led.unmatched_totals()["n"], 0)


def main() -> int:
    case_basic()
    case_manual_beats_rule_on_import()
    case_remember_guard()
    case_recompute_priority()
    case_recompute_keeps_value_and_revert_resets()
    case_apply_manual_scope()
    case_direction_roundtrip()
    case_unmatched_aggregation()
    case_apply_manual_skips_confirmed_dup()
    case_encrypted_roundtrip()

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
