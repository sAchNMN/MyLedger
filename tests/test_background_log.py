"""后台执行器的日志隐私测试（ML-004，端到端）。

为什么必须有这个文件：

    ML-007 修的是「解析失败会把原始行打印出来」，但如果后台执行器还在
    无条件把子进程的全部输出抄进日志，那就等于把上游的修复白做了 ——
    打印出来和写进文件是两件事，而写进文件的这份才是长期存在的。

    所以这里不测函数，**直接跑真的 scripts\\monthly-run.ps1**：
    丢两份合成账单进投递目录、装一个真实的 SQLite 触发器让一笔写失败、
    执行后台执行器，然后按字节扫日志文件。

    验三件事：
      1. 日志里没有任何交易内容（标记串一个都不许出现）
      2. 日志里没有文件名原文（长数字串被脱敏成 ####）
      3. 该记的还在：每个文件的状态、计数、错误码、退出码

用法：<venv>\\Scripts\\python.exe tests/test_background_log.py
返回码 0 表示全部通过。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from make_fixtures import ALIPAY_HEADER, ALIPAY_PREAMBLE  # noqa: E402

from importer import monthly, secure_store  # noqa: E402

RUNNER = os.path.join(ROOT, "scripts", "monthly-run.ps1")

MARKERS = ("商户_TEST", "123.45", "TEST-001", "TEST_NOTE", "acct_TEST", "商品_TEST")

LONG_DIGITS = "622202123456"

GOOD_A = ("2026-09-03 12:31:05,餐饮美食,合成商户甲,abc***@example.com,午餐,支出,"
          "28.50,余额宝,交易成功,SYN-BG-0001,MO-SYN-BG-0001,")
# 金额是坏值，整行塞满标记串
BAD_ROW = ("2026-09-04 09:10:00,餐饮美食,商户_TEST,acct_TEST,商品_TEST,支出,"
           "不是数字,TEST-001,TEST-002,TEST-003,TEST-004,TEST_NOTE")
GOOD_B1 = ("2026-09-05 20:00:00,日用百货,合成商户乙,def***@example.com,日用品,支出,"
           "63.20,余额,交易成功,SYN-BG-0002,MO-SYN-BG-0002,")
GOOD_B2 = ("2026-09-06 08:15:00,交通出行,合成商户丙,ghi***@example.com,打车,支出,"
           "19.90,花呗,交易成功,SYN-BG-0003,MO-SYN-BG-0003,")
GOOD_B3 = ("2026-09-07 19:40:00,日用百货,合成商户丁,jkl***@example.com,日用品,支出,"
           "45.00,余额,交易成功,SYN-BG-0004,MO-SYN-BG-0004,")

FAIL_TRIGGER = """
CREATE TRIGGER syn_fail BEFORE INSERT ON txn
WHEN NEW.counterparty = '合成商户乙'
BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END;
"""

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: 期望 {want!r}，实际 {got!r}")
    print(f"  [{'✓' if ok else '✗'}] {name:<46} -> {got!r}")


def write_bill(path: str, rows: list[str]) -> None:
    lines = ALIPAY_PREAMBLE + [ALIPAY_HEADER] + rows
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("\n".join(lines) + "\n")


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="myledger-bglog-")
    try:
        return run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(tmp: str) -> int:
    print()
    print("=" * 78)
    print("前提：脚本本身得能被 PowerShell 正确读出来")
    print("=" * 78)
    # Windows PowerShell 5.1 读无 BOM 的 .ps1 时按系统 ANSI（本机是 GBK）解码，
    # 中文注释会被解成乱码，而乱码里一旦出现引号，整个脚本就是语法错误 ——
    # 报错信息还停在「Missing closing }」这种完全指不到病根的地方。
    # 重写 monthly-run.ps1 时就是这么炸过一次，所以钉一条守卫。
    scripts = sorted(n for n in os.listdir(os.path.join(ROOT, "scripts"))
                     if n.endswith(".ps1"))
    check("scripts/ 下所有 .ps1 都带 UTF-8 BOM",
          [n for n in scripts
           if open(os.path.join(ROOT, "scripts", n), "rb").read(3) != b"\xef\xbb\xbf"],
          [])

    vault = os.path.join(tmp, "vault")

    # 先建好账本并装上触发器：让「合成商户乙」那一笔真的写不进去，
    # 从而走 partial_failed 这条路径。
    with secure_store.open_encrypted(vault, save_on_exit=True) as led:
        led.conn.execute(FAIL_TRIGGER)
        led.conn.commit()

    box = monthly.ensure_inbox(vault)
    good_name = f"合成账单-某商户-{LONG_DIGITS}.csv"
    bad_name = "合成账单-写入失败.csv"
    write_bill(os.path.join(box, good_name), [GOOD_A, BAD_ROW])
    write_bill(os.path.join(box, bad_name), [GOOD_B1, GOOD_B2, GOOD_B3])

    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", RUNNER, "-Vault", vault],
        cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    stdout = proc.stdout or ""

    print()
    print("=" * 78)
    print("退出码与屏幕输出")
    print("=" * 78)
    check("有批次没完成 → 退出码 2", proc.returncode, 2)
    check("报告仍然打在屏幕上（只是不进日志）", "发现 2 个待处理文件" in stdout, True)
    check("屏幕输出里也没有标记串",
          [m for m in MARKERS if m in stdout], [])

    print()
    print("=" * 78)
    print("日志文件：全部是 JSON 行")
    print("=" * 78)
    log_file = os.path.join(vault, "logs",
                            "monthly-{0}.log".format(__import__("datetime").datetime.now().strftime("%Y%m")))
    check("日志文件已生成", os.path.isfile(log_file), True)
    raw = open(log_file, "r", encoding="utf-8").read()
    events = []
    bad_lines = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            bad_lines.append(line[:80])
    check("没有解析不了的行", bad_lines, [])
    check("事件顺序",
          [e["event"] for e in events],
          ["runner_start", "run_start", "file_done", "file_done",
           "run_end", "runner_end"])

    print()
    print("=" * 78)
    print("隐私扫描：日志里不许有交易内容")
    print("=" * 78)
    check("标记串一个都不出现", [m for m in MARKERS if m in raw], [])
    check("文件名里的长数字串没有原文", LONG_DIGITS in raw, False)
    check("文件名已脱敏（有 #### 占位）", "####" in raw, True)
    check("没有完整路径", vault in raw, False)

    print()
    print("=" * 78)
    print("该记的还在（不然日志没用了）")
    print("=" * 78)
    files = [e for e in events if e["event"] == "file_done"]
    by_status = {e["status"]: e for e in files}
    check("两个文件各一条", len(files), 2)

    ok_event = by_status.get("success")
    check("成功那条记了 status", ok_event is not None, True)
    check("成功那条记了解析/跳过笔数",
          (ok_event["parsed"], ok_event["skipped_rows"], ok_event["inserted"]),
          (1, 1, 1))
    check("成功那条记了错误码（不含取值）",
          ok_event["issue_codes"], ["invalid_amount"])

    partial_event = by_status.get("partial_failed")
    check("部分失败那条记了 status", partial_event is not None, True)
    check("部分失败那条记了计数",
          (partial_event["inserted"], partial_event["failed_rows"]), (2, 1))
    check("部分失败那条只记异常类型名",
          partial_event["write_error_types"], ["IntegrityError"])
    check("没有把异常消息写进日志", "synthetic write failure" in raw, False)

    run_end = [e for e in events if e["event"] == "run_end"][0]
    check("收尾汇总的退出码", run_end["exit_code"], 2)
    check("收尾汇总的计数",
          (run_end["ok"], run_end["partial_failed"], run_end["failed"]), (1, 1, 0))

    print()
    print("=" * 78)
    print("行为没变：成功的归档，失败的留在原地")
    print("=" * 78)
    check("成功的文件已移入 _done/",
          os.path.exists(os.path.join(monthly.done_dir(vault), good_name)), True)
    check("部分失败的文件仍在投递目录",
          os.path.exists(os.path.join(box, bad_name)), True)

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
