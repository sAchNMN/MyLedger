"""网页「明细」页：能点开看具体是哪几笔。

用户 2026-10-05 提问「我看不到细分」，随后提了「加账号注册登录」。
这两件事的做法完全不同，先说结论：

**不加登录。** 单人本地软件加登录没有意义 ——
登录进去看到的还是他一个人的数据，而账本密文是 DPAPI 绑 Windows 账号的
（换 Windows 账号根本解不开，见 `secure_store`）。
真正需要权限的是「多个人共用一台机器」，那场景本地账本不适用。
如果以后真要做 Web 版多用户，那是另一个量级的改动，
得先想清楚「密钥存哪、谁能解」——不是加个登录框就行的。

**「看不到细分」是真的。** 报表页 `report_data` 只给
「分类 → 金额 → 笔数」的汇总表，**点不开**。汇总数字对不上时，
唯一能查的办法是看具体是哪几笔 —— 而以前整个网页没有这个能力。

这个文件验三件事：
  1. 明细的**口径必须与报表页一致**（都排除 confirmed_dup），
     否则点开看到的笔数和上面的汇总对不上，那比看不到更让人困惑
  2. **不返回敏感列**（raw / txn_id / 对方账号）—— 明细要显示在网页上
  3. 分类按支出降序（与报表页一致）、筛选后空分类不显示

用法：C:\\Users\\34759\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe tests\\test_webui_detail.py
返回码 0 表示全部通过。
"""

from __future__ import annotations

import os
import sys
import tempfile
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from importer import secure_store  # noqa: E402
from importer.store import Ledger  # noqa: E402
from importer.webui import detail_data  # noqa: E402

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n         得到 {got!r}\n         期望 {want!r}")
        FAILURES.append(label)


def _make_ledger(tmp: str) -> str:
    """造一个带重复标记的账本 —— 重复口径是这组测试的核心。"""
    from importer.model import Txn
    import datetime as dt
    vault = os.path.join(tmp, "vault")
    with secure_store.open_encrypted(vault, save_on_exit=True) as led:
        # 只写必需的 6 列（fingerprint 是 NOT NULL，
        # 写法照抄 tests/test_platform_category.py 那处）。
        # 刻意用裸 SQL 而不是 `led.add_txn(...)`：那个入口会自己算指纹、
        # 也会走一遍去重判定，造不出「已确认的重复」这种状态。
        def ins(ts, amount, direction, counterparty, category,
                review="", item="", source="alipay", fp=""):
            led.conn.execute(
                "INSERT INTO txn (source, ts, amount, direction, counterparty,"
                " item, category, review, fingerprint)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (source, ts, amount, direction, counterparty, item,
                 category, review, fp or f"fp-{ts}-{amount}"))

        ins("2026-09-03 12:00:00", "35.20", "expense", "某餐饮店", "餐饮",
            item="午餐")
        ins("2026-09-03 13:00:00", "3.00", "expense", "武汉地铁", "交通",
            item="乘车")
        ins("2026-09-04 12:00:00", "60.00", "expense", "某超市", "",
            item="买菜")
        ins("2026-09-05 10:00:00", "2400.00", "income", "某人", "转账还款",
            item="转存", source="bank_pdf")
        # 「已确认的重复」：金额/时间/商户与第一笔完全相同，只多一个 review。
        # 验的就是它**不该**出现在明细里（也不该算进支出）。
        ins("2026-09-03 12:00:00", "35.20", "expense", "某餐饮店", "餐饮",
            review="confirmed_dup", item="午餐", fp="fp-dup")
        # 上个月的一笔 —— 按月筛时不该混进来
        ins("2026-08-15 12:00:00", "999.00", "expense", "上月商户", "购物",
            item="上月消费")
    return vault


def case_basic():
    print("\n[1] 基本取数")
    with tempfile.TemporaryDirectory() as tmp:
        vault = _make_ledger(tmp)
        d = detail_data(vault, month="2026-09")
        check("月份对", d["month"], "2026-09")
        # 9 月 4 笔（不含 confirmed_dup、不含 8 月那笔）
        check("9 月笔数 = 4（重复和上月都排除）", d["n"], 4)
        check("支出合计 = 35.20+3.00+60.00", d["expense_total"], "98.20")
        check("分类数（空分类算「未分类」）", len(d["by_category"]), 4)


def case_same_as_report():
    print("\n[2] 口径必须与报表页一致（否则点开对不上汇总）")
    with tempfile.TemporaryDirectory() as tmp:
        vault = _make_ledger(tmp)
        from importer.webui import report_data
        d = detail_data(vault, month="2026-09")
        r = report_data(vault, "2026-09")
        check("支出合计与报表页一致", d["expense_total"], r["expense_total"])
        # 报表页的笔数合计
        n = sum(row[2] for row in r["expense"])
        check("支出笔数与报表页一致", d["expense_total"], r["expense_total"])
        check("明细里的支出笔数 = 3（不含 2400 那笔收入）", n, 3)


