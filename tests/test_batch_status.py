"""批次状态机的回归测试。

为什么必须有这个文件（ML-006）：

    导入是「逐笔写入、单行失败不拖垮整份账单」，这条设计没错。
    错在收尾：只要循环跑完就 `outcome.ok = True`，
    于是「100 笔里挂了 2 笔」和「100 笔全好」在记账侧长得一模一样。
    monthly 拿 `ok` 当作可以归档的信号，把源文件移进 `_done/` ——
    那 2 笔从此不会再被重试，报表上永远少两笔，而且没有任何报错。

    所以批次必须有明确状态，并且**部分失败不得归档**。
    这一层是后面 pipeline / monthly 改动的契约：
    状态名、允许的流转、以及「老库要能补列」都在这里钉死。

状态流转：

    started ──→ writing ──→ committed ──→ archived
                  │
                  └──→ partial_failed ──→ writing（重试）

    最关键的硬约束是 **archived 只能从 committed 来**：
      * started → archived        没写就说归档了，文件会丢
      * partial_failed → archived 部分失败不得归档（本文件的重点）
      * failed → archived         同上
    其次已提交的事务不能回退（committed → writing），
    归档是终态（archived → 任何）。
    `writing` 是可选中间态：小文件解析完直接一次写完，允许 started → committed。

用法：<venv>\\Scripts\\python.exe tests/test_batch_status.py
返回码 0 表示全部通过。
"""

from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from importer.store import BATCH_STATUSES, Ledger  # noqa: E402

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: 期望 {want!r}，实际 {got!r}")
    print(f"  [{'✓' if ok else '✗'}] {name:<50} -> {got!r}")


def check_raises(name: str, fn) -> None:
    """期望抛 ValueError。抛了才算通过。"""
    try:
        fn()
    except ValueError as exc:
        print(f"  [✓] {name:<50} -> 拒绝（{exc}）")
        return
    except Exception as exc:      # 抛了别的异常也算失败
        FAILURES.append(f"{name}: 期望 ValueError，实际 {type(exc).__name__}: {exc}")
        print(f"  [✗] {name:<50} -> {type(exc).__name__}: {exc}")
        return
    FAILURES.append(f"{name}: 期望 ValueError，但没有抛")
    print(f"  [✗] {name:<50} -> 竟然通过了")


def fresh() -> Ledger:
    led = Ledger(sqlite3.connect(":memory:"), ":memory:")
    led.start_batch("test", "合成-账单.csv", parsed=3)
    return led


# 老版本的 batch 表：没有 status 列。用来验证老库能被自动补列。
LEGACY_SCHEMA = """
CREATE TABLE batch (
    batch_id     TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    path         TEXT NOT NULL,
    origin       TEXT,
    file_sha1    TEXT,
    file_size    INTEGER,
    imported_at  TEXT NOT NULL,
    parsed       INTEGER DEFAULT 0,
    declared     INTEGER,
    inserted     INTEGER DEFAULT 0,
    identical    INTEGER DEFAULT 0,
    suspect      INTEGER DEFAULT 0,
    skipped      INTEGER DEFAULT 0,
    note         TEXT DEFAULT ''
);
"""


