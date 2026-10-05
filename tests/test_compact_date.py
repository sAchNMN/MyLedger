"""紧凑日期/时间：YYYYMMDD / HHMMSS。

真实事故
--------
用户导入银行账单（zip 里那份加密 PDF），AES 解密修好后能拿到 PDF 了，
但**解析出 0 笔、跳过 0 行**。跑 `bank-pdf-probe.py`（输出全脱敏）拿到结构：

    含日期形态的格 : 0        ← 死因在这
    含两位小数的格 : 8

交易行形如：
    99999999 | 999999 | 字字 | +9999.99 | 9999.99 | 字字字 | a999999999 | 字字字字
    （8 位数字 = 20260903，6 位数字 = 083000）

**这个 PDF 里一个带 `-` `/` `.` 分隔符的日期都没有。**
而 `sources._RE_DATE_TOKEN` 恰好要求分隔符，于是每一行都走
`if not dates: continue` —— 0 笔，且因为丢弃时不记 skipped，
日志只剩两句「解析 0 笔 / 跳过 0 行」，用户完全不知道发生了什么。

紧凑写法在国内银行 PDF 里很常见，不是这份账单的特殊怪癖。

这里钉住三件事
--------------
1. 紧凑日期/时间能认出来，且解析出正确的交易
2. **账号、卡号绝不能被当成日期** —— 那比解析不出来危险得多，
   因为它是静默的错（金额会挂在错误的时间上，且不报错）
3. 「统计区间 / 合计」这类标题行不算失败，不进 skipped

第 2 条是这里最重要的一条。同一份账单里同时有：
    8 位日期 | 6 位时间 | 10 位账号 | 19 位卡号 | 带两位小数的金额
无脑匹配「8 位数字 = 日期」会把其中一部分账号读成日期。

用法：C:\\Users\\34759\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe tests\\test_compact_date.py
返回码 0 表示全部通过。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from importer.sources import (  # noqa: E402
    _compact_date_parts, _compact_time_parts, _extract_dates,
    _looks_like_summary_row, _parse_bank_rows_auto,
)

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n         得到 {got!r}\n         期望 {want!r}")
        FAILURES.append(label)


# --------------------------------------------------------------------------
# 按探针骨架 1:1 复刻的行结构（值是编的，形状是真的）
# --------------------------------------------------------------------------

HEADER = ["交易日期", "交易时间", "借贷标志", "交易金额", "账户余额",
          "对方户名", "摘要", "备注", "交易类型", "对方账号", "币种"]

ROWS = [
    ["账户名称：张某某"],
    ["交易区间：20260901-20260930", "合计金额：1234567.89"],
    HEADER,
    ["20260903", "借", "+5800.00", "12000.00", "--", "测试超市有限公司",
     "日常消费", "a123456789", "消费", "ACCT-TEST-001", "CNY"],
    ["20260905", "贷", "-25.50", "11974.50", "--", "测试餐饮店",
     "餐饮", "b123456789", "消费", "ACCT-TEST-001", "CNY"],
    ["20260910", "083000", "借", "-120.00", "11854.50", "测试加油站",
     "加油", "c123456789", "POS消费", "ACCT-TEST-001", "CNY"],
    ["20260915", "191500", "贷", "+3000.00", "14854.50", "测试公司",
     "工资", "d123456789", "转账", "ACCT-TEST-001", "CNY"],
]


def case_compact_date():
    print("\n[1] 紧凑日期 YYYYMMDD")
    check("20260903", _compact_date_parts("20260903"), (2026, 9, 3))
    check("20261231（年末）", _compact_date_parts("20261231"), (2026, 12, 31))
    check("20250903（往年）", _compact_date_parts("20250903"), (2025, 9, 3))
    check("六位 YYMMDD", _compact_date_parts("260903"), (2026, 9, 3))
    # 非法日期必须否掉，否则 `20261332` 会被当成 2026 年 13 月 32 日
    check("月 13 否", _compact_date_parts("20261332"), None)
    check("日 32 否", _compact_date_parts("20260932"), None)
    check("2026 不是闰年，2 月 29 日否", _compact_date_parts("20260229"), None)
    check("2024 是闰年，2 月 29 日收", _compact_date_parts("20240229"), (2024, 2, 29))
    check("七位数字否", _compact_date_parts("2026093"), None)
    check("带分隔符的不走这条路", _compact_date_parts("2026-09-03"), None)


def case_accounts_never_dates():
    print("\n[2] 账号/卡号绝不能被当成日期（静默的错比解析不出来危险）")
    # 这份账单里真实存在的几种数字长度
    for digits in (10, 16, 19, 20):
        check(f"{digits} 位数字不认成日期", _compact_date_parts("9" * digits), None)
    check("8 位全 9（月份 99 非法）", _compact_date_parts("99999999"), None)
    check("8 位但年份 1900", _compact_date_parts("19990903"), None)
    check("8 位但日期非法（12345678）", _compact_date_parts("12345678"), None)
    # 有分隔符的照旧走老路，两条路不能互相污染
    check("带分隔符仍认得出", _extract_dates("交易日期 2026-09-03"),
          [(2026, 9, 3)])
    check("有分隔符时不再试紧凑", _extract_dates("2026-09-03"), [(2026, 9, 3)])


def case_compact_time():
    print("\n[3] 紧凑时间 HHMMSS")
    check("083000", _compact_time_parts("083000"), (8, 30, 0))
    check("235959（末秒）", _compact_time_parts("235959"), (23, 59, 59))
    check("000000", _compact_time_parts("000000"), (0, 0, 0))
    check("24 点否", _compact_time_parts("240000"), None)
    check("60 分否", _compact_time_parts("126060"), None)
    # 探针里那个 6 位全 9 的格子不能被当成时间
    check("999999 否", _compact_time_parts("999999"), None)
    check("8 位不是时间", _compact_time_parts("20260903"), None)


def case_summary_rows():
    print("\n[4] 统计区间/合计不算失败")
    check("含「区间」", _looks_like_summary_row(["交易区间：20260901-20260930"]), True)
    check("含「合计」", _looks_like_summary_row(["合计金额：1234567.89"]), True)
    check("含「小计」", _looks_like_summary_row(["小计"]), True)
    # 紧凑格式的区间（两端都没分隔符）
    check("紧凑区间", _looks_like_summary_row(["20260901-20260930"]), True)
    # 真正的交易行不能被误判成汇总行
    check("真交易行（含借贷标志）", _looks_like_summary_row(
        ["20260903", "借", "+5800.00", "12000.00", "测试超市有限公司"]), False)
    check("含「消费」的交易行", _looks_like_summary_row(
        ["20260910", "083000", "借", "-120.00", "测试加油站", "加油"]), False)


def case_end_to_end():
    print("\n[5] 端到端：这份账单的行结构能解析出全部交易")
    result = _parse_bank_rows_auto(ROWS, "x.zip", source="bank_pdf")
    check("解析出 4 笔", len(result.txns), 4)
    # 标题行不该进 skipped —— 否则每月失败清单里多一条指向假问题的噪声
    check("跳过 0 行", len(result.skipped), 0)

    txns = result.txns
    check("日期对（2026-09-03）", txns[0].ts.strftime("%Y-%m-%d"), "2026-09-03")
    check("无时间的记 00:00:00", txns[0].ts.strftime("%H:%M:%S"), "00:00:00")
    check("紧凑时间对（08:30:00）", txns[2].ts.strftime("%H:%M:%S"), "08:30:00")
    check("紧凑时间对（19:15:00）", txns[3].ts.strftime("%H:%M:%S"), "19:15:00")
    check("金额取对（取带符号的，不是余额）", str(txns[0].amount), "5800.00")
    check("支出方向", txns[0].direction, "expense")
    check("收入方向", txns[1].direction, "income")
    check("支出方向（2）", txns[2].direction, "expense")
    check("收入方向（2）", txns[3].direction, "income")
    check("交易对方取到商户名", txns[0].counterparty, "测试超市有限公司")
    check("所有笔都有 source", sorted({t.source for t in txns}), ["bank_pdf"])
    # 月份必须落在 2026-09，否则月报会归错月
    check("月份归属正确",
          sorted({t.ts.strftime("%Y-%m") for t in txns}), ["2026-09"])


def case_never_regresses_separated():
    print("\n[6] 带分隔符的老格式不能被改坏")
    old = [
        ["2026-09-01", "12:31:05", "商户消费", "测试餐饮", "-28.50", "1234.56"],
        ["2026-09-05", "19:02:44", "工资", "测试公司", "+3200.00", "4434.56"],
    ]
    result = _parse_bank_rows_auto(old, "x.pdf", source="bank_pdf")
    check("老格式仍解析出 2 笔", len(result.txns), 2)
    check("老格式时间仍认得出", result.txns[0].ts.strftime("%H:%M:%S"), "12:31:05")
    check("老格式方向仍对", result.txns[0].direction, "expense")


def main() -> int:
    print()
    print("=" * 78)
    print("紧凑日期/时间（YYYYMMDD / HHMMSS）")
    print("=" * 78)
    case_compact_date()
    case_accounts_never_dates()
    case_compact_time()
    case_summary_rows()
    case_end_to_end()
    case_never_regresses_separated()

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
