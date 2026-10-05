"""历史数据的一次性修正。

为什么需要这个：
    导入规则会随着发现的问题而变。已经入库的数据不会自动跟着变，
    但用户看到的报表是按库里的数据算的 —— 规则改了、旧数据没改，
    报表就还是错的。

    **默认只试算，不动数据。** 必须显式 --apply 才写库，
    并且在写库前自动备份。这是「先只读盘点、列候选、再确认」落到代码里。

修正是幂等的：跑几次结果都一样，可以放心重复跑。

碰哪些列：
    recompute  只碰 direction 和 category，不碰金额、时间、单号、对方、raw
    strip_raw  只碰 raw（把老格式的整行原文压成白名单形态），不碰别的
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
from .model import (
    DIRECTION_EXPENSE,
    DIRECTION_NEUTRAL,
    STATUS_KILLS_EXPENSE,
    normalize_text,
    parse_direction,
    status_verdict,
)
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

# raw 里允许留下的键（与 sources._row_summary 写入的一致）。
# strip_raw 用这张表来判断一条老 raw 需不需要整理。
_RAW_KEEP = ("来源", "编码", "表头行", "数据行", "方向列", "状态列",
             "字段数", "非空字段数", "金额列有值", "时间列有值")


def _derive_direction(row) -> str:
    """从原始行重新推导方向。

    **为什么必须能重推**：只能单向「压成中性」的修正是不可逆的 ——
    规则一旦改回来（比如发现生活费应该算收入），被压掉的数据就回不去了，
    只能靠备份回滚。太脆。

    原始行里存着平台自己的「收/支」列，用它重算即可。
    没有方向列的来源（银行 PDF / 截图，方向是按金额正负号定的）保持原样。

    status 列**不走 raw**，直接读 txn 表里那一列：
    状态是固定词表（交易成功 / 已全额退款 / 交易关闭…），和方向列同一性质，
    但它本来就是一个正式字段，存了就没必要再抄一份到 raw 里。
    读正式列还有个好处：用户手工在界面上改过状态也能被 recompute 认到。

    raw 存在三种形态，每一种都要处理对：

        1. 新格式：显式记着「方向列」的值。
           空值也是答案 —— 微信的中性交易那一格就是空的。
        2. 老格式：raw["原始"] 是整行，方向列按下标取；列数变了就退回按值搜。
        3. 更早的：raw 是 {}（schema 的默认值就是 '{}'）。

    第 3 种是修 ML-010 时顺手发现的坑：原来的代码在「查不到方向值」时
    继续往下走，而 parse_direction("") 返回的是**中性** ——
    跑一次 recompute --apply，一批支出就会从报表里消失，全程没有任何报错。
    查不到就别猜：不改，永远比改错安全。
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

    cells = raw.get("原始", [])
    idx = _DIRECTION_INDEX_HINT.get(row["source"])

    # 2) 老数据：按来源的已知列下标取。可能为空 —— 微信的中性交易就是空值，
    #    空值在这里是答案，不是「查不到」。
    if idx is not None and idx < len(cells):
        return parse_direction(cells[idx], row["counterparty"], row["item"])

    # 3) 下标不可用（格式改了、列数变了）→ 退回按值搜。
    #    按值搜会把备注里恰好写着「收入」的格子当成方向列，所以只在
    #    拿不到下标时才用它，而且要求整格相等（实测过这个坑，见文件头注释）。
    for cell in cells:
        if normalize_text(cell) in _DIRECTION_LABELS:
            return parse_direction(cell, row["counterparty"], row["item"])

    # 4) 什么线索都没有：保持原样。
    return row["direction"]