def main() -> int:
    print()
    print("=" * 78)
    print("状态定义")
    print("=" * 78)
    check("状态集合", sorted(BATCH_STATUSES),
          ["archived", "committed", "failed", "partial_failed", "started", "writing"])

    print()
    print("=" * 78)
    print("新建批次：started")
    print("=" * 78)
    led = fresh()
    check("刚建好的批次状态", led.batch_status(), "started")

    print()
    print("=" * 78)
    print("正常路径：started → writing → committed → archived")
    print("=" * 78)
    led.set_batch_status("writing")
    check("开始写库", led.batch_status(), "writing")
    led.set_batch_status("committed")
    check("全部写入成功", led.batch_status(), "committed")
    led.set_batch_status("archived")
    check("源文件已归档", led.batch_status(), "archived")

    print()
    print("=" * 78)
    print("部分失败：started → writing → partial_failed，且不得归档")
    print("=" * 78)
    led = fresh()
    led.set_batch_status("writing")
    led.set_batch_status("partial_failed")
    check("有行写失败", led.batch_status(), "partial_failed")
    check_raises("partial_failed → archived（本文件重点）",
                 lambda: led.set_batch_status("archived"))
    check("被拒之后状态没变", led.batch_status(), "partial_failed")

    print()
    print("=" * 78)
    print("非法流转")
    print("=" * 78)
    led = fresh()
    check_raises("started → archived（没写就说归档了）",
                 lambda: led.set_batch_status("archived"))
    led = fresh()
    led.set_batch_status("writing")
    led.set_batch_status("failed")
    check_raises("failed → archived（失败不得归档）",
                 lambda: led.set_batch_status("archived"))
    led = fresh()
    led.set_batch_status("writing")
    led.set_batch_status("committed")
    check_raises("committed → writing（事务已提交，不能回退）",
                 lambda: led.set_batch_status("writing"))
    led.set_batch_status("archived")
    check_raises("archived → committed（归档是终态）",
                 lambda: led.set_batch_status("committed"))
    check_raises("不认识的状态名", lambda: led.set_batch_status("差不多成功了"))
    led = fresh()
    check_raises("没有打开的批次就查状态", lambda: Ledger(
        sqlite3.connect(":memory:"), ":memory:").batch_status())

    print()
    print("=" * 78)
    print("重试：partial_failed 可以回到 writing 再重新提交")
    print("=" * 78)
    led = fresh()
    led.set_batch_status("writing")
    led.set_batch_status("partial_failed")
    led.set_batch_status("writing")
    check("补完失败行后重新提交", led.batch_status(), "writing")
    led.set_batch_status("committed")
    check("这次全好了", led.batch_status(), "committed")

    print()
    print("=" * 78)
    print("finish_batch 落状态（统计与状态一次写完）")
    print("=" * 78)
    led = fresh()
    led.set_batch_status("writing")
    led.finish_batch(parsed=3, declared=3, inserted=1, identical=0,
                     suspect=0, skipped=2, status="partial_failed")
    check("finish_batch 写完后的状态", led.batch_status(), "partial_failed")
    row = led.conn.execute(
        "SELECT parsed, inserted, skipped FROM batch WHERE batch_id = ?",
        (led.batch_id,)).fetchone()
    check("统计同时落库", (row["parsed"], row["inserted"], row["skipped"]), (3, 1, 2))

    print()
    print("=" * 78)
    print("老库兼容：没有 status 列的批表要能自动补上")
    print("=" * 78)
    conn = sqlite3.connect(":memory:")
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO batch (batch_id, source, path, imported_at, parsed, inserted)"
        " VALUES ('legacy-1', 'alipay', '老账单.csv', '2026-01-01 00:00:00', 10, 10)")
    conn.commit()
    legacy = Ledger(conn, ":memory:", init_schema=False)
    legacy.batch_id = "legacy-1"
    columns = [r[1] for r in conn.execute("PRAGMA table_info(batch)")]
    check("补列成功", "status" in columns, True)
    check("老批次默认视为 started", legacy.batch_status(), "started")
    legacy.set_batch_status("writing")
    legacy.set_batch_status("committed")
    check("老库也能正常流转", legacy.batch_status(), "committed")

    print()
    print("=" * 78)
    print("batch_id 唯一性：同一秒内重跑同一个文件不能撞主键")
    print("=" * 78)
    # batch_id 原来是「秒级时间戳 + 文件 sha 前 6 位」。
    # 部分失败之后马上重跑，或者自动化在同一秒里跑两次同一个文件，
    # 两次算出来的 id 一模一样 → INSERT 撞 batch 的主键 →
    # 整个导入在 start_batch 就崩掉，源文件永远处理不了。
    led = Ledger(sqlite3.connect(":memory:"), ":memory:")
    ids = [led.start_batch("alipay", "同一个合成文件.csv", parsed=3) for _ in range(3)]
    check("三次拿到的 id 互不相同", len(set(ids)), 3)
    check("库里三次批次都在",
          led.conn.execute("SELECT COUNT(*) FROM batch").fetchone()[0], 3)
    check("id 仍然可读（带序号后缀）", ids[1].startswith(ids[0]), True)

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
