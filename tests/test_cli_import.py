"""`import` 子命令的报告与退出码测试（ML-006 的对外出口）。

为什么必须有这个文件：

    monthly 是自动化入口，`import` 是人手敲的入口。两条路都会碰到
    「3 笔里挂了 1 笔」，而人看到的这句话决定了他接下来干什么：

        说「没导入成功」→ 他以为一条都没进去，重新导一遍或去翻文件
        说「只导入了一部分」→ 他知道库里已经有几笔，补完再跑一次就行

    退出码同理：脚本和批处理靠它判断要不要报警。
    部分失败必须是非零 —— 否则上游会把一次漏记当成成功。

用法：<venv>\\Scripts\\python.exe tests/test_cli_import.py
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

from importer import cli, secure_store  # noqa: E402

FAILURES: list[str] = []

ROWS = [
    "2026-09-03 12:31:05,餐饮美食,合成商户甲,abc***@example.com,午餐,支出,28.50,余额宝,交易成功,SYN-C-0001,MO-SYN-0001,",
    "2026-09-04 09:10:00,交通出行,合成商户乙,def***@example.com,打车,支出,19.90,花呗,交易成功,SYN-C-0002,MO-SYN-0002,",
    "2026-09-05 20:00:00,日用百货,合成商户丙,ghi***@example.com,日用品,支出,63.20,余额,交易成功,SYN-C-0003,MO-SYN-0003,",
]


def check(name: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: 期望 {want!r}，实际 {got!r}")
    print(f"  [{'✓' if ok else '✗'}] {name:<46} -> {got!r}")


def make_bill(tmp: str, name: str) -> str:
    path = os.path.join(tmp, name)
    lines = ALIPAY_PREAMBLE + [ALIPAY_HEADER] + ROWS
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def install_trigger(vault: str, when: str) -> None:
    """在账本里装一个 INSERT 触发器，用来制造真实的单行写入失败。"""
    os.makedirs(vault, exist_ok=True)
    with secure_store.open_encrypted(vault, save_on_exit=True) as led:
        led.conn.execute(
            "CREATE TRIGGER syn_fail BEFORE INSERT ON txn WHEN " + when +
            " BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END;")
        led.conn.commit()


def run_cli(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


def txn_count(vault: str) -> int:
    with secure_store.open_encrypted(vault, save_on_exit=False) as led:
        return led.conn.execute("SELECT COUNT(*) FROM txn").fetchone()[0]


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="myledger-cli-")
    try:
        return run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(tmp: str) -> int:
    print()
    print("=" * 78)
    print("全部成功：退出码 0")
    print("=" * 78)
    vault = os.path.join(tmp, "vault-ok")
    bill = make_bill(tmp, "全成功.csv")
    code, out = run_cli(["import", bill, "--vault", vault, "--no-archive"])
    check("退出码", code, 0)
    check("报告写入笔数", "本次共写入 3 笔。" in out, True)
    check("没有失败字样", "没导入成功" in out, False)
    check("库内笔数", txn_count(vault), 3)

    print()
    print("=" * 78)
    print("部分失败：退出码必须非零，且不能说成「没导入成功」")
    print("=" * 78)
    vault = os.path.join(tmp, "vault-partial")
    install_trigger(vault, "NEW.counterparty = '合成商户乙'")
    bill = make_bill(tmp, "部分失败.csv")
    code, out = run_cli(["import", bill, "--vault", vault, "--no-archive"])
    check("退出码", code, 2)
    check("明说只导入了一部分", "只导入了一部分" in out, True)
    check("点明已入库 2 笔", "已入库 2 笔" in out, True)
    check("点明失败 1 笔", "失败 1 笔" in out, True)
    check("提示重跑不会重复入账", "已入库的会按单号/指纹跳过" in out, True)
    check("不要删源文件的提醒", "不要把源文件删掉" in out, True)
    check("已成功的 2 笔在库里", txn_count(vault), 2)

    print()
    print("=" * 78)
    print("彻底失败：退出码非零，文案是「没导入成功」")
    print("=" * 78)
    vault = os.path.join(tmp, "vault-fail")
    install_trigger(vault, "1")
    bill = make_bill(tmp, "全失败.csv")
    code, out = run_cli(["import", bill, "--vault", vault, "--no-archive"])
    check("退出码", code, 2)
    check("走的是「没导入成功」那条文案", "没导入成功" in out, True)
    check("没有说成部分失败", "只导入了一部分" in out, False)
    check("库内没有交易", txn_count(vault), 0)

    print()
    print("=" * 78)
    print("认不出来的文件：退出码非零，且不写库")
    print("=" * 78)
    vault = os.path.join(tmp, "vault-unknown")
    junk = os.path.join(tmp, "认不出来.csv")
    with open(junk, "w", encoding="utf-8", newline="") as fh:
        fh.write("这不是账单\n随便写点什么\n")
    code, out = run_cli(["import", junk, "--vault", vault, "--no-archive"])
    check("退出码", code, 2)
    check("库内没有交易", txn_count(vault), 0)

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
