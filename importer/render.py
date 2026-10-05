"""终端输出排版与报表渲染。

单独抽出来是因为 cli 和 monthly 都要出报表，
而 monthly 被 cli 调用——如果报表逻辑留在 cli 里就成了循环依赖。
"""

from __future__ import annotations

import unicodedata
from decimal import Decimal

from .model import DIRECTION_EXPENSE, DIRECTION_INCOME, DIRECTION_NEUTRAL


def width(text) -> int:
    """按终端显示宽度算长度：中日韩全角字符占 2 列。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(text))


def pad(text, target: int, align: str = "left") -> str:
    text = str(text)
    space = max(0, target - width(text))
    if align == "right":
        return " " * space + text
    return text + " " * space


def table(headers: list, rows: list, aligns: list | None = None) -> str:
    aligns = aligns or ["left"] * len(headers)
    widths = [width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], width(cell))
    lines = ["  ".join(pad(h, widths[i]) for i, h in enumerate(headers))]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append("  ".join(pad(c, widths[i], aligns[i]) for i, c in enumerate(row)))
    return "\n".join(lines)


def money(value) -> str:
    return f"{Decimal(str(value or 0)):,.2f}"


def mask_merchant(name) -> str:
    """把商户名遮蔽成「首字 + ···」，用于把输出安全外发。

    保留首字是为了还能看出类别（「某」「腾」「支」之间是有信息量的），
    其余字符一律打码、长度保留 —— 形状可以出门，值不行。
    这和 `scripts/bank-pdf-probe.py` 是同一个原则：数字→9、中文→字、标点保留。

    但**只有一个字的商户名整个打掉**：保留首字的前提是「首字只是类别线索、不含全部信息」，
    一个字的名字本身就是全部的值，原样返回等于没打码。
    （真实账单里出现过单字母商户名，遮蔽后仍然原样出现在输出里 —— 就是这么发现的。）
    """
    text = str(name or "")
    if not text:
        return "（空）"
    if len(text) == 1:
        return "·"                        # 只有一个字 = 整个就是值，不能留
    return text[0] + "·" * (len(text) - 1)


def print_overview(ledger, db_path: str = "") -> None:
    overall = ledger.overall()
    txn = overall["txn"]
    print("=" * 72)
    print(f"账本概览    {db_path}" if db_path else "账本概览")
    print(f"  交易总笔数 : {txn['n'] or 0}")
    print(f"  时间范围   : {txn['first_ts'] or '-'} ~ {txn['last_ts'] or '-'}")
    print(f"  导入批次   : {overall['batches']} 次")
    print(f"  支出总额   : {money(txn['expense'])} 元")
    if txn["suspect"]:
        print(f"  待确认重复 : {txn['suspect']} 笔（用 review 处理）")
    if txn["confirmed_dup"]:
        print(f"  已确认重复 : {txn['confirmed_dup']} 笔（已从统计中剔除）")


def print_month(ledger, month: str) -> None:
    summary = ledger.month_summary(month)
    print()
    if not summary["rows"]:
        print(f"{month} 没有数据。")
        return

    expense = [
        (row["category"], Decimal(str(row["total"] or 0)), row["n"])
        for row in summary["rows"] if row["direction"] == DIRECTION_EXPENSE
    ]
    income = [
        Decimal(str(row["total"] or 0))
        for row in summary["rows"] if row["direction"] == DIRECTION_INCOME
    ]
    neutral = [
        Decimal(str(row["total"] or 0))
        for row in summary["rows"] if row["direction"] == DIRECTION_NEUTRAL
    ]

    print(f"{month} 支出构成（已剔除确认重复）")
    if expense:
        expense.sort(key=lambda item: item[1], reverse=True)
        total = sum(item[1] for item in expense)
        rows = [[cat, money(amt), n] for cat, amt, n in expense]
        rows.append(["合计", money(total), sum(n for _, _, n in expense)])
        print(table(["分类", "金额(元)", "笔数"], rows, ["left", "right", "right"]))
    else:
        print("  （本月没有支出）")

    if income:
        print(f"\n  收入合计 {money(sum(income))} 元")
    if neutral:
        # 「还款」那一项是从支付宝/微信借来的口径；银行流水的「中性」还包括
        # 代持转账（钱过手，自己既没赚也没花）。用户明确要求那种进账算收入、
        # 出账不计入支出 —— 出账就落在这里。写清楚有哪些来源，
        # 别让人以为「中性」全是同一类东西。
        print(f"  中性交易（转账 / 充值 / 提现 / 账户互转等）合计 "
              f"{money(sum(neutral))} 元，不计入消费")
