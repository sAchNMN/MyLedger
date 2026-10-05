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

# --------------------------------------------------------------------------
# 交易状态判定
#
# 平台的状态列回答的是一个问题：**这笔钱到底有没有真的花出去。**
# 这是账单里最容易被忽略、但对「月支出」影响最大的一列：
#
#     支付宝「交易关闭」  —— 付了但交易被撤销/超时关闭，钱没扣（常见于共享单车
#                           押金、免密支付失败、订单取消）
#     微信「已全额退款」  —— 钱回来了，那笔支出等于没发生
#     微信「已过期」      —— 红包/转账超时退回零钱，钱根本没离开过
#     微信「对方已收钱」  —— 这才是真花出去了
#
# 不判的后果非常具体：月支出被虚高，而这种虚高在报表上看起来完全正常。
#
# 判定顺序刻意是 failed → refunded → pending → ok，**不是按「成功」先判**。
# 因为「退款成功」这个值里含有「成功」两个字：先判 ok 的话，
# 一笔全额退款会被当成正常消费稳稳地算进支出，这正是最该避免的那种错。
# --------------------------------------------------------------------------

STATUS_OK = "ok"                # 确认完成，钱真的出去了
STATUS_REFUNDED = "refunded"    # 钱回来了（退款）
STATUS_FAILED = "failed"        # 交易没成立（关闭/取消/失败/过期）
STATUS_PENDING = "pending"      # 还没定论
STATUS_UNKNOWN = "unknown"      # 没有状态列，或值认不出来

# 判为「没花出去」的值。闭单、退款、超时退回都在这里。
_STATUS_FAILED_WORDS = (
    "交易关闭", "支付失败", "交易失败", "已取消", "取消成功", "交易取消",
    "已关闭", "关闭", "失败", "未支付", "待付款", "已过期", "过期", "失效",
)

# 判为「钱回来了」的值。**必须排在 ok 之前判**，见上面的注释。
_STATUS_REFUNDED_WORDS = (
    "已全额退款", "全额退款", "部分退款", "已退款", "退款成功", "退款",
    "已退回", "退回", "已返还", "返还",
)

_STATUS_PENDING_WORDS = (
    "交易中", "处理中", "待确认", "待支付", "等待", "冻结", "授权中", "确认中",
)

# 确认完成。注意「成功」放在最后匹配，且只在前面都没命中时才算数。
_STATUS_OK_WORDS = (
    "交易成功", "支付成功", "对方已收钱", "朋友已收钱", "已转账", "收款成功",
    "已存入零钱", "充值完成", "提现成功", "已到账", "已入账", "还款成功",
    "已结清", "成功",
)


def status_verdict(status: str) -> str:
    """把平台的交易状态/当前状态转成四种结论之一。

    认不出来就返回 unknown，**不猜**。原因和 fixups._derive_direction 里
    「查不到就别猜」是同一条纪律：把「已过期」误判成 ok 或 failed，
    都会让一笔钱的去留凭空翻转，而用户没有任何线索去发现。
    """
    text = normalize_text(status)
    if not text:
        return STATUS_UNKNOWN
    for word in _STATUS_FAILED_WORDS:
        if word in text:
            return STATUS_FAILED
    for word in _STATUS_REFUNDED_WORDS:
        if word in text:
            return STATUS_REFUNDED
    for word in _STATUS_PENDING_WORDS:
        if word in text:
            return STATUS_PENDING
    for word in _STATUS_OK_WORDS:
        if word in text:
            return STATUS_OK
    return STATUS_UNKNOWN


# 状态为这几种时，**支出**要被压成中性（钱没真的花出去）
STATUS_KILLS_EXPENSE = frozenset({STATUS_FAILED, STATUS_REFUNDED, STATUS_PENDING})


def status_note(status: str) -> str:
    """给人看的一句话结论。只输出状态词和结论，不含任何其他字段。"""
    verdict = status_verdict(status)
    labels = {
        STATUS_OK: "已确认完成",
        STATUS_REFUNDED: "已退款（钱回来了）",
        STATUS_FAILED: "交易未成立（钱没花出去）",
        STATUS_PENDING: "尚未完成",
        STATUS_UNKNOWN: "状态无法识别",
    }
    return labels[verdict]


