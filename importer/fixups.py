"""历史数据的一次性修正。

为什么需要这个：
    导入规则会随着发现的问题而变。已经入库的数据不会自动跟着变，
    但用户看到的报表是按库里的数据算的 —— 规则改了、旧数据没改，
    报表就还是错的。

    **默认只试算，不动数据。** 必须显式 --apply 才写库，
    并且在写库前自动备份。这是「先只读盘点、列候选、再确认」落到代码里。

修正是幂等的：跑几次结果都一样，可以放心重复跑。
只碰 direction 和 category 两列，不碰金额、时间、单号、对方、原始行。
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from typing import Callable

from .categorize import NON_CONSUMPTION_CATEGORIES, classify_by_rules
from .model import DIRECTION_NEUTRAL
from .store import Ledger


def recompute(ledger: Ledger, apply: bool = False,
              emit: Callable[[str], None] = print) -> dict:
    """按当前规则重算分类与方向。默认只试算。"""
    rows = ledger.conn.execute(
        "SELECT id, source, direction, category, counterparty, item, amount FROM txn"
    ).fetchall()

    changes = []
    for r in rows:
        new_cat = classify_by_rules(r["counterparty"], r["item"]) or r["category"]
        new_dir = r["direction"]
        if new_cat in NON_CONSUMPTION_CATEGORIES:
            new_dir = DIRECTION_NEUTRAL
        if new_cat != r["category"] or new_dir != r["direction"]:
            changes.append({
                "id": r["id"],
                "source": r["source"],
                "amount": r["amount"],
                "old_cat": r["category"], "new_cat": new_cat,
                "old_dir": r["direction"], "new_dir": new_dir,
            })

    # 只统计「原本被算成支出、改完不再是支出」的量，这是用户最关心的数
    off_expense = [
        c for c in changes if c["old_dir"] == "expense" and c["new_dir"] != "expense"
    ]
    moved_amount = sum(float(c["amount"]) for c in off_expense)
    recategorised = [c for c in changes if c["old_cat"] != c["new_cat"]]

    emit(f"待修正 {len(changes)} 笔（全库 {len(rows)} 笔）")
    emit(f"  其中从「支出」改为不计消费：{len(off_expense)} 笔，"
         f"合计 {moved_amount:,.2f} 元")
    emit(f"  其中分类发生变化的：{len(recategorised)} 笔")
    emit("")

    if recategorised:
        from collections import Counter
        flows = Counter((c["old_cat"], c["new_cat"]) for c in recategorised)
        emit("  分类变更明细（原分类 → 新分类：笔数）：")
        for (old, new), n in flows.most_common(12):
            emit(f"    {old:<10} → {new:<10} {n:>3} 笔")
        emit("")

    if not changes:
        emit("没有需要修正的记录。")
        return {"changed": 0, "moved_amount": 0.0, "applied": False}

    if not apply:
        emit("这是试算，**没有改动任何数据**。确认无误后加 --apply 执行。")
        return {"changed": len(changes), "moved_amount": moved_amount, "applied": False}

    for c in changes:
        ledger.conn.execute(
            "UPDATE txn SET category = ?, direction = ? WHERE id = ?",
            (c["new_cat"], c["new_dir"], c["id"]),
        )
    ledger.conn.commit()
    emit(f"已修正 {len(changes)} 笔。")
    return {"changed": len(changes), "moved_amount": moved_amount, "applied": True}


def backup(path: str, emit: Callable[[str], None] = print) -> str:
    """写库前先备份密文。备份失败就不动手。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = f"{path}.backup-{stamp}"
    shutil.copy2(path, target)
    emit(f"已备份到 {target}")
    return target