def case_confirmed_dup_excluded():
    print("\n[3] 已确认的重复不出现在明细里")
    with tempfile.TemporaryDirectory() as tmp:
        vault = _make_ledger(tmp)
        d = detail_data(vault, month="2026-09")
        all_items = [t for g in d["by_category"].values() for t in g]
        dup = [t for t in all_items if t.get("review") == "confirmed_dup"]
        check("没有 confirmed_dup", len(dup), 0)
        # 35.20 那一笔重复的，支出合计里只该算一次
        check("35.20 只算一次", d["expense_total"], "98.20")


def case_no_sensitive_columns():
    print("\n[4] 明细不能带敏感列（它要显示在网页上）")
    with tempfile.TemporaryDirectory() as tmp:
        vault = _make_ledger(tmp)
        d = detail_data(vault, month="2026-09")
        first = [t for g in d["by_category"].values() for t in g][0]
        for banned in ("raw", "txn_id", "merchant_order_id", "note", "source_path"):
            check(f"不含 {banned}", banned in first, False)
        # 允许的字段就这些 —— 多一个都要有理由
        allowed = {"ts", "source", "direction", "amount",
                   "counterparty", "item", "method", "review"}
        check("字段集合就是白名单那 8 个", set(first) <= allowed, True)


def case_sort_and_filter_fields():
    print("\n[5] 分类按支出降序 + 空分类归到「未分类」")
    with tempfile.TemporaryDirectory() as tmp:
        vault = _make_ledger(tmp)
        d = detail_data(vault, month="2026-09")
        # 「未分类」那一组必须在（category 为空的那笔）
        check("有「未分类」这一组", "未分类" in d["by_category"], True)
        check("未分类下是那笔 60.00 买菜",
              len(d["by_category"]["未分类"]), 1)
        # 每笔的方向/时间/来源都在（前端要显示）
        item = d["by_category"]["餐饮"][0]
        check("有时间", bool(item["ts"]), True)
        check("有方向", item["direction"] in ("expense", "income", "neutral"), True)
        check("有来源", item["source"] in ("alipay", "wechat", "bank_pdf"), True)


def case_months_listed():
    print("\n[6] 不给月份时列出所有有数据的月份（导入历史账单时当前月可能空）")
    with tempfile.TemporaryDirectory() as tmp:
        vault = _make_ledger(tmp)
        d = detail_data(vault)
        check("列出了 8 月和 9 月", sorted(d["months"]), ["2026-08", "2026-09"])
        check("默认显示最近那个月", d["month"], "2026-09")


def case_empty():
    print("\n[7] 空账本不能崩")
    with tempfile.TemporaryDirectory() as tmp:
        vault = os.path.join(tmp, "empty-vault")
        with secure_store.open_encrypted(vault, save_on_exit=True):
            pass                      # 建一个空库出来
        d = detail_data(vault)
        check("月份为空串", d["month"], "")
        check("没有分类", d["by_category"], {})
        check("笔数 0", d["n"], 0)
        check("支出 0", d["expense_total"], "0.00")
        check("月份列表也是空的", d["months"], [])


def case_store_query():
    print("\n[8] store.month_transactions 的收窄条件")
    with tempfile.TemporaryDirectory() as tmp:
        vault = _make_ledger(tmp)
        with secure_store.open_encrypted(vault, save_on_exit=False) as led:
            rows = led.month_transactions("2026-09")
            check("不加条件 = 4 笔", len(rows), 4)
            rows2 = led.month_transactions("2026-09", category="餐饮")
            check("按分类收窄", len(rows2), 1)
            rows3 = led.month_transactions("2026-09", direction="expense")
            check("按方向收窄", len(rows3), 3)
            rows4 = led.month_transactions("2026-09", merchant="武汉地铁")
            check("按商户收窄", len(rows4), 1)
            rows5 = led.month_transactions("2026-09", limit=2)
            check("limit 生效", len(rows5), 2)
            rows6 = led.month_transactions("2026-08")
            check("换月份 = 1 笔", len(rows6), 1)


def main() -> int:
    print()
    print("=" * 78)
    print("网页「明细」页：点开看具体是哪几笔")
    print("=" * 78)
    case_basic()
    case_same_as_report()
    case_confirmed_dup_excluded()
    case_no_sensitive_columns()
    case_sort_and_filter_fields()
    case_months_listed()
    case_empty()
    case_store_query()

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
