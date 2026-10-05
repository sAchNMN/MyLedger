"""换格式的账单能不能解析：表头叫法千差万别时的三级兜底。

问题背景
--------
2026-10-05 用户问：「如果未来出现格式不一样的 PDF、或者内容更多的 PDF、
或者格式不一样的 excel / 内容更多的 excel，会不会解析失败？」

实测发现两类真失败：
  1. **表头认不出来** → 报 `missing_header`，而文件**明明有表头**。
     原因是 `find_header_row` 只认 6 个锚点词
     （交易时间/交易号/交易单号/交易订单号/金额/收-支）+ 至少 5 个字段，
     于是这些表头全部被判成「文件里没有表头」：
         日期 | 时间 | 摘要 | 发生额 | 余额 | 对方户名 | 流水号 | 渠道 | 附言
         交易日期 | 交易时间 | 交易摘要 | 交易金额      （只有 4 列）
         交易日期 | 交易金额 | 对方户名               （只有 3 列）

  2. **认出了表头但必需列对不上** → 报 `missing_required_column`，
     整份文件 0 笔。**为什么不退回自动识别？** 那份文件里明明有正常交易。

这两个都是「报错指向的原因和真实原因不是一回事」——
前者说「文件里没有表头」，后者说「表头缺列」，实际都只是「没见过这种叫法」。

修法（三层）
------------
  1. `find_header_row` 改成三级判据：锚点词 → 别名表 → 弱信号词+形状。
  2. `_parse_rows` 认不出表头 → 退回无表头自动识别（`parse_bank_pdf` 里
     原来那次兜底删掉了，挪进 `_parse_rows`，三条链路都能吃到）。
  3. `_parse_rows` 认出表头但缺必需列 → **也**退回自动识别。

误判防护
--------
放宽判据最怕的是把**数据行**当成表头。实测拦住了这些：
    户名：张三 | 币种：人民币 | 起止日期：20260901-20260930 | 账户：… | 电子流水号：…
    中国农业银行账户活期交易明细清单
    ********** | ********** | **********
    共 119 笔记录
    收入：27笔 1234元

第一条是最险的：按「子串包含」判，「起止日期：…」含「日期」、
「电子流水号：…」含「流水号」，两个就够「>= 2」→ 整行被当表头，
而真表头在下一行反而被跳过。修法是**带分隔符的「键：值」形态直接否掉**。

用法：C:\\Users\\34759\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe tests\\test_format_variants.py
返回码 0 表示全部通过。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from importer.sniff import find_header_row  # noqa: E402
from importer.sources import (  # noqa: E402
    ALIPAY_ALIASES, BANK_PDF_ALIASES, WECHAT_ALIASES, _parse_rows,
)

FAILURES: list[str] = []

# 各种叫法的银行表头 + 一行数据（值全是编的）
BANK_CASES = [
    ("农行实测那份（9 列）",
     ["交易日期", "交易时间", "交易摘要", "交易金额", "本次余额",
      "对方信息", "日志号", "交易渠道", "交易附言"],
     ["20260928", "205056", "转账", "-2400.00", "2400.04", "XX中心", "L1", "超级网银", ""]),
    ("极简叫法（9 列，两个字为主）",
     ["日期", "时间", "摘要", "发生额", "余额", "对方户名", "流水号", "渠道", "附言"],
     ["20260928", "205056", "转账", "-2400.00", "2400.04", "XX中心", "L1", "超级网银", ""]),
    ("复式记账叫法（借方/贷方）",
     ["记账日期", "交易时间", "交易类型", "借方发生额", "贷方发生额",
      "对方账户名", "凭证号", "业务类型", "用途"],
     ["20260928", "205056", "转账", "-2400.00", "2400.04", "XX中心", "L1", "超级网银", ""]),
    ("只有 3 列（内容很少的 PDF）",
     ["交易日期", "交易金额", "对方户名"],
     ["20260928", "-2400.00", "XX中心"]),
    ("只有 4 列（列名全是 2-4 个字）",
     ["交易日期", "交易时间", "交易摘要", "交易金额"],
     ["20260928", "205056", "转账", "-2400.00"]),
    ("完全另一套叫法",
     ["交易日", "交易时刻", "摘要", "金额", "余额", "对方", "凭证", "渠道"],
     ["20260928", "205056", "转账", "-2400.00", "2400.04", "XX", "L1", "网银"]),
    ("列多且带未知列（内容更多的 PDF）",
     ["交易日期", "交易时间", "交易摘要", "交易金额", "本次余额", "对方信息",
      "日志号", "交易渠道", "交易附言", "币种", "网点", "柜员", "附加信息"],
     ["20260928", "205056", "转账", "-2400.00", "2400.04", "XX中心",
      "L1", "超级网银", "", "CNY", "001", "A02", "无"]),
]

# 不能被误判成表头的行
NOISE_ROWS = [
    ["户名：张三", "币种：人民币", "起止日期：20260901-20260930",
     "账户：ACCT-TEST-001", "电子流水号：LOG-TEST-001"],
    ["中国农业银行账户活期交易明细清单"],
    ["**********", "**********", "**********", "**********"],
    ["共 119 笔记录"],
    ["收入：27笔 1234元"],
    ["说明：本表由系统自动生成，请勿手工修改"],
    ["打印时间：2026-10-05 15:00:00"],
]


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n         得到 {got!r}\n         期望 {want!r}")
        FAILURES.append(label)


def case_all_header_variants():
    print("\n[1] 各种表头叫法都要能解析（换银行不能就废）")
    for name, header, row in BANK_CASES:
        result = _parse_rows([header, row], BANK_PDF_ALIASES,
                             "bank_pdf", "x.pdf", "pdf", "")
        check(f"{name}（{len(header)} 列）", len(result.txns) >= 1, True)
    # 金额不能丢
    r = _parse_rows([BANK_CASES[3][1], BANK_CASES[3][2]], BANK_PDF_ALIASES,
                    "bank_pdf", "x.pdf", "pdf", "")
    check("3 列那份的金额取对了", str(r.txns[0].amount) if r.txns else "没解析出来", "2400.00")


def case_no_misjudge():
    print("\n[2] 数据行/说明行不能被误判成表头")
    for row in NOISE_ROWS:
        pos = find_header_row([row], aliases=BANK_PDF_ALIASES)
        check(f"不误判：{row[0][:24]}", pos, None)
    # 最险的那条：整行「键：值」形态
    kv = NOISE_ROWS[0]
    check("键：值 形态全被否掉",
          find_header_row([kv], aliases=BANK_PDF_ALIASES), None)
    # 真表头在同一批里仍要被认出来（误判会连带把真表头挤掉）
    # 注意把「账户信息」那几行**拆成多行**放前面 —— 真实 PDF 里
    # 它们确实是各自独立的一行，不是打包在一个格子里。
    mixed = [
        ["户名：张三"],
        ["币种：人民币"],
        ["起止日期：20260901-20260930"],
        ["账户：ACCT-TEST-001"],
        ["电子流水号：LOG-TEST-001"],
        ["交易日期", "交易时间", "交易金额"],
    ]
    check("真表头仍被认出（没被上面几行挤掉）",
          find_header_row(mixed, aliases=BANK_PDF_ALIASES), 5)


def case_fallback_not_failure():
    print("\n[3] 认不出表头/必需列要退回自动识别，而不是报「文件坏了」")
    # 一份完全没有列名行的纯流水（部分银行导出就是这样）
    headerless = [
        ["20260928", "205056", "转账", "-2400.00", "2400.04", "XX中心"],
        ["20260929", "101010", "消费", "-35.20", "1204.36", "XX便利店"],
    ]
    r = _parse_rows(headerless, BANK_PDF_ALIASES, "bank_pdf", "x.pdf", "pdf", "")
    check("无表头也能解析出 2 笔", len(r.txns), 2)
    # 认不出且真的没数据时，skipped 要说清「两条路都试过了」
    empty = [["这是一份空文件"], []]
    r2 = _parse_rows(empty, BANK_PDF_ALIASES, "bank_pdf", "x.pdf", "pdf", "")
    check("真没数据时 0 笔", len(r2.txns), 0)
    check("skipped 非空（有诊断线索）", len(r2.skipped) >= 1, True)
    joined = " ".join(str(s.get("reason", "")) for s in r2.skipped)
    check("提到了「列名没见过」而不是「文件里没有表头」",
          "没见过" in joined or "第一次见" in joined, True)


def case_others_not_broken():
    print("\n[4] 支付宝/微信不能被这次改动带坏")
    abase = ["交易时间", "交易分类", "交易对方", "对方账号", "商品说明", "收/支",
             "金额", "收/付款方式", "交易状态", "交易订单号", "商家订单号", "备注"]
    arow = ["2026-09-01 12:00:00", "餐饮美食", "某店", "acc1", "午餐", "支出",
            "35.20", "余额宝", "交易成功", "T1", "M1", ""]
    check("支付宝原样", len(_parse_rows([abase, arow], ALIPAY_ALIASES,
                                        "alipay", "a.csv", "csv", "").txns), 1)
    # 列顺序全换（内容形态变了但列名没变）
    check("支付宝列顺序全换",
          len(_parse_rows([list(reversed(abase)), list(reversed(arow))],
                          ALIPAY_ALIASES, "alipay", "a.csv", "csv", "").txns), 1)
    # 前面几行是说明文字（真实导出就是这样）
    check("支付宝前面有说明行",
          len(_parse_rows([
              ["支付宝交易记录明细查询"], ["账号：TEST"], ["---------------------"],
              abase, arow], ALIPAY_ALIASES, "alipay", "a.csv", "csv", "").txns), 1)
    # 多几列未知列
    check("支付宝多 3 列",
          len(_parse_rows([abase + ["X1", "X2", "X3"], arow + ["a", "b", "c"]],
                          ALIPAY_ALIASES, "alipay", "a.csv", "csv", "").txns), 1)
    wbase = ["交易时间", "交易类型", "交易对方", "商品", "收/支", "金额",
             "交易方式", "当前状态", "交易单号", "备注"]
    wrow = ["2026-09-01 12:00:00", "商户消费", "某店", "午餐", "支出",
            "35.20", "零钱", "支付成功", "W1", ""]
    check("微信原样", len(_parse_rows([wbase, wrow], WECHAT_ALIASES,
                                      "wechat", "w.xlsx", "xlsx", "").txns), 1)
    check("微信多 5 列",
          len(_parse_rows([wbase + ["A", "B", "C", "D", "E"],
                           wrow + ["1", "2", "3", "4", "5"]],
                          WECHAT_ALIASES, "wechat", "w.xlsx", "xlsx", "").txns), 1)


def case_more_rows_than_expected():
    print("\n[5] 「内容更多」—— 几十上百行、多页、跨月都不能崩")
    header = ["交易日期", "交易时间", "交易摘要", "交易金额", "本次余额",
              "对方信息", "日志号", "交易渠道", "交易附言"]
    body = []
    for day in range(1, 29):
        body.append([f"202609{day:02d}", f"{day:02d}0000", "消费",
                     "-35.20", f"{1000 - day * 3}.56", "XX便利店", "",
                     "超级网银", ""])
    result = _parse_rows([header] + body, BANK_PDF_ALIASES,
                         "bank_pdf", "x.pdf", "pdf", "")
    check("28 行全部解析出来", len(result.txns), 28)
    check("首笔日期对", result.txns[0].ts.strftime("%Y-%m-%d"), "2026-09-01")
    check("末笔日期对", result.txns[-1].ts.strftime("%Y-%m-%d"), "2026-09-28")
    check("首笔时间对", result.txns[0].ts.strftime("%H:%M:%S"), "01:00:00")
    # 前面有标题、账户信息、说明行 —— 真实 PDF 就是这样
    noisy = [
        ["中国某某银行账户活期交易明细清单"],
        ["户名：TEST", "币种：人民币", "起止日期：20260901-20260928",
         "账户：ACCT-TEST-001", "电子流水号：LOG-TEST-001"],
        ["交易日期：20260901-20260928", "合计金额：1234.56"],
        header,
    ] + body
    r2 = _parse_rows(noisy, BANK_PDF_ALIASES, "bank_pdf", "x.pdf", "pdf", "")
    check("带标题/账户行/区间行时仍解析 28 笔", len(r2.txns), 28)


def case_semantic_fallback():
    print("\n[6] 列名不在别名表里时按语义兜底（否则「大部分对、一项空」）")
    from importer.sniff import build_index
    from importer.sources import BANK_PDF_ALIASES

    # 实测事故：农行活期明细表头第 5 列的叫法没进别名表，
    # 于是 counterparty **整个字段消失** —— 而日期/金额/方向/渠道全对，
    # 报表看起来完全正常，只有商户名是空的。**这种错最难查。**
    # 实测那次折腾了好几轮：字坐标验过、列数验过、列映射验过，
    # 最后才从 `index` 字典里发现少了 counterparty 这一项。
    variants = [
        ["交易日期", "交易时间", "交易摘要", "交易金额", "本次余额",
         "对方信息", "日志号", "交易渠道", "交易附言"],
        ["交易日期", "交易时间", "交易摘要", "交易金额", "本次余额",
         "对方名称", "日志号", "交易渠道", "交易附言"],
        ["交易日期", "交易时间", "交易摘要", "交易金额", "本次余额",
         "对方账户名称", "日志号", "交易渠道", "交易附言"],
        ["交易日期", "交易时间", "交易摘要", "交易金额", "本次余额",
         "对手方信息", "日志号", "交易渠道", "交易附言"],
        ["交易日期", "交易时间", "交易摘要", "交易金额", "本次余额",
         "收款方名称", "日志号", "交易渠道", "交易附言"],
        ["交易日期", "交易时间", "交易摘要", "交易金额", "本次余额",
         "对手户名", "日志号", "交易渠道", "交易附言"],
    ]
    for header in variants:
        idx = build_index(header, BANK_PDF_ALIASES)
        check(f"认出第 5 列（表头写「{header[5]}」）", idx.get("counterparty"), 5)

    # 端到端：列有内容时必须真的取到
    header = variants[0]
    row = ["20260928", "205056", "转账", "-2400.00", "2400.04",
           "XX公积金中心", "", "LOG1", "掌上银行", "石桥环保公积金"]
    result = _parse_rows([header, row], BANK_PDF_ALIASES, "bank_pdf", "x", "pdf", "")
    check("解析出 1 笔", len(result.txns), 1)
    check("对方取到值（不是空串）", result.txns[0].counterparty, "XX公积金中心")

    # 兜底**不能**抢掉已经认对的列
    idx = build_index(header, BANK_PDF_ALIASES)
    check("amount 仍在第 3 列（没被兜底抢走）", idx["amount"], 3)
    check("date 仍在第 0 列", idx["date"], 0)
    # 注意：不同字段**允许**共用同一列 ——「交易摘要」同时是
    # category_raw / item / note 的来源，这是设计如此（见 BANK_PDF_ALIASES
    # 顶部注释：靠 dict 顺序决定谁先命中）。所以这里只验「兜底没抢走
    # 金额列和日期列」，不去断言列与列一一对应。


def case_fallback_not_for_strict_fields():
    print("\n[7] 兜底不用于 ts/amount（那两个有严格校验，猜错会产出脏数据）")
    import importer.sniff as sniff
    check("语义兜底表里没有 ts", "ts" in sniff._SEMANTIC_FALLBACK, False)
    check("语义兜底表里没有 amount", "amount" in sniff._SEMANTIC_FALLBACK, False)
    check("语义兜底表里没有 direction", "direction" in sniff._SEMANTIC_FALLBACK, False)
    check("语义兜底表里没有 txn_id", "txn_id" in sniff._SEMANTIC_FALLBACK, False)


def main() -> int:
    print()
    print("=" * 78)
    print("换格式的账单：表头千差万别时的三级兜底")
    print("=" * 78)
    case_all_header_variants()
    case_no_misjudge()
    case_fallback_not_failure()
    case_others_not_broken()
    case_more_rows_than_expected()
    case_semantic_fallback()
    case_fallback_not_for_strict_fields()

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