_DIRECTION_MAP = {
    "支出": DIRECTION_EXPENSE,
    "收入": DIRECTION_INCOME,
    "不计收支": DIRECTION_NEUTRAL,
    "中性交易": DIRECTION_NEUTRAL,
    "其他": DIRECTION_NEUTRAL,
    # 银行流水用「借/贷」记账：贷 = 钱进来，借 = 钱出去。
    # 这是复式记账的惯例（贷方增加借方减少），各家银行的 PDF 都这么印。
    "借": DIRECTION_EXPENSE,
    "贷": DIRECTION_INCOME,
    "借方": DIRECTION_EXPENSE,
    "贷方": DIRECTION_INCOME,
    "借方发生额": DIRECTION_EXPENSE,
    "贷方发生额": DIRECTION_INCOME,
    "收": DIRECTION_INCOME,
    "付": DIRECTION_EXPENSE,
    "转入": DIRECTION_INCOME,
    "转出": DIRECTION_EXPENSE,
    "存入": DIRECTION_INCOME,
    "支取": DIRECTION_EXPENSE,
    "": DIRECTION_NEUTRAL,
}

# 视为中性交易的关键词：充值、提现、转账给自己等，不算真实消费
NEUTRAL_KEYWORDS = (
    "充值", "提现", "零钱通", "理财通", "信用卡还款", "余额宝",
    "转账", "还款", "备用金", "花呗还款", "借款", "基金",
)


def parse_direction(raw: str, counterparty: str = "", item: str = "",
                    amount: Optional[Decimal] = None) -> str:
    """把「收/支」列的值转成统一方向。

    支付宝用「不计收支」，微信用「中性交易」或留空。
    另外，即使标注为「支出」，充值/提现这类也不算真实消费，归为中性。

    `amount` 是**兜底用的**：实测农行活期明细的 9 列表头里根本没有
    方向列（没有「借贷标志」也没有「收/支」），只有「交易金额」一列
    印着 `+2400.00` / `-2400.00`。没有这个参数时那两笔都会落到下面的
    兜底分支被判成中性 —— 月支出看起来是 0、月收入也是 0，
    而实际上有 2400 进来过。
    """
    text = normalize_text(raw)
    for key, direction in _DIRECTION_MAP.items():
        if key and key in text:
            return direction

    blob = f"{counterparty}{item}"
    if any(k in blob for k in NEUTRAL_KEYWORDS):
        # 「转账」在 NEUTRAL_KEYWORDS 里，**但只在金额认不出正负号时才生效**。
        # 实测踩到的：用户有一笔「转存 +2400.00」（别人转给他，
        # 他再转进别人账户帮朋友刷业绩），他明确要求这笔**算收入**。
        # 若这里不看金额直接判中性，月收入就是 0 —— 而钱确实进来了。
        #
        # 口径与 `sources._NEUTRAL_SUMMARY_WORDS` 一致：**只压支出侧**。
        if amount is not None and amount > 0:
            return DIRECTION_INCOME
        return DIRECTION_NEUTRAL

    # 没有方向列可依据时，用金额正负号兜底。
    #
    # 三支都要写全，缺一支就会静默错判：
    #   正数 → 收入（钱进来了）
    #   负数 → 支出（钱出去了）   ← 少了这支，普通消费会被判成中性，
    #                              月支出永远是 0，而报表看不出异常
    #   无符号/为 0 → 中性（真的认不出来）
    #
    # 刻意**不**「默认判支出」：银行流水里绝大多数是支出，
    # 但「绝大多数」不是「全部」，默认支出等于把收入也吞掉。
    if amount is not None:
        if amount > 0:
            return DIRECTION_INCOME
        if amount < 0:
            return DIRECTION_EXPENSE
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
    platform_category: str = ""       # 平台自带的分类原文（支付宝「交易分类」/ 微信「交易类型」）
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
#
# 这张表还承担第二个职责：**区分「文件坏了」和「规则没认出来」**。
#     解析出 0 笔时，这两类天差地别 ——
#       · 文件真的没有流水行  → 重新导出
#       · 有行但字段没认出来  → 调解析规则
#     以前两者报同一句话（且 skipped 也是空的），
#     用户看到「解析 0 笔 / 跳过 0 行」完全不知道该干什么。
#     所以「有金额没日期」这类行必须进 skipped，不能静默丢掉。
# --------------------------------------------------------------------------

ISSUE_LABELS = {
    "missing_header": "找不到表头行",
    "missing_required_column": "表头缺少必需列",
    "invalid_amount": "金额字段格式无效",
    "invalid_datetime": "时间字段格式无效",
    # 有金额、没日期。单独列出来是因为它和 invalid_amount 是两类问题：
    # 金额格式对但日期写法没被认出来（紧凑 8 位、只有年月、或者写在别的格里）。
    "no_date": "没找到日期（这一行有金额）",
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
    "status": "交易状态",
    "platform_category": "平台分类",
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
