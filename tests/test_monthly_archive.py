"""月度批处理的归档条件测试（ML-006 的收尾一环）。

为什么必须有这个文件：

    前面已经把状态机修好了（批次不会在部分失败时变成 committed），
    但只要 monthly 还在拿 `outcome.ok` 之外的东西当归档依据，
    「部分失败也把文件挪进 _done/」这条路就还开着。
    文件一旦被挪走，下一次自动运行扫不到它，失败的那几笔就再也不会被重试，
    报表上永远少这几笔 —— 而且全程没有任何报错。

    所以这里验的是一件很具体的事：

        源文件被移动 ⟺ 这一批全部写入成功

    失败注入用的是真的 SQLite 触发器，不是 mock：
    触发器在 INSERT 时 RAISE(ABORT)，从 ledger.add() 里抛出来的是一个真实的
    写库异常，和「库被锁」「磁盘满」走的是同一条代码路径。

用法：<venv>\\Scripts\\python.exe tests/test_monthly_archive.py
返回码 0 表示全部通过。
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from make_fixtures import ALIPAY_HEADER, ALIPAY_PREAMBLE  # noqa: E402

from importer import monthly, secure_store  # noqa: E402

FAILURES: list[str] = []

ROWS = [
    "2026-09-03 12:31:05,餐饮美食,合成商户甲,abc***@example.com,午餐,支出,28.50,余额宝,交易成功,SYN-M-0001,MO-SYN-0001,",
    "2026-09-04 09:10:00,交通出行,合成商户乙,def***@example.com,打车,支出,19.90,花呗,交易成功,SYN-M-0002,MO-SYN-0002,",
    "2026-09-05 20:00:00,日用百货,合成商户丙,ghi***@example.com,日用品,支出,63.20,余额,交易成功,SYN-M-0003,MO-SYN-0003,",
]

# 让「合成商户乙」这一笔写不进去。RAISE(ABORT) 抛的是真实的 sqlite3 异常。
FAIL_TRIGGER = """
CREATE TRIGGER syn_fail_merchant_yi BEFORE INSERT ON txn
WHEN NEW.counterparty = '合成商户乙'
BEGIN
    SELECT RAISE(ABORT, 'synthetic write failure');
END;
"""


def check(name: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: 期望 {want!r}，实际 {got!r}")
    print(f"  [{'✓' if ok else '✗'}] {name:<48} -> {got!r}")


def write_bill(path: str, rows: list[str]) -> None:
    lines = ALIPAY_PREAMBLE + [ALIPAY_HEADER] + rows
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("\n".join(lines) + "\n")


def drop_bill(vault: str, name: str) -> str:
    """把一份合成账单丢进投递目录，返回它的路径。"""
    box = monthly.ensure_inbox(vault)
    path = os.path.join(box, name)
    write_bill(path, ROWS)
    return path


def set_trigger(vault: str, sql: str, *, install: bool) -> None:
    with secure_store.open_encrypted(vault, save_on_exit=True) as led:
        if install:
            led.conn.execute(sql)
        else:
            led.conn.execute("DROP TRIGGER syn_fail_merchant_yi")
        led.conn.commit()


def run_monthly(vault: str) -> tuple[int, str]:
    """跑一次月度批处理，把它的输出收起来（测试自己的输出才看得清）。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = monthly.run(vault, use_ai=False, auto_review=False)
    return code, buf.getvalue()


def txn_count(vault: str) -> int:
    with secure_store.open_encrypted(vault, save_on_exit=False) as led:
        return led.conn.execute("SELECT COUNT(*) FROM txn").fetchone()[0]


def batch_statuses(vault: str) -> list[str]:
    with secure_store.open_encrypted(vault, save_on_exit=False) as led:
        return [r["status"] for r in led.conn.execute("SELECT status FROM batch")]


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="myledger-monthly-")
    try:
        return run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(tmp: str) -> int:
    print()
    print("=" * 78)
    print("全部成功：文件移进 _done/，退出码 0")
    print("=" * 78)
    vault = os.path.join(tmp, "vault-ok")
    source = drop_bill(vault, "合成账单-全成功.csv")
    code, out = run_monthly(vault)
    check("退出码", code, 0)
    check("源文件已不在投递目录", os.path.exists(source), False)
    check("已出现在 _done/",
          os.path.exists(os.path.join(monthly.done_dir(vault),
                                      "合成账单-全成功.csv")), True)
    check("批次状态", batch_statuses(vault), ["archived"])
    check("入库笔数", txn_count(vault), 3)

    print()
    print("=" * 78)
    print("部分失败：文件必须留在原地，退出码 2")
    print("=" * 78)
    vault = os.path.join(tmp, "vault-partial")
    source = drop_bill(vault, "合成账单-部分失败.csv")
    set_trigger(vault, FAIL_TRIGGER, install=True)
    code, out = run_monthly(vault)
    check("退出码", code, 2)
    check("源文件仍在投递目录", os.path.exists(source), True)
    check("_done/ 里没有它",
          os.path.exists(os.path.join(monthly.done_dir(vault),
                                      "合成账单-部分失败.csv")), False)
    check("批次状态", batch_statuses(vault), ["partial_failed"])
    check("成功的 2 笔仍已入库", txn_count(vault), 2)
    check("输出里说明了「留在原地」", "留在原地" in out, True)
    check("报告把「部分失败」和「失败」分开数",
          "成功 0 个，部分失败 1 个，失败 0 个" in out, True)
    check("说明里点明了已入库几笔", "已写入 2 笔、失败 1 笔" in out, True)

    print()
    print("=" * 78)
    print("重试：补完问题后再跑一次，文件才归档，且不产生重复")
    print("=" * 78)
    set_trigger(vault, FAIL_TRIGGER, install=False)
    code, out = run_monthly(vault)
    check("退出码", code, 0)
    check("这次移进了 _done/", os.path.exists(source), False)
    check("库内笔数仍是 3（没有重复入账）", txn_count(vault), 3)
    check("批次状态序列", batch_statuses(vault) == ["partial_failed", "archived"], True)
    check("报告不再有部分失败",
          "成功 1 个，部分失败 0 个，失败 0 个" in out, True)

    print()
    print("=" * 78)
    print("--keep：不移动文件，但仍要求全部成功")
    print("=" * 78)
    vault = os.path.join(tmp, "vault-keep")
    source = drop_bill(vault, "合成账单-keep.csv")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = monthly.run(vault, use_ai=False, auto_review=False, keep=True)
    check("退出码", code, 0)
    check("文件原地不动", os.path.exists(source), True)
    check("库内笔数", txn_count(vault), 3)

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
