"""明文库迁移的验证测试（ML-009 的前半）。

为什么必须有这个文件：

    原来的迁移是这样：

        读 ledger.db → 加密写 ledger.db.enc → 明文 ledger.db 继续留着
        然后打印一句「明文文件不会被自动删除，请你手动处理它」

    「加密迁移完成」这句话当时是没有依据的：protect() 返回了，只能说明
    字节写出去了，不能说明这些字节能变回一个能用的数据库。
    加解密、序列化、原子替换 —— 任何一步出错都可能留下一份打不开或者
    缺表的密文，而明文还躺在旁边，看起来一切正常。
    等哪天你手滑把明文删了，才会发现备份是空的。

    所以这个文件钉住两件事：

        1. 迁移完必须把密文当成陌生文件重新打开，逐项核对
           （表清单、笔数、金额合计、时间范围、最大 id）
        2. 核对不过就撤销：删掉刚写的密文、原样保留明文、抛错
           —— 半成品密文比没有密文更危险
        3. 明文不是账本（缺 txn/batch 表）就拒绝迁移，一个字都不写

用法：<venv>\\Scripts\\python.exe tests/test_store_migration.py
返回码 0 表示全部通过。
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import sqlite3
import sys
import tempfile
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from importer import credentials, secure_store  # noqa: E402
from importer.model import Txn  # noqa: E402
from importer.store import Ledger  # noqa: E402

FAILURES: list[str] = []

BASE = dt.datetime(2026, 9, 3, 12, 0, 0)


def check(name: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: 期望 {want!r}，实际 {got!r}")
    print(f"  [{'✓' if ok else '✗'}] {name:<46} -> {got!r}")


def check_raises(name: str, fn):
    try:
        fn()
    except Exception as exc:
        print(f"  [✓] {name:<46} -> 拒绝（{type(exc).__name__}: {exc}）")
        return exc
    FAILURES.append(f"{name}: 期望抛异常，但没有")
    print(f"  [✗] {name:<46} -> 竟然通过了")
    return None


def txn(offset_min: int, amount: str, counterparty: str, txn_id: str) -> Txn:
    return Txn(source="alipay", ts=BASE + dt.timedelta(minutes=offset_min),
               amount=Decimal(amount), direction="expense",
               counterparty=counterparty, item="日用品", txn_id=txn_id)


ROWS = [
    txn(1, "28.50", "合成商户甲", "SYN-M-0001"),
    txn(2, "19.90", "合成商户乙", "SYN-M-0002"),
    txn(3, "63.20", "合成商户丙", "SYN-M-0003"),
]


def make_plaintext(path: str) -> None:
    """造一个合法的明文账本（走公开 API，不手写 schema）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    led = Ledger.open_plaintext(path)
    led.start_batch("alipay", "合成账单.csv", parsed=len(ROWS))
    for row in ROWS:
        led.add(row)
    led.finish_batch(len(ROWS), len(ROWS), len(ROWS), 0, 0, 0)
    led.close()


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="myledger-migrate-")
    try:
        return run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(tmp: str) -> int:
    print()
    print("=" * 78)
    print("正常迁移：密文必须能被独立打开，且内容与源库一致")
    print("=" * 78)
    vault = os.path.join(tmp, "vault-ok")
    plain = secure_store.plaintext_path(vault)
    make_plaintext(plain)
    check("前提：明文库已存在", os.path.exists(plain), True)

    with secure_store.open_ledger(vault) as led:
        before = secure_store.ledger_fingerprint(led.conn)
    check("迁移后密文存在", os.path.exists(secure_store.encrypted_path(vault)), True)
    check("源库笔数", before["txn"], 3)

    # 重新打开（这次走的是密文那条路），逐项核对
    with secure_store.open_ledger(vault) as led:
        after = secure_store.ledger_fingerprint(led.conn)
    check("重新打开后指纹一致", after, before)
    check("金额合计保住了", after["total"], "111.60")
    check("时间范围保住了", (after["first_ts"], after["last_ts"]),
          ("2026-09-03 12:01:00", "2026-09-03 12:03:00"))

    print()
    print("=" * 78)
    print("迁移写完的密文，本次运行内就校验过（不是等下次打开才发现坏）")
    print("=" * 78)
    check("vault 根目录里已经没有裸的明文", os.path.exists(plain), False)
    check("但也没被删掉（挪进了隔离目录）",
          len(secure_store.find_residue(vault)), 1)
    info = secure_store.status(vault)
    check("status 报告 vault 根下没有明文了", info["plaintext_exists"], False)
    check("status 报告附属文件列表（可能为空）", isinstance(info["wal_files"], list), True)

    print()
    print("=" * 78)
    print("明文不是账本：拒绝迁移，一个字都不写")
    print("=" * 78)
    vault2 = os.path.join(tmp, "vault-notledger")
    os.makedirs(vault2, exist_ok=True)
    other = secure_store.plaintext_path(vault2)
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE 随便什么 (x TEXT)")
    conn.execute("INSERT INTO 随便什么 VALUES ('不是账本')")
    conn.commit()
    conn.close()
    check_raises("冒名顶替的 sqlite 文件",
                 lambda: secure_store.open_ledger(vault2))
    check("没有写出密文",
          os.path.exists(secure_store.encrypted_path(vault2)), False)
    check("源文件原封不动", os.path.exists(other), True)

    print()
    print("=" * 78)
    print("密文校验失败：撤销本次迁移，明文原样保留")
    print("=" * 78)
    vault3 = os.path.join(tmp, "vault-corrupt")
    make_plaintext(secure_store.plaintext_path(vault3))
    real_protect = credentials.protect
    # 让加密返回一段垃圾：写入会「成功」，但那份密文根本解不开 ——
    # 正是要防的那种「看起来迁移完了，其实备份是废的」。
    credentials.protect = lambda data: b"this is not a dpapi blob"
    try:
        exc = check_raises("写出来的密文解不开",
                           lambda: secure_store.open_ledger(vault3))
    finally:
        credentials.protect = real_protect
    check("报错说清了是校验没过",
          "校验" in str(exc) if exc else False, True)
    check("半成品密文被删掉了（不能留在那冒充备份）",
          os.path.exists(secure_store.encrypted_path(vault3)), False)
    check("明文完好无损",
          os.path.exists(secure_store.plaintext_path(vault3)), True)

    print()
    print("=" * 78)
    print("修好之后重跑：明文还能正常迁移（没有把自己锁死）")
    print("=" * 78)
    with secure_store.open_ledger(vault3) as led:
        fixed = secure_store.ledger_fingerprint(led.conn)
    check("这次迁移成功", fixed["txn"], 3)
    check("密文在了", os.path.exists(secure_store.encrypted_path(vault3)), True)

    print()
    print("=" * 78)
    print("附属文件检测：-wal / -shm 也要能被看见")
    print("=" * 78)
    vault4 = os.path.join(tmp, "vault-wal")
    plain4 = secure_store.plaintext_path(vault4)
    make_plaintext(plain4)
    for suffix in ("-wal", "-shm"):
        with open(plain4 + suffix, "wb") as fh:
            fh.write(b"stale")
    found = secure_store.plaintext_artifacts(vault4)
    check("三件套都被列出来",
          sorted(os.path.basename(p) for p in found),
          ["ledger.db", "ledger.db-shm", "ledger.db-wal"])
    check("status 里的附属文件",
          sorted(os.path.basename(p) for p in secure_store.status(vault4)["wal_files"]),
          ["ledger.db-shm", "ledger.db-wal"])

    print()
    print("=" * 78)
    print("明文隔离：迁移成功后，明文不再是 vault 根目录里的裸文件")
    print("=" * 78)
    vault5 = os.path.join(tmp, "vault-quarantine")
    plain5 = secure_store.plaintext_path(vault5)
    make_plaintext(plain5)
    messages: list[str] = []
    with secure_store.open_ledger(vault5, warn=messages.append) as led:
        check("迁移后内容正确", secure_store.ledger_fingerprint(led.conn)["txn"], 3)
    check("明文主文件已不在 vault 根", os.path.exists(plain5), False)
    check("-wal / -shm 也不在 vault 根",
          [os.path.exists(plain5 + s) for s in ("-wal", "-shm")], [False, False])
    box = secure_store.quarantine_dir(vault5)
    check("隔离目录建好了", os.path.isdir(box), True)
    stamps = sorted(os.listdir(box)) if os.path.isdir(box) else []
    check("有一个隔离批次", len(stamps), 1)
    inside = sorted(os.listdir(os.path.join(box, stamps[0]))) if stamps else []
    check("是移动不是删除（文件还在）", inside, ["ledger.db"])
    check("隔离文件有内容",
          os.path.getsize(os.path.join(box, stamps[0], "ledger.db")) > 0, True)
    check("迁移过程里说明了处置办法",
          any("隔离" in m for m in messages), True)

    print()
    print("=" * 78)
    print("残留提醒：每次启动都提醒，不能只提醒一次")
    print("=" * 78)
    again: list[str] = []
    with secure_store.open_ledger(vault5, warn=again.append):
        pass
    check("再次打开仍然强提醒", any("明文副本" in m for m in again), True)
    check("提醒里点名了具体位置",
          any("_plaintext_quarantine" in m for m in again), True)
    check("提醒里告诉人怎么删",
          any("Remove-Item" in m for m in again), True)

    residue = secure_store.find_residue(vault5)
    check("隔离目录算作残留", len(residue), 1)
    check("残留路径指向那份文件", residue[0].endswith("ledger.db"), True)

    print()
    print("=" * 78)
    print("store 子命令必须把残留说出来")
    print("=" * 78)
    import contextlib  # noqa: E402
    import io  # noqa: E402

    from importer import cli  # noqa: E402

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(["store", "--vault", vault5])
    printed = buf.getvalue()
    check("退出码", code, 0)
    check("说了还有明文副本", "还有明文副本" in printed, True)
    check("给出了删除命令", "Remove-Item" in printed, True)

    print()
    print("=" * 78)
    print("用户手动把明文拷回来：照样被认出来并提醒")
    print("=" * 78)
    shutil.copy2(residue[0], plain5)
    residue = secure_store.find_residue(vault5)
    check("两份残留都被列出来", len(residue), 2)
    back: list[str] = []
    with secure_store.open_ledger(vault5, warn=back.append):
        pass
    check("提醒里同时点名 vault 根目录下的明文",
          any(os.path.basename(m.rstrip()) == "ledger.db" and "vault-quarantine" in m
              for m in back), True)

    print()
    print("=" * 78)
    print("迁移校验失败时不得隔离明文（数据还没被证明安全）")
    print("=" * 78)
    vault6 = os.path.join(tmp, "vault-fail-noquarantine")
    plain6 = secure_store.plaintext_path(vault6)
    make_plaintext(plain6)
    credentials.protect = lambda data: b"still not a dpapi blob"
    try:
        check_raises("坏密文", lambda: secure_store.open_ledger(vault6))
    finally:
        credentials.protect = real_protect
    check("明文仍在 vault 根目录", os.path.exists(plain6), True)
    check("没有建隔离目录",
          os.path.isdir(secure_store.quarantine_dir(vault6)), False)

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
