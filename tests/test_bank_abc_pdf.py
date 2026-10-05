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

1. **两笔是「代持」：别人转给他 2400，他再转进对方账户，
   帮在银行工作的朋友刷业绩。** 不是自己转给自己。

   口径（用户明确选定）：**进的那笔算收入，出的那笔不算支出。**
   理由：钱确实进来了；他选的方案是「收入 +2400 / 支出 0」。

   为什么只能选这个口径：同一个「转账」摘要，数据上分不出三种情况 ——
     A. 朋友还款 / 家里给生活费 → 真金白银进来，该算收入
     B. 代持过账（钱过手）      → 一分钱没赚到，不该算收入
     C. 自己账户间搬            → 不是收入也不是消费
   三种在账单上长得一模一样。所以代码按 A 处理（与
   `categorize._apply_non_consumption` 同一原则：**只压支出侧**），
   代价是 B 情况下收入会略虚高 —— 而反过来让「借出去的钱」凭空消失更糟。
   想改这个行为，用 `setcat` 手工指定即可。

   支出那笔必须压：转进别人账户是「钱换个地方存」，
   算成消费会让月支出凭空多 2400，而报表看起来完全正常。

2. **0.00 的两行（结息/利息税）不是交易。** 那是账户计息的分录，
   一分钱没进出一出。留着会让「交易总笔数」不对，
   而那个数字常被用来判断「这个月数据全不全」。

3. **日期和时间印成两列，而且整份表没有方向列。**
   - 日期时间分列：`build_index` 遍历列在前、先命中先用，
     所以 `ts` 必然落到第 0 列「交易日期」上，时间被丢掉 ——
     表现是所有交易时间都变成 00:00:00。
   - 没有方向列：表头 9 列里既没有「借贷标志」也没有「收/支」，
     方向**只能**靠金额正负号。而 `parse_amount` 刻意返回绝对值
     （金额一律存正，方向另判），所以符号必须从原文单独取。

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
    print("\n[4] 代持转账：进的那笔算收入，出的那笔算中性")
    result = _parse_rows(ROWS, BANK_PDF_ALIASES, "bank_pdf", "x.zip", "pdf", "")
    by_item = {t.item: t for t in result.txns}
    check("转存在", "转存" in by_item, True)
    check("转账在", "转账" in by_item, True)

    # 用户明确的要求：「进的那笔算收入」。
    # 理由：钱确实进来了（别人转给他），哪怕是代持。
    # 口径与 categorize._apply_non_consumption 一致：**只压支出侧**。
    check("转存（进来）算收入", by_item["转存"].direction, "income")
    check("转账（出去）算中性", by_item["转账"].direction, "neutral")

    # 整个月报的三项数字
    income = sum(t.amount for t in result.txns if t.direction == "income")
    expense = sum(t.amount for t in result.txns if t.direction == "expense")
    neutral = sum(t.amount for t in result.txns if t.direction == "neutral")
    check("收入 2400", str(income), "2400.00")
    # 这条最关键：转出去那 2400 是「钱换个地方存」，
    # 算成消费的话月支出凭空多 2400，而报表看起来完全正常
    check("支出 0（不能虚增 2400）", str(expense), "0")
    check("中性 2400", str(neutral), "2400.00")
    # 金额本身不能丢
    check("金额没丢", str(by_item["转存"].amount), "2400.00")


def case_no_direction_column():
    print("\n[5] 没有方向列时靠金额正负号（这份账单就没方向列）")
    result = _parse_rows(ROWS, BANK_PDF_ALIASES, "bank_pdf", "x.zip", "pdf", "")
    # 「+2400.00」与「2400.00」在金额上完全一样，差别只在那个加号。
    # parse_amount 刻意返回绝对值，所以符号必须从原文单独取。
    check("进来的那笔靠加号判成收入",
          [t.direction for t in result.txns if t.amount == 2400][0], "income")
    # 普通消费（没有方向列、也没有转账关键词）必须仍然是支出，
    # 不能因为「没有方向列」就全判中性 —— 那样月支出永远是 0
    plain = [
        ["交易日期", "交易时间", "交易摘要", "交易金额", "本次余额", "对方信息",
         "日志号", "交易渠道", "交易附言"],
        ["20260930", "120000", "消费", "-35.20", "964.84", "XX便利店", "", "超级网银", ""],
    ]
    r2 = _parse_rows(plain, BANK_PDF_ALIASES, "bank_pdf", "x.zip", "pdf", "")
    check("普通消费仍是支出（不能全判中性）", r2.txns[0].direction, "expense")


def case_columns_not_guessed():
    print("\n[6] 有表头时按列取，不靠猜长度")
    result = _parse_rows(ROWS, BANK_PDF_ALIASES, "bank_pdf", "x.zip", "pdf", "")
    t = [x for x in result.txns if x.item == "转账"][0]
    check("摘要取对（不是 XX公积金中心 / 掌上银行）", t.item, "转账")
    check("对方取对（不是最长的中文）", t.counterparty, "XX公积金中心")
    check("渠道取对", t.method, "掌上银行")



