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

import json
import shutil
import sqlite3
from collections import Counter
from datetime import datetime
from typing import Callable, Optional

from .categorize import (
    DEFAULT_CATEGORY,
    NON_CONSUMPTION_CATEGORIES,
    category_for,
    classify_by_rules,
    manual_map,
)
from .model import DIRECTION_EXPENSE, DIRECTION_NEUTRAL, normalize_text, parse_direction
from .store import Ledger

# 平台在「收/支」列里会用到的值
_DIRECTION_LABELS = frozenset({"支出", "收入", "不计收支", "中性交易"})

# 各来源「收/支」列在原始行里的下标。
#
# 为什么要写死下标而不是「按值搜整个行」：按值搜会把备注里
# 恰好写着「收入」的格子当成方向列 —— 实测因此把两笔 100 元的中性记录
# 误判成了收入，收入总额凭空多了 200 元。
# 宁可写死下标（新导入的行会显式记下方向列的值，见 sources.py），
# 也不要猜。
_DIRECTION_INDEX_HINT = {"alipay": 5, "wechat": 4}

# 这两个来源没有「收/支」列 —— 方向是按金额的正负号定的。
# 对它们重推方向只会把数据搞坏，直接保持原样。
_SIGN_DERIVED_SOURCES = frozenset({"bank_ocr", "bank_pdf"})


def _derive_direction(row) -> str:
    """从原始行重新推导方向。

    **为什么必须能重推**：只能单向「压成中性」的修正是不可逆的 ——
    规则一旦改回来（比如发现生活费应该算收入），被压掉的数据就回不去了，
    只能靠备份回滚。太脆。

    原始行里存着平台自己的「收/支」列，用它重算即可。
    没有方向列的来源（银行 PDF / 截图，方向是按金额正负号定的）
    保持原样，不猜 —— 猜错比不改更糟。
    """
    if row["source"] in _SIGN_DERIVED_SOURCES:
        return row["direction"]

    try:
        raw = json.loads(row["raw"])
    except Exception:
        return row["direction"]

    # 1) 新导入的行会显式记下方向列的值（sources.py 写入）
    if "方向列" in raw:
        cell = raw.get("方向列") or ""
        return parse_direction(cell, row["counterparty"], row["item"])

    # 2) 老数据：按来源的已知列下标取。
    #    列超出范围（格式变了）才退回按值搜。
    cells = raw.get("原始", [])
    idx = _DIRECTION_INDEX_HINT.get(row["source"])
    if idx is not None and idx < len(cells):
        cell = cells[idx]          # 可能为空 —— 微信的中性交易就是空值
    else:
        cell = ""
        for c in cells:
            if normalize_text(c) in _DIRECTION_LABELS:
                cell = c
                break

    if not cell and idx is None:
        return row["direction"]
    return parse_direction(cell, row["counterparty"], row["item"])


def recompute(ledger: Ledger, apply: bool = False,
              emit: Callable[[str], None] = print) -> dict:
    """按当前规则重算分类与方向。默认只试算。

    幂等且**双向**：方向从原始行重推，所以规则改回来时也能还原。

    分类优先级：**人工指定 > 规则 > 原值**。
    人工指定必须排第一 —— 否则用户刚手工纠正的分类，跑一次 recompute
    就被规则改回去，等于这个功能没做。原值垫底是为了「规则认不出来的
    交易不要被洗成未分类」。
    """
    manual = manual_map(ledger)

    rows = ledger.conn.execute(
        "SELECT id, source, direction, category, counterparty, item, amount, raw FROM txn"
    ).fetchall()

    changes = []
    for r in rows:
        merchant = normalize_text(r["counterparty"])
        new_cat = category_for(merchant, r["counterparty"], r["item"], manual) or r["category"]
        new_dir = _derive_direction(r)
        # 非消费分类只压支出，收入保留（家里给的生活费要算收入）
        if new_cat in NON_CONSUMPTION_CATEGORIES and new_dir == DIRECTION_EXPENSE:
            new_dir = DIRECTION_NEUTRAL
        if new_cat != r["category"] or new_dir != r["direction"]:
            changes.append({
                "id": r["id"],
                "source": r["source"],
                "amount": r["amount"],
                "old_cat": r["category"], "new_cat": new_cat,
                "old_dir": r["direction"], "new_dir": new_dir,
            })

    to_expense = [c for c in changes if c["old_dir"] != "expense" and c["new_dir"] == "expense"]
    off_expense = [c for c in changes if c["old_dir"] == "expense" and c["new_dir"] != "expense"]
    to_income = [c for c in changes if c["old_dir"] != "income" and c["new_dir"] == "income"]
    off_income = [c for c in changes if c["old_dir"] == "income" and c["new_dir"] != "income"]
    recategorised = [c for c in changes if c["old_cat"] != c["new_cat"]]

    if manual:
        emit(f"人工指定的商户 {len(manual)} 个（优先级高于规则，不会被规则改回）")
    emit(f"待修正 {len(changes)} 笔（全库 {len(rows)} 笔）")
    for label, group in (
        ("支出 → 不计消费", off_expense),
        ("其他 → 支出", to_expense),
        ("其他 → 收入", to_income),
        ("收入 → 其他", off_income),
    ):
        if group:
            amount = sum(float(c["amount"]) for c in group)
            emit(f"  {label}：{len(group)} 笔，合计 {amount:,.2f} 元")
    emit(f"  分类发生变化的：{len(recategorised)} 笔")
    emit("")

    if recategorised:
        flows = Counter((c["old_cat"], c["new_cat"]) for c in recategorised)
        emit("  分类变更明细（原分类 → 新分类：笔数）：")
        for (old, new), n in flows.most_common(12):
            emit(f"    {old:<10} → {new:<10} {n:>3} 笔")
        emit("")

    if not changes:
        emit("没有需要修正的记录。")
        return {"changed": 0, "applied": False}

    if not apply:
        emit("这是试算，**没有改动任何数据**。确认无误后加 --apply 执行。")
        return {"changed": len(changes), "applied": False}

    for c in changes:
        ledger.conn.execute(
            "UPDATE txn SET category = ?, direction = ? WHERE id = ?",
            (c["new_cat"], c["new_dir"], c["id"]),
        )
    ledger.conn.commit()
    emit(f"已修正 {len(changes)} 笔。")
    return {"changed": len(changes), "applied": True}


