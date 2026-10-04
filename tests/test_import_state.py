"""导入结果状态的回归测试（ML-006）。

为什么必须有这个文件：

    导入器是「逐笔写入、单行失败不拖垮整份账单」。这条设计本身没错，
    但收尾时必须回答一个问题：**这份账单到底算不算导完了？**
    原来的答案是 `outcome.ok = True` —— 只要循环跑完就是 True，
    于是「3 笔里挂了 1 笔」和「3 笔全好」在调用方眼里一模一样，
    monthly 会把源文件移进 _done/，失败的那笔永远不会重试。

    这个文件把五种结果的边界钉死：

        success        全部写成功（含重复跳过）→ 可以归档
        partial_failed 有行失败，也有行写进去了 → 源文件必须留在原地
        write_failed   一笔都没写成功           → 源文件必须留在原地
        parse_failed   解析阶段就失败           → 连批次都不该建
        dry_run        只试算，没写库           → 文件本来就不该动

    ok 由 status 派生，不再是独立的布尔字段 —— 两个来源迟早会不一致。

用法：<venv>\\Scripts\\python.exe tests/test_import_state.py
返回码 0 表示全部通过。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from make_fixtures import ALIPAY_HEADER, ALIPAY_PREAMBLE, ALIPAY_ROWS  # noqa: E402

from importer.pipeline import (  # noqa: E402
    OUTCOME_STATUSES,
    STATUS_LABELS,
    ImportOutcome,
    import_file,
)
from importer.store import Ledger  # noqa: E402

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: 期望 {want!r}，实际 {got!r}")
    print(f"  [{'✓' if ok else '✗'}] {name:<46} -> {got!r}")


class FlakyLedger(Ledger):
    """按条件让某几笔写入抛异常。

    用真实的写库异常（库被锁）而不是 mock：要验的正是「某一笔炸了之后
    整个批次算什么状态」，异常类型不重要，重要的是它从 add() 里抛出来。
    """

    def __init__(self, *args, should_fail=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._should_fail = should_fail or (lambda txn: False)

    def add(self, txn):
        if self._should_fail(txn):
            raise sqlite3.OperationalError("database is locked")
        return super().add(txn)


def write_alipay(path: str, rows: list[str]) -> str:
    lines = ALIPAY_PREAMBLE + [ALIPAY_HEADER] + rows
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


THREE_ROWS = [
    "2026-09-03 12:31:05,餐饮美食,合成商户甲,abc***@example.com,午餐,支出,28.50,余额宝,交易成功,SYN-A-0001,MO-SYN-0001,",
    "2026-09-04 09:10:00,交通出行,合成商户乙,def***@example.com,打车,支出,19.90,花呗,交易成功,SYN-A-0002,MO-SYN-0002,",
    "2026-09-05 20:00:00,日用百货,合成商户丙,ghi***@example.com,日用品,支出,63.20,余额,交易成功,SYN-A-0003,MO-SYN-0003,",
]


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="myledger-state-")
    try:
        return run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(tmp: str) -> int:
    print()
    print("=" * 78)
    print("成功：3 笔全部写入")
    print("=" * 78)
    path = write_alipay(os.path.join(tmp, "全成功.csv"), THREE_ROWS)
    led = Ledger(sqlite3.connect(":memory:"), os.path.join(tmp, "ledger.db.enc"))
    out = import_file(path, led, source="alipay", archive=False, emit=lambda _: None)
    check("status", out.status, "success")
    check("ok（由 status 派生）", out.ok, True)
    check("写入笔数", out.inserted, 3)
    check("批次状态", led.batch_status(), "committed")
    check("没有失败行", len(out.failed_rows), 0)

    print()
    print("=" * 78)
    print("部分失败：3 笔里挂了 1 笔（本次修复的核心场景）")
    print("=" * 78)
    path = write_alipay(os.path.join(tmp, "部分失败.csv"), THREE_ROWS)
    led = FlakyLedger(sqlite3.connect(":memory:"), os.path.join(tmp, "ledger.db.enc"),
                      should_fail=lambda t: "合成商户乙" in t.counterparty)
    out = import_file(path, led, source="alipay", archive=False, emit=lambda _: None)
    check("status", out.status, "partial_failed")
    check("ok 必须是 False", out.ok, False)
    check("成功的 2 笔已入库", out.inserted, 2)
    check("失败行记了 1 笔", len(out.failed_rows), 1)
    check("批次状态", led.batch_status(), "partial_failed")
    check("error 有话说（不能是空字符串）", bool(out.error), True)
    check("库里实际笔数", led.conn.execute("SELECT COUNT(*) FROM txn").fetchone()[0], 2)

    print()
    print("=" * 78)
    print("整批失败：一笔都没写成功")
    print("=" * 78)
    path = write_alipay(os.path.join(tmp, "全失败.csv"), THREE_ROWS)
    led = FlakyLedger(sqlite3.connect(":memory:"), os.path.join(tmp, "ledger.db.enc"),
                      should_fail=lambda t: True)
    out = import_file(path, led, source="alipay", archive=False, emit=lambda _: None)
    check("status", out.status, "write_failed")
    check("ok", out.ok, False)
    check("写入笔数", out.inserted, 0)
    check("失败行", len(out.failed_rows), 3)
    check("批次状态", led.batch_status(), "failed")

    print()
    print("=" * 78)
    print("解析失败：只有说明和表头，一笔流水都没有")
    print("=" * 78)
    stub = os.path.join(tmp, "空账单.csv")
    with open(stub, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("\n".join(ALIPAY_PREAMBLE + [ALIPAY_HEADER]) + "\n")
    led = Ledger(sqlite3.connect(":memory:"), os.path.join(tmp, "ledger.db.enc"))
    out = import_file(stub, led, source="alipay", archive=False, emit=lambda _: None)
    check("status", out.status, "parse_failed")
    check("ok", out.ok, False)
    check("没有建批次行",
          led.conn.execute("SELECT COUNT(*) FROM batch").fetchone()[0], 0)

    print()
    print("=" * 78)
    print("解析失败：认不出来的文件")
    print("=" * 78)
    junk = os.path.join(tmp, "认不出来.csv")
    with open(junk, "w", encoding="utf-8", newline="") as fh:
        fh.write("这不是账单\n随便写点什么\n")
    led = Ledger(sqlite3.connect(":memory:"), os.path.join(tmp, "ledger.db.enc"))
    out = import_file(junk, led, source="auto", archive=False, emit=lambda _: None)
    check("status", out.status, "parse_failed")
    check("ok", out.ok, False)

    print()
    print("=" * 78)
    print("dry_run：只试算，既不写库也不建批次")
    print("=" * 78)
    path = write_alipay(os.path.join(tmp, "试算.csv"), THREE_ROWS)
    led = Ledger(sqlite3.connect(":memory:"), os.path.join(tmp, "ledger.db.enc"))
    out = import_file(path, led, source="alipay", archive=False, dry_run=True,
                      emit=lambda _: None)
    check("status", out.status, "dry_run")
    check("ok（试算不算失败）", out.ok, True)
    check("库里没有交易",
          led.conn.execute("SELECT COUNT(*) FROM txn").fetchone()[0], 0)
    check("也没有批次",
          led.conn.execute("SELECT COUNT(*) FROM batch").fetchone()[0], 0)

    print()
    print("=" * 78)
    print("归档：只有全部写入成功才复制进 vault\\raw，批次才走到 archived")
    print("=" * 78)
    vault = os.path.join(tmp, "vault")
    os.makedirs(vault, exist_ok=True)
    path = write_alipay(os.path.join(tmp, "归档-成功.csv"), THREE_ROWS)
    led = Ledger(sqlite3.connect(":memory:"), os.path.join(vault, "ledger.db.enc"))
    out = import_file(path, led, source="alipay", archive=True, emit=lambda _: None)
    check("status", out.status, "success")
    check("批次走到终态", led.batch_status(), "archived")
    check("归档目录里有文件",
          len(os.listdir(os.path.join(vault, "raw"))) if os.path.isdir(
              os.path.join(vault, "raw")) else 0, 1)

    vault2 = os.path.join(tmp, "vault2")
    os.makedirs(vault2, exist_ok=True)
    path = write_alipay(os.path.join(tmp, "归档-部分失败.csv"), THREE_ROWS)
    led = FlakyLedger(sqlite3.connect(":memory:"),
                      os.path.join(vault2, "ledger.db.enc"),
                      should_fail=lambda t: "合成商户乙" in t.counterparty)
    out = import_file(path, led, source="alipay", archive=True, emit=lambda _: None)
    check("部分失败时 status", out.status, "partial_failed")
    check("批次绝不允许走到 archived", led.batch_status(), "partial_failed")
    check("源文件仍在原地", os.path.exists(path), True)
    check("没有往归档目录里塞东西",
          os.path.isdir(os.path.join(vault2, "raw")), False)

    print()
    print("=" * 78)
    print("状态集合没有漏（防止以后加状态忘了处理）")
    print("=" * 78)
    check("ok 只在 success / dry_run 下为真",
          [s for s in OUTCOME_STATUSES if ImportOutcome(path="x", status=s).ok],
          ["success", "dry_run"])
    check("状态集合里没有漏掉标签",
          [s for s in OUTCOME_STATUSES if s not in STATUS_LABELS], [])

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