def _base_direction(row) -> str:
    """重推「未经任何压中性处理」的基础方向。

    和 _derive_direction 的区别只有一处，但这一处是整套双向性的命门：
    _derive_direction 查不到方向线索时返回 row["direction"]，
    而那一列可能**已经被压成中性**了（比如上一轮 recompute 的结果）。
    拿它当基础方向去跑状态判定，就等于在一个「已经是中性」的值上
    判断「要不要压成中性」—— 状态改回正常时永远压不回来。

    所以这里查不到就返回 expense（视为最原始的形态），
    让状态判定总能从「支出」这个起点重新出发。
    代价是：如果一笔收入本来就查不到方向线索，会被误当成支出 ——
    但那一笔随后要么被状态压成中性、要么按非消费分类压成中性，
    最坏情况是它留在支出里，而它本来就在那儿。不是新增的风险。
    """
    if row["source"] in _SIGN_DERIVED_SOURCES:
        return row["direction"]
    try:
        raw = json.loads(row["raw"])
    except Exception:
        return DIRECTION_EXPENSE
    if "方向列" in raw:
        return parse_direction(raw.get("方向列") or "", row["counterparty"], row["item"])
    cells = raw.get("原始", [])
    idx = _DIRECTION_INDEX_HINT.get(row["source"])
    if idx is not None and idx < len(cells):
        return parse_direction(cells[idx], row["counterparty"], row["item"])
    for cell in cells:
        if normalize_text(cell) in _DIRECTION_LABELS:
            return parse_direction(cell, row["counterparty"], row["item"])
    return DIRECTION_EXPENSE


def _apply_status_to(direction: str, status: str) -> str:
    """按状态列决定要不要把支出压成中性。这是**纯函数**，可逆。

    保持纯函数（而不是像以前那样在导入时就把方向改掉）是刻意的：
    这样状态规则改了以后，跑一次 recompute 就能双向还原 ——
    「已过期」这个判断明天被证明是错的，方向能自己回去。

    认不出来的状态一律不动。

    ⚠ 调用方必须把「未经状态处理的基础方向」传进来，不能传已经压过的值。
    原因：一旦某笔被压成中性，再压第二次是空操作，而当状态改回正常时
    就再也压不回来了 —— 看起来像幂等，实际是单向压死。
    """
    if direction == DIRECTION_EXPENSE and status_verdict(status) in STATUS_KILLS_EXPENSE:
        return DIRECTION_NEUTRAL
    return direction


def recompute(ledger: Ledger, apply: bool = False,
              emit: Callable[[str], None] = print) -> dict:
    """按当前规则重算分类与方向。默认只试算。

    幂等且**双向**：方向从原始行重推，所以规则改回来时也能还原。

    分类优先级：**人工指定 > 平台分类 > 规则 > 原值**（ML-011 起）。
    人工指定必须排第一 —— 否则用户刚手工纠正的分类，跑一次 recompute
    就被规则改回去，等于这个功能没做。平台分类排第二是因为它是平台
    **已经判好**的结果，比我们靠关键词猜的准。原值垫底是为了
    「规则认不出来的交易不要被洗成未分类」。

    方向判定是双向的：先从「收/支」列重推基础方向，再按状态列决定
    要不要压成中性。两个环节都是纯函数，规则改回来时都能还原。
    """
    manual = manual_map(ledger)

    rows = ledger.conn.execute(
        "SELECT id, source, direction, category, counterparty, item, amount,"
        " status, platform_category, raw FROM txn"
    ).fetchall()

    changes = []
    for r in rows:
        merchant = normalize_text(r["counterparty"])
        new_cat = category_for(merchant, r["counterparty"], r["item"], manual,
                               r["source"], r["platform_category"] or "") or r["category"]
        # 方向：先从「收/支」列重推，再按状态列决定要不要压中性。
        # 顺序不能反 —— 状态判定是作用在「确认为支出」的记录上的，
        # 如果先压了中性，状态那一步就没有支出了。
        new_dir = _apply_status_to(_base_direction(r), r["status"] or "")
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
            "SELECT id, source, direction, category, counterparty, item, amount,"
            " status, platform_category, raw FROM txn"
        ).fetchall()
        if normalize_text(r["counterparty"]) == key
    ]

    changes = []
    for r in rows:
        if target is None:
            new_cat = (classify_by_rules(r["counterparty"], r["item"])
                       or DEFAULT_CATEGORY)
        else:
            new_cat = target
        new_dir = _apply_status_to(_base_direction(r), r["status"] or "")
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