def apply_manual(ledger: Ledger, merchant: str, category: Optional[str] = None,
                 apply: bool = False, emit: Callable[[str], None] = print) -> dict:
    """把「某一个商户」的人工分类应用到**已经入库**的历史交易。

    set_manual() 写的是「商户 → 分类」缓存表，新导入的交易下次自动生效；
    但躺在库里的历史交易不会自己变，靠这个函数补上。

    范围刻意收窄到只有一个商户（对比 recompute 的全库重算）：
    手工纠一个商户时不该动别的地方，影响面越窄越可预测 ——
    真出问题时也能一眼知道是哪次操作干的。

    category=None 表示「撤销人工指定」：该商户退回规则判定。
    **规则仍然认不出就落到「未分类」，而不是保留现值。**
    这里刻意和 recompute 不一样 —— 撤销是个明确的动作，用户要看到
    分类真的变回去了；如果保留原来的手工值，命令报「已撤销」但界面上
    分类纹丝不动，看起来就像撤销失败。

    默认只试算，--apply 才写库；写库前由调用方负责备份密文。
    """
    key = normalize_text(merchant)
    if not key:
        raise ValueError("商户名不能为空")

    manual = manual_map(ledger)
    target = manual.get(key) if category is None else category

    # 归一化比较只能放在 Python 里做：SQLite 没有 normalize_text，
    # 而商户名可能有全角/多余空白这类差异。账本规模（几千笔以内）下这样最稳。
    rows = [
        r for r in ledger.conn.execute(
            "SELECT id, source, direction, category, counterparty, item, amount, raw FROM txn"
        ).fetchall()
        if normalize_text(r["counterparty"]) == key
    ]

    changes = []
    for r in rows:
        if target is None:
            new_cat = classify_by_rules(r["counterparty"], r["item"]) or DEFAULT_CATEGORY
        else:
            new_cat = target
        new_dir = _derive_direction(r)
        if new_cat in NON_CONSUMPTION_CATEGORIES and new_dir == DIRECTION_EXPENSE:
            new_dir = DIRECTION_NEUTRAL
        if new_cat != r["category"] or new_dir != r["direction"]:
            changes.append({
                "id": r["id"], "amount": r["amount"],
                "old_cat": r["category"], "new_cat": new_cat,
                "old_dir": r["direction"], "new_dir": new_dir,
            })

    emit(f"商户「{key}」历史交易 {len(rows)} 笔，需要改动 {len(changes)} 笔")
    if changes:
        flows = Counter((c["old_cat"], c["new_cat"]) for c in changes)
        for (old, new), n in flows.most_common(10):
            emit(f"    {old:<10} → {new:<10} {n:>3} 笔")

    if not changes:
        return {"changed": 0, "applied": False}
    if not apply:
        emit("这是试算，**没有改动已入库的历史交易**。确认无误后加 --apply 执行。")
        return {"changed": len(changes), "applied": False}

    for c in changes:
        ledger.conn.execute(
            "UPDATE txn SET category = ?, direction = ? WHERE id = ?",
            (c["new_cat"], c["new_dir"], c["id"]),
        )
    ledger.conn.commit()
    emit(f"已修正 {len(changes)} 笔。")
    return {"changed": len(changes), "applied": True}


def backup(path: str, emit: Callable[[str], None] = print) -> str:
    """写库前先备份密文。备份失败就不动手。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = f"{path}.backup-{stamp}"
    shutil.copy2(path, target)
    emit(f"已备份到 {target}")
    return target
