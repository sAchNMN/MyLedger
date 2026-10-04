"""结构化审计日志的测试（ML-004）。

为什么必须有这个文件：

    后台执行器以前是这么记日志的：

        $output = & $python @pyArgs 2>&1
        [System.IO.File]::AppendAllLines($LogFile, $output)

    子进程的**全部**标准输出和错误输出都被追加进 vault\\logs\\monthly-YYYYMM.log。
    解析失败的行、dry-run 的预览、异常堆栈 —— 有什么抄什么，
    而这份日志会被备份、会被同步、会在半年后被人翻出来看。

    现在改成：Python 自己写结构化日志，只写白名单字段。
    这个文件把白名单钉死：

        * 白名单外的字段被丢掉，只留下字段名（不留值）
        * 文件名里的长数字串（账号、订单号）要打掉
        * 整份日志里不许出现交易内容

用法：<venv>\\Scripts\\python.exe tests/test_audit_log.py
返回码 0 表示全部通过。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from importer.audit_log import ALLOWED_FIELDS, AuditLog, mask_name  # noqa: E402

FAILURES: list[str] = []

MARKERS = ("商户_TEST", "123.45", "TEST-001", "TEST_NOTE", "622202123456")


def check(name: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: 期望 {want!r}，实际 {got!r}")
    print(f"  [{'✓' if ok else '✗'}] {name:<46} -> {got!r}")


def read_events(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="myledger-audit-")
    try:
        return run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(tmp: str) -> int:
    log = os.path.join(tmp, "logs", "monthly-202610.log")

    print()
    print("=" * 78)
    print("只写白名单字段")
    print("=" * 78)
    audit = AuditLog(log)
    record = audit.record(
        "file_done",
        source="alipay",
        status="partial_failed",
        parsed=3,
        inserted=2,
        failed_rows=1,
        file_masked="合成账单.csv",
        # 下面这些是「不该进日志」的东西，混进来会被丢掉
        counterparty="商户_TEST",
        amount="123.45",
        txn_id="TEST-001",
        note="TEST_NOTE",
    )
    check("白名单字段留下了", record.get("status"), "partial_failed")
    check("白名单外的字段没进记录", "counterparty" in record, False)
    check("被丢掉的字段名有记录（只记名字，不记值）",
          sorted(audit.rejected()), ["amount", "counterparty", "note", "txn_id"])
    check("被丢掉的字段名里不含值",
          [k for k in audit.rejected() if any(m in k for m in MARKERS)], [])

    audit.record("run_end", ok=0, partial_failed=1, failed=0, exit_code=2)

    print()
    print("=" * 78)
    print("日志文件内容扫描")
    print("=" * 78)
    events = read_events(log)
    check("写了两条事件", len(events), 2)
    check("每条都有时间戳", all("ts" in e for e in events), True)
    check("事件名", [e["event"] for e in events], ["file_done", "run_end"])
    blob = open(log, "r", encoding="utf-8").read()
    check("整份日志里一个标记串都没有",
          [m for m in MARKERS if m in blob], [])
    check("日志是 JSON Lines（逐行可解析）", len(blob.strip().splitlines()), 2)

    print()
    print("=" * 78)
    print("文件名脱敏：账号/订单号经常就藏在文件名里")
    print("=" * 78)
    check("长数字串被打掉",
          mask_name("招商银行_张三_622202123456_202609.pdf"),
          "招商银行_张三_####_####.pdf")
    check("短数字不受影响（日期里的月份之类）", mask_name("账单_09月.csv"), "账单_09月.csv")
    check("没有数字就原样返回", mask_name("合成账单.csv"), "合成账单.csv")

    print()
    print("=" * 78)
    print("白名单本身")
    print("=" * 78)
    for forbidden in ("counterparty", "item", "note", "txn_id", "amount",
                      "path", "cmdline", "traceback", "raw"):
        check(f"禁止字段 {forbidden} 不在白名单里",
              forbidden in ALLOWED_FIELDS, False)

    print()
    print("=" * 78)
    print("追加写：多次运行不覆盖旧日志")
    print("=" * 78)
    audit2 = AuditLog(log)
    audit2.record("run_start", files=2)
    events = read_events(log)
    check("旧记录还在", len(events), 3)
    check("新记录追加在最后", events[-1]["event"], "run_start")
    check("文件不存在时自动建目录",
          os.path.isdir(os.path.dirname(log)), True)

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