# --------------------------------------------------------------------------
# 历史 raw 的清理（ML-010）
#
# 改了 sources.py 只代表**新导入**的行不再存整行原文。已经入库的那些
# 还是老格式 —— 账本里那份「账单完整副本」不会因为改了代码就自己消失。
# 所以需要一次性的整理。
#
# 这一步有两个必须同时满足的约束，少一个都不能做：
#
#   1. 排障能力不能丢：字段数这类形状摘要要从原始行里算出来带过去
#   2. **方向可重推的能力不能丢**：「原始」正是老格式里方向列的载体，
#      直接删掉它，recompute 的双向性（规则改回来能还原）就断了。
#      所以必须先把方向值搬进新格式的「方向列」，再丢掉整行。
#
# 而且这是改写历史数据，默认只试算。
# --------------------------------------------------------------------------

def legacy_raw_summary(raw: dict, source: str) -> tuple:
    """把一条老格式 raw 压成白名单形态，返回 (新 raw, 方向是否仍可重推)。"""
    keep: dict = {key: raw[key] for key in ("来源", "编码", "表头行", "数据行")
                  if key in raw}
    cells = raw.get("原始") or []

    reversible = False
    if "方向列" in raw:
        keep["方向列"] = raw["方向列"]
        reversible = True
    elif cells:
        # 先把方向值搬到新位置，再丢整行 —— 顺序反了就永久失去重推能力
        idx = _DIRECTION_INDEX_HINT.get(source)
        if idx is not None and idx < len(cells):
            keep["方向列"] = cells[idx]
            reversible = True
        else:
            for cell in cells:
                if normalize_text(cell) in _DIRECTION_LABELS:
                    keep["方向列"] = cell
                    reversible = True
                    break

    if cells:
        keep["字段数"] = len(cells)
        keep["非空字段数"] = sum(1 for cell in cells if cell)
    elif "字段数" in raw:
        keep["字段数"] = raw["字段数"]
        keep["非空字段数"] = raw.get("非空字段数", 0)
    return keep, reversible


def strip_raw(ledger: Ledger, apply: bool = False,
              emit: Callable[[str], None] = print) -> dict:
    """把历史数据里带整行原文的 raw 压成白名单形态。默认只试算。

    幂等：整理过的行不会再被认作「需要整理」，跑几次结果都一样。
    """
    rows = ledger.conn.execute("SELECT id, source, raw FROM txn").fetchall()

    changes: list[dict] = []
    lost = 0
    for row in rows:
        try:
            raw = json.loads(row["raw"])
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        if not any(key not in _RAW_KEEP for key in raw):
            continue                       # 已经是白名单形态
        keep, reversible = legacy_raw_summary(raw, row["source"])
        if not reversible:
            lost += 1
        changes.append({
            "id": row["id"],
            "raw": json.dumps(keep, ensure_ascii=False),
        })

    emit(f"待整理的 raw：{len(changes)} 笔（全库 {len(rows)} 笔）")
    if not changes:
        emit("没有需要整理的记录。")
        return {"changed": 0, "applied": False, "lost_direction": 0}

    emit("  整理后只保留：来源 / 编码 / 行号 / 方向列 / 形状摘要")
    if lost:
        emit(f"  ！其中 {lost} 笔查不到方向值，整理后将无法再重推方向"
             f"（当前方向不受影响）")

    if not apply:
        emit("这是试算，**没有改动任何数据**。确认无误后加 --apply 执行。")
        return {"changed": len(changes), "applied": False, "lost_direction": lost}

    for item in changes:
        ledger.conn.execute("UPDATE txn SET raw = ? WHERE id = ?",
                            (item["raw"], item["id"]))
    ledger.conn.commit()
    emit(f"已整理 {len(changes)} 笔的 raw。")
    return {"changed": len(changes), "applied": True, "lost_direction": lost}