def case_amount_not_balance():
    print("\n[7] 金额列不能落在余额列上（认错不报错、报表也看不出来）")
    from importer.sniff import build_index, header_names
    from importer.sources import _fix_amount_vs_balance_column

    h = header_names(["交易日期", "交易时间", "交易摘要", "交易对方",
                      "交易金额", "账户余额"])
    # 直接构造「认错了」的 index，而不是指望 build_index 认错 ——
    # 后者依赖别名表顺序，改一次别名就变，测试会莫名其妙地失效。
    # 这里要验的是**互斥函数本身**能不能把错的拉回来。
    wrong = {"amount": 5, "counterparty": 3}     # 5 = 账户余额
    fixed = _fix_amount_vs_balance_column(wrong, h)
    check("修正后取回交易金额", h[fixed["amount"]], "交易金额")
    check("修正后下标变了", fixed["amount"] != wrong["amount"], True)
    check("其他字段不动", fixed["counterparty"], 3)

    # 余额类列名**绝不能**出现在 amount 的别名里 ——
    # 靠名字分不清「余额有时是金额列」，所以只能靠互斥校验
    from importer.sources import BALANCE_COLUMN_NAMES
    check("余额列名不在 amount 别名里",
          any(b in n for b in BALANCE_COLUMN_NAMES
              for n in BANK_PDF_ALIASES["amount"]), False)

    # 表头里只有余额列、没有金额列时，**不要瞎改** ——
    # 有些表格金额列就叫别的名字，强行换成「唯一像金额的」反而更错
    only_balance = {"amount": 1}
    header_only = header_names(["交易日期", "账户余额"])
    kept = _fix_amount_vs_balance_column(only_balance, header_only)
    check("只有余额列时保持原样（不瞎猜）", kept["amount"], 1)

    # 认对了的时候不能被改坏
    h2 = header_names(ROWS[2])
    i2 = build_index(h2, BANK_PDF_ALIASES)
    fixed2 = _fix_amount_vs_balance_column(i2, h2)
    check("本来认对的不会被改", h2[fixed2["amount"]], "交易金额")


def case_move_to_done_retry():
    print("\n[8] 源文件被占用时不能崩整轮（实测崩过）")
    from importer import monthly
    # Windows 上刚被 7z 读过的 zip，句柄不立即释放，
    # 紧接着 shutil.move 抛 WinError 32 → 整轮批处理崩溃，
    # 而此时前面几十笔已经写进账本并落盘了。
    import inspect
    src = inspect.getsource(monthly._move_to_done)
    check("有重试逻辑", "for attempt in range(5)" in src, True)
    check("有等待", "time.sleep" in src, True)
    check("重试用尽后给出可操作提示", "数据已经入库" in src, True)
    check("提示文件仍在原处", "文件仍在原处" in src, True)



def _make_pdf_with_gaps(dest: str) -> str:
    """造一份「有空单元格」的 PDF —— 复刻真实踩到的那种形态。

    为什么必须造：真实那份账单的问题是**列对齐全塌了**
    （PDF 里空单元格不占格，后面的列全部左移），而
    `fixtures/sample_bank.pdf` 每行都满格，验不出这个问题。
    """
    import pymupdf
    header = ["TXN_DATE", "TXN_TIME", "SUMMARY", "AMOUNT", "BALANCE",
              "CP", "LOG", "CHANNEL", "MEMO", "A", "B"]
    body = [
        # 时间列为空 -> 少了 1 格
        ["20260921", "", "INT", "+0.00", "0.04", "--", "", "PERIODIC", "", "x", "y"],
        # 满格
        ["20260928", "205056", "TRANSFER", "+2400.00", "2400.04",
         "XX", "", "SUPERNET", "", "x", "y"],
    ]
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    x = 40
    for name in header:
        page.insert_text((x, 100), name, fontsize=7)
        x += 70
    y = 120
    for row in body:
        x = 40
        for value in row:
            if value:
                page.insert_text((x, y), value, fontsize=7)
            x += 70
        y += 18
    doc.save(dest)
    doc.close()
    return dest


def case_column_alignment():
    print("\n[9] 空单元格必须补成空串，不能让后面的列左移（实测错值的根因）")
    import tempfile
    from importer.bank_pdf import extract_rows

    tmp = tempfile.mkdtemp()
    path = _make_pdf_with_gaps(os.path.join(tmp, "gaps.pdf"))
    rows, _ = extract_rows(path)

    widths = [len(r) for r in rows]
    check("所有行格数一致（对齐成功）", len(set(widths)), 1)
    check("格数等于表头列数", widths[0], 11)
    # 关键两行：空的那格必须在原位
    check("有空格那行：时间列是空串（不是摘要）", rows[1][1], "")
    check("有空格那行：摘要还在第 2 列", rows[1][2], "INT")
    check("有空格那行：金额还在第 3 列", rows[1][3], "+0.00")
    check("满格那行：时间列是真实时间", rows[2][1], "205056")
    check("满格那行：摘要是 TRANSFER", rows[2][2], "TRANSFER")
    # 这两条是同一个下标在不同行装不同东西的对照
    check("下标 1 在两行装的是不同的东西（正是原 bug）",
          rows[1][1] != rows[2][1], True)
    # 0 元那一行照样在（对齐不负责过滤，过滤是解析层的事）
    check("0 元那行的金额没丢", rows[1][3], "+0.00")


def main() -> int:
    print()
    print("=" * 78)
    print("农行活期明细：表头链路 / 中性判定 / 0 元分录")
    print("=" * 78)
    case_header_mapping()
    case_date_time_merged()
    case_zero_amount_not_a_txn()
    case_transfer_is_neutral()
    case_no_direction_column()
    case_columns_not_guessed()
    case_amount_not_balance()
    case_move_to_done_retry()
    case_column_alignment()

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
