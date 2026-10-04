"""生成合成加密银行 PDF 账单，用于测试 PDF 导入链路。

为什么不直接用真实账单测：
    真实账单里全是隐私（姓名、卡号、商户、金额）。测试要的是「格式对不对」，
    不需要真数据。用合成数据能在**不碰任何真实信息**的前提下把链路验通，
    真实数据只在你自己跑的那一次接触解析器。

刻意做出真实银行 PDF 的几个特征：
  * 每行同时有「交易金额」和「账户余额」两个两位小数（考验取哪个）
  * 金额带正负号
  * 日期和时间分列
  * AES-256 加密 + 用户密码（测试解密路径）

用法：<venv>\\Scripts\\python.exe tests/make_bank_pdf.py
"""

from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(os.path.dirname(HERE), "fixtures")
OUT = os.path.join(FIXTURES, "sample_bank.pdf")
PASSWORD = "TestBankPdf-2026"

HEADER = ["交易日期", "交易时间", "交易摘要", "交易对方", "交易金额", "账户余额"]

# 全部是编的。结构照着常见银行流水来。
ROWS = [
    ["2026-09-03", "12:31:05", "商户消费", "测试餐饮管理有限公司", "-28.50", "1234.56"],
    ["2026-09-05", "19:02:44", "快捷支付", "测试网络科技有限公司", "-115.00", "1119.56"],
    ["2026-09-08", "07:45:10", "商户消费", "测试便利店连锁有限公司", "-6.50", "1113.06"],
    ["2026-09-12", "13:20:31", "转账收入", "测试工资代发", "+3200.00", "4313.06"],
    ["2026-09-15", "21:11:09", "商户消费", "测试超市有限公司", "-88.80", "4224.26"],
    ["2026-09-18", "08:03:52", "代扣缴费", "测试电力公司", "-63.20", "4161.06"],
    ["2026-09-22", "17:40:15", "商户消费", "测试出行科技有限公司", "-19.90", "4141.16"],
    ["2026-09-26", "11:28:03", "退款", "测试电商平台", "+45.00", "4186.16"],
    ["2026-09-29", "20:55:47", "商户消费", "测试餐饮管理有限公司", "-52.00", "4134.16"],
]

# 列位置（点）。用固定 x 让每列对齐，PDF 里的词坐标才会成列。
#
# 这里踩过一次坑，记下来：PyMuPDF 的内置中文字体把数字也按**全角**渲染，
# 「2026-09-03」10 个字符在字号 9 下占 90pt，不是直觉上的 45pt。
# 第一版按 50/110/160 排，结果日期列（50→140）直接和后一列（110）重叠，
# 词坐标互相压住，按间距分列的算法自然全乱。
# 所以这里用横向 A4，并按「字符数 x 字号」留足列距。
COL_X = [40, 160, 280, 360, 560, 660]

FONT_SIZE = 9
TITLE_SIZE = 15
META_SIZE = 9
LINE_STEP = 16


def main() -> None:
    try:
        import pymupdf
    except ImportError:
        import fitz as pymupdf

    os.makedirs(FIXTURES, exist_ok=True)
    doc = pymupdf.open()
    # 横向 A4：银行流水列多，纵向排不下，真实账单也多半是横向
    page = doc.new_page(width=842, height=595)

    font = "china-s"     # PyMuPDF 内置简体中文字体
    page.insert_text((40, 45), "测试银行 交易明细", fontname=font, fontsize=TITLE_SIZE)
    page.insert_text((40, 65), "账号：**** **** **** 0000（合成测试数据）",
                     fontname=font, fontsize=META_SIZE)
    page.insert_text((40, 80), "统计区间：2026-09-01 至 2026-09-30",
                     fontname=font, fontsize=META_SIZE)

    y = 110
    for i, name in enumerate(HEADER):
        page.insert_text((COL_X[i], y), name, fontname=font, fontsize=FONT_SIZE)
    y += 5
    page.draw_line(pymupdf.Point(40, y), pymupdf.Point(790, y))
    y += LINE_STEP

    for row in ROWS:
        for i, cell in enumerate(row):
            page.insert_text((COL_X[i], y), cell, fontname=font, fontsize=FONT_SIZE)
        y += LINE_STEP

    y += 10
    page.insert_text((40, y), "本文件为合成测试数据，不含任何真实信息。",
                     fontname=font, fontsize=8)

    doc.save(
        OUT,
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw=PASSWORD + "-owner",
        user_pw=PASSWORD,
    )
    doc.close()

    size = os.path.getsize(OUT)
    print(f"已生成 {OUT}（{size} 字节，AES-256 加密，密码 {PASSWORD}）")
    print(f"合成交易 {len(ROWS)} 笔，其中收入 2 笔、支出 7 笔")


if __name__ == "__main__":
    main()
