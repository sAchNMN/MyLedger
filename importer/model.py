"""统一交易模型：所有数据源解析后都收敛成 Txn。

三个设计要点：

1. 金额一律存正的 Decimal，收支方向单独用 direction 表示。
   不用「负数=支出」这种约定——各家导出的符号规则不统一，很容易记反。

2. raw 里只留来源信息、行号和形状摘要，不留整行原文（见 sources.py 的注释）。
   排障需要知道「第几行、有几个字段、关键列有没有值」，不需要把那一行抄一遍。
   整行原文是账单里最完整的那份数据，把它存进账本等于账本变成了
   「账单副本 + 解析结果」。

3. fingerprint 只用于提示「疑似重复」，永远不用于自动删除数据。
   跨渠道的同一笔支出（比如支付宝扣款 + 银行流水）本来就长得很像，
   机器没有资格替你判断哪一笔是多余的。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

# --------------------------------------------------------------------------
# 收支方向
# --------------------------------------------------------------------------

DIRECTION_EXPENSE = "expense"
DIRECTION_INCOME = "income"
DIRECTION_NEUTRAL = "neutral"

_DIRECTION_MAP = {
    "支出": DIRECTION_EXPENSE,
    "收入": DIRECTION_INCOME,
    "不计收支": DIRECTION_NEUTRAL,
    "中性交易": DIRECTION_NEUTRAL,
    "其他": DIRECTION_NEUTRAL,
    "": DIRECTION_NEUTRAL,
}

# 视为中性交易的关键词：充值、提现、转账给自己等，不算真实消费
NEUTRAL_KEYWORDS = (
    "充值", "提现", "零钱通", "理财通", "信用卡还款", "余额宝",
    "转账", "还款", "备用金", "花呗还款", "借款", "基金",
)


def parse_direction(raw: str, counterparty: str = "", item: str = "") -> str:
    """把「收/支」列的值转成统一方向。

    支付宝用「不计收支」，微信用「中性交易」或留空。
    另外，即使标注为「支出」，充值/提现这类也不算真实消费，归为中性。
    """
    text = normalize_text(raw)
    for key, direction in _DIRECTION_MAP.items():
        if key and key in text:
            return direction

    blob = f"{counterparty}{item}"
    if any(k in blob for k in NEUTRAL_KEYWORDS):
        return DIRECTION_NEUTRAL
    return DIRECTION_NEUTRAL


# --------------------------------------------------------------------------
# 文本与金额归一化
# --------------------------------------------------------------------------

_FULLWIDTH_MAP = {ord(c): ord(c) - 0xFEE0 for c in "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ：，．（）"}


def normalize_text(value: Any) -> str:
    """全角转半角、去不可见字符、压缩空白。用于比较，不用于展示。"""
    if value is None:
        return ""
    text = str(value).translate(_FULLWIDTH_MAP)
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_amount(raw: Any) -> Optional[Decimal]:
    """从各种写法里抠出金额。

    见过的写法：「38.5」「¥38.50」「38.50元」「1,234.56」「-12.00」「38.50(退款)」。
    取不到或不是数字时返回 None，由调用方决定是跳过还是报错——
    绝不静默当成 0，那是最危险的失败方式。
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float, Decimal)):
        try:
            return abs(Decimal(str(raw)))
        except InvalidOperation:
            return None

    text = normalize_text(raw).replace(",", "").replace("，", "")
    text = text.replace("¥", "").replace("￥", "").replace("元", "").replace("$", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return abs(Decimal(match.group()))
    except InvalidOperation:
        return None


_DATETIME_PATTERNS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y年%m月%d日 %H:%M:%S",
)


def parse_datetime(raw: Any) -> Optional[datetime]:
    """解析交易时间。返回 None 表示解析失败，由调用方决定如何处理。"""
    if isinstance(raw, datetime):
        return raw
    text = normalize_text(raw)
    if not text:
        return None
    for pattern in _DATETIME_PATTERNS:
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    # 兜底：从中抠出 yyyy-mm-dd hh:mm:ss
    match = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?\s*(\d{1,2})?:?(\d{1,2})?:?(\d{1,2})?", text)
    if match:
        y, mo, d, h, mi, s = (int(g) if g else 0 for g in match.groups())
        try:
            return datetime(y, mo, d, h, mi, s)
        except ValueError:
            return None
    return None


def parse_excel_serial(value: float) -> Optional[datetime]:
    """Excel 日期序列号转 datetime（有些导出把时间存成数字）。"""
    try:
        from datetime import timedelta
        return datetime(1899, 12, 30) + timedelta(days=float(value))
    except Exception:
        return None


# --------------------------------------------------------------------------
# 交易
# --------------------------------------------------------------------------

SOURCE_LABELS = {
    "alipay": "支付宝",
    "wechat": "微信",
    "bank_ocr": "银行卡截图",
    "bank_pdf": "银行卡PDF",
}


