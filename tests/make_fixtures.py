"""生成合成测试账单。

刻意不包含任何真实数据——fixtures/ 是唯一允许提交进仓库的「数据」目录。
这里的商户名、金额、单号全是编的，但表头、说明行数、编码格式
都和支付宝/微信真实导出一模一样，这样解析器才测得出问题。

用法：python tests/make_fixtures.py
"""

from __future__ import annotations

import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(os.path.dirname(HERE), "fixtures")

ALIPAY_HEADER = (
    "交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,"
    "收/付款方式,交易状态,交易订单号,商家订单号,备注"
)

WECHAT_HEADER = (
    "交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,"
    "当前状态,交易单号,商户单号,备注"
)

# ---------------------------------------------------------------------------
# 支付宝样本
#
# 第 1 笔和第 2 笔刻意做成「同一时间、同一金额、同一对方」，
# 用来测内容指纹去重。第 4 笔是收入，第 3 笔是不计收支。
# 最后三笔覆盖平台分类与交易状态这两条新判定路径（见 model.status_verdict、
# importer/platformmap.py）—— 全用「交易成功」的样本会让它们一次都跑不到。
#
# ⚠ 增删行必须同步改下面 preamble 里的「共N笔」和收入/支出小计。
# 导出自述的笔数不是装饰：pipeline 拿它和实际解析数对账，
# 对不上就中止导入并报警（曾经真的靠这道校验发现过一份空账单）。
# ---------------------------------------------------------------------------
ALIPAY_PREAMBLE = [
    "------------------------------------------------------------------------------------",
    "导出信息：",
    "姓名：测试用户",
    "支付宝账户：test@example.com",
    "起始时间：[2026-09-01 00:00:00]    终止时间：[2026-09-30 23:59:59]",
    "导出交易类型：[全部]",
    "导出时间：[2026-10-04 21:37:46]",
    "共8笔记录",
    "收入：1笔 100.00元",
    "支出：6笔 529.90元",
    "不计收支：1笔 500.00元",
    "特别提示：",
    "1.本回单内容可表明支付宝受理了相应支付申请，因系统原因或通讯故障等偶发因素导致本回单与实际交易结果不符时，以实际交易情况为准；",
    "2.请勿将本回单作为收款方发货的凭据使用，请查证账户实际到账情况后再进行发货操作；",
]

ALIPAY_ROWS = [
    "2026-09-03 12:31:05,餐饮美食,某某餐饮管理有限公司,abc***@example.com,午餐,支出,28.50,余额宝,交易成功,2026090322001234567890,MO20260903001,",
    "2026-09-03 12:31:05,餐饮美食,某某餐饮管理有限公司,abc***@example.com,午餐,支出,28.50,余额宝,交易成功,2026090322009999999999,MO20260903002,内容指纹与上一笔相同但单号不同",
    "2026-09-10 08:02:11,交通出行,某出行科技有限公司,def***@example.com,打车,支出,19.90,花呗,交易成功,2026091022009876543210,MO20260910003,",
    "2026-09-15 20:11:00,转账,张三,ghi***@example.com,转账,不计收支,500.00,余额,交易成功,2026091522001111111111,MO20260915004,",
    "2026-09-20 09:00:00,其他,李四,jkl***@example.com,退款,收入,100.00,余额,交易成功,2026092022002222222222,MO20260920005,",
    # ↓ 下面三笔专门覆盖「状态」这条判定路径（见 model.status_verdict）
    #
    # 第 6 笔：交易关闭 —— 付了但没成交（免密支付失败、订单取消）。
    #         商户名和商品都看不出问题，只有状态列能揭穿它。
    #         如果不判状态，这 88 元会堂堂皇皇算进 9 月支出。
    "2026-09-12 19:20:00,餐饮美食,某某餐饮管理有限公司,mno***@example.com,晚餐,支出,88.00,余额,交易关闭,2026091222003333333333,MO20260912006,",
    # 第 7 笔：已全额退款 —— 钱回来了。分类是「餐饮美食」，
    #         规则和平台分类都会判成餐饮，只有状态能把它从支出里剔掉。
    "2026-09-18 12:00:00,餐饮美食,某某餐饮管理有限公司,pqr***@example.com,午餐,支出,45.00,余额,已全额退款,2026091822004444444444,MO20260918007,",
    # 第 8 笔：商户名认不出、关键词也命中不了，唯一线索就是平台分类
    #         「生活服务」→ 生活缴费。没有平台分类这一层它就是未分类。
    "2026-09-22 10:05:00,生活服务,某某城市服务有限公司,stu***@example.com,物业费,支出,320.00,余额,交易成功,2026092222005555555555,MO20260922008,",
]

# ---------------------------------------------------------------------------
# 支付宝「交易关闭 / 已退款」专项样本
#
# 单独一个文件，不混进主样本：
# 主样本的「共 N 笔」自述、收入/支出小计都被其它用例依赖，
# 往里加行就得同步改一堆数字，很容易改漏一处。
# ---------------------------------------------------------------------------

ALIPAY_STATUS_PREAMBLE = [
    "------------------------------------------------------------------------------------",
    "导出信息：",
    "姓名：测试用户",
    "支付宝账户：test@example.com",
    "起始时间：[2026-09-01 00:00:00]    终止时间：[2026-09-30 23:59:59]",
    "导出交易类型：[全部]",
    "导出时间：[2026-10-04 21:37:46]",
    "共4笔记录",
    "收入：0笔 0.00元",
    "支出：3笔 421.50元",
    "不计收支：1笔 500.00元",
    "特别提示：",
    "1.本回单内容可表明支付宝受理了相应支付申请；",
]

