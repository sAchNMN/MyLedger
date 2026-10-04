"""历史 raw 清理的测试（ML-010 的收尾）。

为什么必须有这个文件：

    改了 sources.py，只代表**新导入**的行不再存整行原文。
    已经入库的那些还是老格式 —— 账本里那份「账单完整副本」不会
    因为改了代码就自己消失，等于这次修复只对新数据生效。

    清理历史 raw 有两个必须同时满足的约束，少一个都不能做：

        1. 不能弄丢排障能力：字段数这些形状摘要要从原始行里算出来带过去
        2. 不能弄丢方向可重推的能力：「原始」正是老格式里方向列的载体，
           直接删掉它，fixups 的双向性（规则改回来能还原）就断了 ——
           所以必须先把方向值搬进新格式的「方向列」，再丢掉整行

    而且这是改写历史数据，默认只能试算。这里把「试算真的不动数据」
    也钉住：拿密文的字节哈希比对。

用法：<venv>\\Scripts\\python.exe tests/test_raw_cleanup.py
返回码 0 表示全部通过。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from importer import cli, fixups, secure_store  # noqa: E402
from importer.fixups import _derive_direction  # noqa: E402
from importer.model import Txn  # noqa: E402
from importer.store import Ledger  # noqa: E402

FAILURES: list[str] = []

BASE = dt.datetime(2026, 9, 3, 12, 0, 0)

# 老格式的一整行（支付宝列序）。第 5 格是「收/支」。
LEGACY_CELLS = ["2026-09-03 12:01:00", "日用百货", "合成便利店", "acct_TEST_9911",
                "日用品", "收入", "50.00", "余额", "交易成功",
                "SYN-CL-0001", "MO-SYN-CL-0001", "extra_TEST_7733"]

MARKERS = ("acct_TEST_9911", "extra_TEST_7733")


def check(name: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: 期望 {want!r}，实际 {got!r}")
    print(f"  [{'✓' if ok else '✗'}] {name:<46} -> {got!r}")


def digest(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def make_legacy_ledger(rows: int = 2, *, with_cells: bool = True) -> Ledger:
    """造一本「老格式」的内存账本。"""
    ledger = Ledger(sqlite3.connect(":memory:", check_same_thread=False), ":memory:")
    ledger.start_batch("alipay", "合成账单.csv", parsed=rows)
    for index in range(1, rows + 1):
        ledger.add(Txn(source="alipay", ts=BASE + dt.timedelta(minutes=index),
                       amount=Decimal("50.00"), direction="expense",
                       counterparty="合成便利店", item="日用品",
                       txn_id=f"SYN-CL-{index:04d}"))
        raw: dict = {"来源": "直接读取", "编码": "utf-8-sig", "数据行": index + 4}
        if with_cells:
            raw["原始"] = list(LEGACY_CELLS)
        ledger.conn.execute("UPDATE txn SET raw = ? WHERE id = ?",
                           (json.dumps(raw, ensure_ascii=False), index))
    ledger.conn.commit()
    return ledger


def raw_of(ledger: Ledger, txn_id: int) -> dict:
    return json.loads(ledger.conn.execute(
        "SELECT raw FROM txn WHERE id = ?", (txn_id,)).fetchone()["raw"])


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="myledger-clean-")
    try:
        return run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(tmp: str) -> int:
    quiet = lambda *_: None  # noqa: E731

    print()
    print("=" * 78)
    print("试算：一行都不许改")
    print("=" * 78)
    ledger = make_legacy_ledger(2)
    before = raw_of(ledger, 1)
    result = fixups.strip_raw(ledger, apply=False, emit=quiet)
    check("报告要整理的笔数", result["changed"], 2)
    check("试算没有写库", result["applied"], False)
    check("raw 原封不动", raw_of(ledger, 1), before)

    print()
    print("=" * 78)
    print("执行：整行原文被清掉，形状摘要被留下来")
    print("=" * 78)
    result = fixups.strip_raw(ledger, apply=True, emit=quiet)
    check("改了几笔", result["changed"], 2)
    raw = raw_of(ledger, 1)
    check("没有「原始」了", "原始" in raw, False)
    check("来源还在", raw.get("来源"), "直接读取")
    check("数据行号还在", raw.get("数据行"), 5)
    check("字段数从原始行算出来带过来了", raw.get("字段数"), 12)
    check("非空字段数", raw.get("非空字段数"), 12)
    check("方向列被搬进新格式", raw.get("方向列"), "收入")

    blob = json.dumps([raw_of(ledger, i) for i in (1, 2)], ensure_ascii=False)
    check("标记串一个都不剩", [m for m in MARKERS if m in blob], [])

    print()
    print("=" * 78)
    print("最重要的：方向仍然能重推（双向修正没被削掉）")
    print("=" * 78)
    row = ledger.conn.execute("SELECT * FROM txn WHERE id = 1").fetchone()
    check("整理之后仍能推出同样的方向", _derive_direction(row), "income")

    print()
    print("=" * 78)
    print("幂等：再跑一次没什么可改的")
    print("=" * 78)
    again = fixups.strip_raw(ledger, apply=True, emit=quiet)
    check("第二次没有改动", again["changed"], 0)

    print()
    print("=" * 78)
    print("没有原始行可查的老记录：如实报告「方向不再可重推」")
    print("=" * 78)
    ledger2 = make_legacy_ledger(2, with_cells=False)
    # 给它塞一个白名单外的键，模拟更早版本留下的杂物
    ledger2.conn.execute(
        "UPDATE txn SET raw = ? WHERE id = 1",
        (json.dumps({"来源": "直接读取", "别的东西": "x"}, ensure_ascii=False),))
    ledger2.conn.commit()
    result2 = fixups.strip_raw(ledger2, apply=True, emit=quiet)
    check("仍然整理了", result2["changed"], 1)
    check("报告了方向不再可重推的笔数", result2["lost_direction"], 1)
    raw2 = raw_of(ledger2, 1)
    check("白名单外的键被去掉", "别的东西" in raw2, False)
    check("来源保留", raw2.get("来源"), "直接读取")

    print()
    print("=" * 78)
    print("CLI：recompute --strip-raw 默认只试算（密文一个字节都不动）")
    print("=" * 78)
    vault = os.path.join(tmp, "vault")
    plain = secure_store.plaintext_path(vault)
    os.makedirs(vault, exist_ok=True)
    disk = Ledger.open_plaintext(plain)
    disk.start_batch("alipay", "合成账单.csv", parsed=1)
    disk.add(Txn(source="alipay", ts=BASE, amount=Decimal("50.00"),
                 direction="expense", counterparty="合成便利店", item="日用品",
                 txn_id="SYN-CL-9001"))
    disk.conn.execute(
        "UPDATE txn SET raw = ? WHERE id = 1",
        (json.dumps({"来源": "直接读取", "原始": LEGACY_CELLS},
                    ensure_ascii=False),))
    disk.conn.commit()
    disk.close()

    enc = secure_store.encrypted_path(vault)
    with contextlib.redirect_stdout(io.StringIO()):
        with secure_store.open_encrypted(vault, save_on_exit=True):
            pass                       # 迁移一次，把老格式带进密文
    before_hash = digest(enc)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(["recompute", "--vault", vault, "--strip-raw"])
    check("退出码", code, 0)
    check("试算说明了没有改动", "没有改动任何数据" in buf.getvalue(), True)
    check("密文一个字节都没变", digest(enc), before_hash)

    print()
    print("=" * 78)
    print("CLI：--apply 才真的写，而且先自动备份")
    print("=" * 78)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(["recompute", "--vault", vault, "--strip-raw", "--apply"])
    check("退出码", code, 0)
    check("密文变了", digest(enc) != before_hash, True)
    backups = [n for n in os.listdir(vault) if ".backup-" in n]
    check("写库前自动备份了密文", len(backups), 1)

    with contextlib.redirect_stdout(io.StringIO()):
        with secure_store.open_encrypted(vault, save_on_exit=False) as opened:
            raw = json.loads(opened.conn.execute(
                "SELECT raw FROM txn WHERE id = 1").fetchone()["raw"])
            direction = _derive_direction(opened.conn.execute(
                "SELECT * FROM txn WHERE id = 1").fetchone())
    check("整行原文清掉了", "原始" in raw, False)
    check("方向列搬过来了", raw.get("方向列"), "收入")
    check("方向仍可重推", direction, "income")
    check("标记串没留在库里",
          [m for m in MARKERS if m in json.dumps(raw, ensure_ascii=False)], [])

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