@dataclass
class Txn:
    source: str                       # alipay | wechat | bank_ocr
    ts: datetime                      # 交易时间
    amount: Decimal                   # 金额（正数）
    direction: str                    # expense | income | neutral
    counterparty: str = ""            # 交易对方
    item: str = ""                    # 商品 / 商品说明
    method: str = ""                  # 收付款方式 / 支付方式
    status: str = ""                  # 交易状态 / 当前状态
    txn_id: str = ""                  # 交易单号 / 交易订单号（跨批次去重的主力）
    merchant_order_id: str = ""       # 商户单号 / 商家订单号
    note: str = ""                    # 备注
    category: str = "未分类"          # 消费分类（规则或本地 AI 给出）
    batch_id: str = ""                # 来自哪一次导入
    raw: dict = field(default_factory=dict)   # 来源 + 行号 + 形状摘要（不含整行原文）

    # ------------------------------------------------------------------
    def source_label(self) -> str:
        """数据源的中文名，用于展示。"""
        return SOURCE_LABELS.get(self.source, self.source)

    def match_key(self) -> Optional[str]:
        """L1 去重键：同一数据源 + 同一交易单号 = 同一条记录。

        单号为空时返回 None，退到内容指纹去比对。
        """
        if self.txn_id:
            return f"{self.source}:{self.txn_id}"
        if self.merchant_order_id:
            return f"{self.source}:mo:{self.merchant_order_id}"
        return None

    def fingerprint(self) -> str:
        """L2 指纹：时间(到分钟) + 金额 + 归一化对方。

        刻意不包含 source——正是为了抓住跨渠道的同一笔交易。
        也刻意不包含商品名，因为同一笔交易在不同渠道的商品描述往往不同。
        """
        # 金额格式化必须容错。Decimal 默认只有 28 位有效数字精度，
        # 遇到超长数字（解析错抓来的卡号、流水号）时 quantize 会抛
        # decimal.InvalidOperation —— 一条脏数据就能把整份账单的导入搞崩。
        # format(x, "f") 不设精度限制，不会抛。
        try:
            amount_text = format(self.amount.quantize(Decimal("0.01")), "f")
        except Exception:
            amount_text = format(self.amount, "f")

        parts = [
            self.ts.strftime("%Y-%m-%d %H:%M"),
            amount_text,
            normalize_text(self.counterparty).lower(),
        ]
        digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
        return digest[:16]

    def to_row(self) -> dict:
        import json
        data = asdict(self)
        data["ts"] = self.ts.isoformat(sep=" ", timespec="seconds")
        data["amount"] = str(self.amount)
        data["raw"] = json.dumps(self.raw, ensure_ascii=False)
        return data


# --------------------------------------------------------------------------
# 解析失败的错误码（ML-007）
#
# 为什么用码而不是自由文本：
#     失败原因以前是一条自由文本，里面内嵌了原始单元格的值
#     （"金额解析失败：'38.50元'"），有些还额外带一个 raw 字段装着整行原文。
#     这些内容会被打印出来，后台执行器再把标准输出整段追加进
#     vault\logs\monthly-YYYYMM.log —— 也就是一笔解析失败就把商户名、
#     金额、单号、备注抄进了一个长期文本文件。
#
#     码是一张白名单：想往失败记录里加内容，就得先来改这张表，
#     改的时候自然会想一下「这个字段是不是原始交易数据」。
# --------------------------------------------------------------------------

ISSUE_LABELS = {
    "missing_header": "找不到表头行",
    "missing_required_column": "表头缺少必需列",
    "invalid_amount": "金额字段格式无效",
    "invalid_datetime": "时间字段格式无效",
    "empty_transaction": "这一行没有任何交易内容",
    "unsupported_format": "不支持的文件格式",
    "duplicate_row": "本次导入内的重复行",
    "year_guessed": "有些行没有年份",
}

# 字段内部名 → 人话。只用于把「缺了哪个字段」说清楚，不含任何取值。
FIELD_LABELS = {
    "ts": "交易时间",
    "amount": "金额",
    "direction": "收支方向",
    "counterparty": "交易对方",
    "item": "商品",
    "txn_id": "交易单号",
}


def describe_issue(issue: dict) -> str:
    """把一条解析失败记录翻译成给人看的一句话。

    输出里**只能**有行号、字段名和错误类型。这个函数是日志白名单的最后一环：
    往这里加任何来自原始行的内容，等于把 ML-007 又打开一次。

    不认识的错误码原样打出来，刻意不回退到任何自由文本字段 ——
    那正是原始交易行溜进日志的入口。
    """
    row = issue.get("row") or 0
    where = f"第 {row} 行" if row else "整份文件"
    code = issue.get("reason_code") or ""
    if code not in ISSUE_LABELS:
        return f"{where}：{code or '未知错误'}"

    if code == "missing_required_column":
        names = "、".join(FIELD_LABELS.get(f, f) for f in (issue.get("fields") or []))
        return f"{where}：{ISSUE_LABELS[code]}（{names}）"
    if code == "year_guessed":
        return (f"{where}：{issue.get('count', 0)} 行没有年份，"
                f"按 {issue.get('year', '?')} 年处理")
    return f"{where}：{ISSUE_LABELS[code]}"


@dataclass
class ParseResult:
    """一次解析的产出。declared_count 来自源文件自述的笔数。"""

    source: str
    txns: list
    declared_count: Optional[int] = None
    skipped: list = field(default_factory=list)   # 解析失败的行：只有行号/字段/错误码
    path: str = ""

    def summary(self) -> str:
        lines = [
            f"数据源    : {SOURCE_LABELS.get(self.source, self.source)}",
            f"源文件    : {self.path}",
            f"解析成功  : {len(self.txns)} 笔",
        ]
        if self.declared_count is not None:
            lines.append(f"文件自述  : {self.declared_count} 笔")
            if len(self.txns) != self.declared_count:
                lines.append(
                    f"！不一致   : 差 {self.declared_count - len(self.txns)} 笔"
                    "（通常是导出文件不完整）"
                )
        if self.skipped:
            lines.append(f"跳过      : {len(self.skipped)} 行（见下方明细）")
        return "\n".join(lines)