ALIPAY_STATUS_ROWS = [
    # 交易成功 —— 唯一一笔真花出去且真该算消费的
    "2026-09-05 12:00:00,餐饮美食,某某餐饮管理有限公司,aaa***@example.com,午餐,支出,21.50,余额,交易成功,2026090522000000000001,MO20260905001,",
    # 交易关闭 —— 钱没扣
    "2026-09-06 12:00:00,餐饮美食,某某餐饮管理有限公司,aaa***@example.com,午餐,支出,30.00,余额,交易关闭,2026090622000000000002,MO20260906002,",
    # 交易关闭 + 零钱充值（不计收支，本来就中性）
    "2026-09-07 12:00:00,余额宝,某某余额宝,bbb***@example.com,转入,不计收支,500.00,余额,交易关闭,2026090722000000000003,MO20260907003,",
    # 已全额退款 —— 钱回来了，收入侧的退款记录另有其行
    "2026-09-08 12:00:00,服饰装扮,某某服饰有限公司,ccc***@example.com,外套,支出,370.00,花呗,已全额退款,2026090822000000000004,MO20260908004,",
]

# ---------------------------------------------------------------------------
# 微信样本
#
# 第 1 笔与支付宝第 1 笔时间(到分)、金额、对方完全一致，
# 但单号不同——模拟「同一笔支出在支付宝和微信都记了一次」。
# 第 2 笔是独立交易。
# 第 3 笔金额写成「¥88.00」带货币符号，测金额解析。
# ---------------------------------------------------------------------------
WECHAT_PREAMBLE = [
    "微信支付账单明细",
    "微信昵称：[测试用户]",
    "起始时间：[2026-09-01 00:00:00] 终止时间：[2026-09-30 23:59:59]",
    "导出类型：[全部账单]",
    "导出时间：[2026-10-04 21:36:57]",
    "共3笔记录",
    "收入：0笔 0.00元",
    "支出：3笔 151.70元",
    "中性交易：0笔 0.00元",
    "注：",
    "1. 充值/提现/理财通购买/零钱通存取/信用卡还款等交易，将计入中性交易",
    "2. 若交易记录明细无有效内容，则代表该时间段内此微信号无交易",
    "3. 本明细仅供个人对账使用",
    "4. 本账单中所有时间均为UTC+08:00时间",
    "----------------------微信支付账单明细列表--------------------",
]

WECHAT_ROWS = [
    "2026-09-03 12:31:05,商户消费,某某餐饮管理有限公司,午餐,支出,¥28.50,零钱,支付成功,WX2026090300001,WXMO20260903001,",
    "2026-09-25 18:45:00,商户消费,某连锁超市有限公司,日用品,支出,¥63.20,零钱通,支付成功,WX2026092500002,WXMO20260925002,",
    "2026-09-28 07:15:30,商户消费,某早餐店,包子豆浆,支出,¥60.00,零钱,支付成功,WX2026092800003,WXMO20260928003,",
]


# ---------------------------------------------------------------------------
# 极简 xlsx 写入（纯标准库，不依赖 openpyxl）
# ---------------------------------------------------------------------------

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>
</Relationships>"""


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _col_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def write_xlsx(path: str, rows: list[list[str]]) -> None:
    strings: list[str] = []
    seen: dict[str, int] = {}

    def sid(text: str) -> int:
        if text not in seen:
            seen[text] = len(strings)
            strings.append(text)
        return seen[text]

    sheet_rows = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row):
            if value is None or value == "":
                continue
            ref = f"{_col_letter(c)}{r}"
            cells.append(f'<c r="{ref}" t="s"><v>{sid(str(value))}</v></c>')
        sheet_rows.append(f'<row r="{r}">{"".join(cells)}</row>')

    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    shared = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'count="{len(strings)}" uniqueCount="{len(strings)}">'
        + "".join(f"<si><t>{_esc(s)}</t></si>" for s in strings)
        + "</sst>"
    )

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _ROOT_RELS)
        zf.writestr("xl/workbook.xml", _WORKBOOK)
        zf.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
        zf.writestr("xl/sharedStrings.xml", shared)


def main() -> None:
    os.makedirs(FIXTURES, exist_ok=True)

    # 支付宝：UTF-8 带 BOM，和真实导出一致
    alipay = os.path.join(FIXTURES, "sample_alipay.csv")
    lines = ALIPAY_PREAMBLE + [ALIPAY_HEADER] + ALIPAY_ROWS
    with open(alipay, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"已生成 {alipay}")

    # 微信：xlsx
    wechat = os.path.join(FIXTURES, "sample_wechat.xlsx")
    grid = [[line] for line in WECHAT_PREAMBLE]
    grid.append(WECHAT_HEADER.split(","))
    grid.extend([row.split(",") for row in WECHAT_ROWS])
    write_xlsx(wechat, grid)
    print(f"已生成 {wechat}")

    # 一个「只有说明和表头、没有流水」的文件，用来验证导入器会不会失败得响亮
    stub = os.path.join(FIXTURES, "sample_stub_only.csv")
    with open(stub, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("\n".join(ALIPAY_PREAMBLE + [ALIPAY_HEADER]) + "\n")
    print(f"已生成 {stub}")

    # 状态专项：交易成功 / 交易关闭 / 已全额退款混在一份账单里。
    # 这几笔的分类和关键词都完全正常，**只有状态列能区分它们**，
    # 所以它必须单独成文件 —— 混进主样本会让「哪一笔该被压中性」说不清。
    status = os.path.join(FIXTURES, "sample_alipay_status.csv")
    lines = ALIPAY_STATUS_PREAMBLE + [ALIPAY_HEADER] + ALIPAY_STATUS_ROWS
    with open(status, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"已生成 {status}")


if __name__ == "__main__":
    main()
