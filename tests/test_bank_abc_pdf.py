"""农行活期明细：表头链路、中性判定、0 元分录。

真实事故
--------
用户给了 PDF 内容截图（他自己涂黑了姓名/账号/流水号）。
表头是 9 列：
    交易日期 | 交易时间 | 交易摘要 | 交易金额 | 本次余额
    | 对方信息 | 日志号 | 交易渠道 | 交易附言
4 行流水：
    20260921        结息   +0.00    0.04    --  个人活期结息
    20260921        利息税 +0.00    0.04    --  个人活期结息
    20260928 205056 转存   +2400.00 2400.04 XX公积金 超级网银
    20260928 210127 转账   -2400.00 0.04   XX公积金 掌上银行 石桥环保公积金

从截图能看出三件必须处理的事，缺一件都会**静默写坏数据**：

1. **「转存 +2400」和「转账 -2400」是自己的钱换个账户存，不是消费。**
   不判中性的话，月支出凭空多 2400，而报表看起来完全正常。
   致命的细节：那一行里还有「本次余额 2400.04」和「对方信息 XX公积金」，
   整行文本里藏着「存入」「转出」这类词 —— 对**整行**扫关键词必然误判。

2. **0.00 的两行（结息/利息税）不是交易。** 那是账户计息的分录，
   一分钱没进出一出。留着会让「交易总笔数」不对，
   而那个数字常被用来判断「这个月数据全不全」。

3. **日期和时间印成两列。** `build_index` 遍历列在前、先命中先用，
   所以 `ts` 必然落到第 0 列「交易日期」上，时间被丢掉 ——
   表现是所有交易时间都变成 00:00:00。

还有一个「看起来像 bug 其实是取舍」的点：摘要/对方/渠道三列都是短中文，
靠长度或位置猜摘要**必然猜错**（实测在「取最长 ≤12 字」和「取最长 ≤6 字」
两个版本里分别挑中了「XX公积金中心」和「掌上银行」）。
所以有表头时必须**按列取**，没表头时才退回猜。

用法：C:\\Users\\34759\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe tests\\test_bank_abc_pdf.py
返回码 0 表示全部通过。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from importer.sources import (  # noqa: E402
    BANK_PDF_ALIASES, _combine_date_time, _parse_rows,
)
from importer.sniff import build_index, find_header_row, header_names  # noqa: E402

FAILURES: list[str] = []

# 1:1 复刻截图里的行结构。**所有值都是编的**，形状是真的。
ROWS = [
    ["中国农业银行账户活期交易明细清单"],
    ["户名：张某某", "币种：人民币", "起止日期：20260901-20260930",
     "账户：ACCT-TEST-001", "密码标识：本币", "电子流水号：LOG-TEST-001"],
    ["交易日期", "交易时间", "交易摘要", "交易金额", "本次余额",
     "对方信息", "日志号", "交易渠道", "交易附言"],
    ["20260921", "", "结息", "+0.00", "0.04", "--", "", "", "个人活期结息"],
    ["20260921", "", "利息税", "+0.00", "0.04", "--", "", "", "个人活期结息"],
    ["20260928", "205056", "转存", "+2400.00", "2400.04",
     "XX公积金中心", "", "超级网银", ""],
    ["20260928", "210127", "转账", "-2400.00", "0.04",
     "XX公积金中心", "", "掌上银行", "石桥环保公积金"],
]


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n         得到 {got!r}\n         期望 {want!r}")
        FAILURES.append(label)


def case_header_mapping():
    print("\n[1] 表头认出来 + 列映射对")
    hr = find_header_row(ROWS)
    check("表头行号是 2", hr, 2)
    header = header_names(ROWS[hr])
    index = build_index(header, BANK_PDF_ALIASES)
    check("摘要列认到「交易摘要」", header[index["category_raw"]], "交易摘要")
    check("金额列认到「交易金额」", header[index["amount"]], "交易金额")
    # 漏了「对方信息」这一条，交易对方就会退回「取最长中文」，
    # 商户名会变成带机构全称的串，分类规则跟着对不上
    check("对方列认到「对方信息」", header[index["counterparty"]], "对方信息")
    check("渠道列认到「交易渠道」", header[index["method"]], "交易渠道")
    check("日期列与时间列分开", (index["date"], index["time"]), (0, 1))


def case_date_time_merged():
    print("\n[2] 日期时间分列要合起来（否则时间全丢成 00:00:00）")
    index = build_index(header_names(ROWS[2]), BANK_PDF_ALIASES)
    # 有时间列 → 合起来
    got = _combine_date_time(ROWS[5], index)
    check("带时间的行", got.strftime("%Y-%m-%d %H:%M:%S"), "2026-09-28 20:50:56")
    got2 = _combine_date_time(ROWS[6], index)
    check("另一行", got2.strftime("%Y-%m-%d %H:%M:%S"), "2026-09-28 21:01:27")
    # 时间列为空 → 只用日期
    got3 = _combine_date_time(ROWS[3], index)
    check("时间列为空则记 00:00:00", got3.strftime("%Y-%m-%d %H:%M:%S"),
          "2026-09-21 00:00:00")


def case_zero_amount_not_a_txn():
    print("\n[3] 0.00 的行不是交易，也不算失败")
    result = _parse_rows(ROWS, BANK_PDF_ALIASES, "bank_pdf", "x.zip", "pdf", "")
    check("只解析出 2 笔", len(result.txns), 2)
    # 「结息 +0.00」「利息税 +0.00」都不该出现
    check("没有 0 元交易", all(t.amount > 0 for t in result.txns), True)
    # 也不能记成失败：那是账户计息分录，不是格式问题
    check("跳过 0 行（0 元不算失败）", len(result.skipped), 0)


def case_transfer_is_neutral():
    print("\n[4] 账户间转账判中性（否则月支出虚高 2400）")
    result = _parse_rows(ROWS, BANK_PDF_ALIASES, "bank_pdf", "x.zip", "pdf", "")
    by_item = {t.item: t for t in result.txns}
    check("转存在", "转存" in by_item, True)
    check("转账在", "转账" in by_item, True)
    check("转存方向是中性", by_item["转存"].direction, "neutral")
    check("转账方向是中性", by_item["转账"].direction, "neutral")
    # 这是整份文件最关键的一个断言
    expense = sum(t.amount for t in result.txns if t.direction == "expense")
    check("支出合计为 0（2400 是自己钱换地方存）", str(expense), "0")
    # 金额本身不能丢
    check("金额没丢", str(by_item["转存"].amount), "2400.00")


def case_columns_not_guessed():
    print("\n[5] 有表头时按列取，不靠猜长度")
    result = _parse_rows(ROWS, BANK_PDF_ALIASES, "bank_pdf", "x.zip", "pdf", "")
    t = [x for x in result.txns if x.item == "转账"][0]
    check("摘要取对（不是 XX公积金中心 / 掌上银行）", t.item, "转账")
    check("对方取对（不是最长的中文）", t.counterparty, "XX公积金中心")
    check("渠道取对", t.method, "掌上银行")


def main() -> int:
    print()
    print("=" * 78)
    print("农行活期明细：表头链路 / 中性判定 / 0 元分录")
    print("=" * 78)
    case_header_mapping()
    case_date_time_merged()
    case_zero_amount_not_a_txn()
    case_transfer_is_neutral()
    case_columns_not_guessed()

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
