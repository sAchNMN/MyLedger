"""各数据源的解析适配器。

每个适配器只做一件事：把自家导出的二维表映射成统一的 Txn。
所有脏活（编码、找表头、压缩包）都在 sniff 里做完了。

新增一个数据源的成本 = 加一张别名表 + 写 10 行映射。

关于解析失败记录（ML-007）
--------------------------
`ParseResult.skipped` 里**只允许**放三样东西：行号、字段名、错误码。

以前这里放的是 `{"reason": f"金额解析失败：{cell!r}", "raw": 整行前 120 字}`，
而 pipeline 会把这些原样打印出来，后台执行器再把标准输出整段追加进
`vault\\logs\\monthly-YYYYMM.log`。也就是说：**一笔解析失败就等于把那一行的
商户名、金额、单号、备注抄进了一个长期文本文件**，日志还会被备份和同步。

所以这里刻意不留任何自由文本字段。「原因」这种东西一旦可以自由填写，
早晚会有人（包括未来的我）为了好排障把原始值塞回去。
需要人话的时候由 model.describe_issue() 从错误码翻译。
"""

from __future__ import annotations

import datetime as dt
import re
import zipfile
from decimal import Decimal, InvalidOperation
from typing import Optional

from .model import (
    ParseResult,
    Txn,
    normalize_text,
    parse_amount,
    parse_datetime,
    parse_direction,
)
from .sniff import (
    build_index,
    cell,
    extract_declared_count,
    find_header_row,
    header_names,
    load_table,
    maybe_excel_date,
)

# --------------------------------------------------------------------------
# 列名别名表
#
# 写别名而不是写列号：导出格式改版时只改这里。
# 名字越具体越好，太短的关键词会误命中（例如「金额」不要去匹配「金额备注」）。
# --------------------------------------------------------------------------

ALIPAY_ALIASES = {
    "ts": ("交易时间", "交易创建时间", "创建时间", "付款时间"),
    "category_raw": ("交易分类",),
    "counterparty": ("交易对方",),
    "counterparty_account": ("对方账号",),
    "item": ("商品说明", "商品名称"),
    "direction": ("收/支", "收支"),
    "amount": ("金额",),
    "method": ("收/付款方式", "付款方式", "支付方式"),
    "status": ("交易状态",),
    "txn_id": ("交易订单号", "交易号"),
    "merchant_order_id": ("商家订单号",),
    "note": ("备注",),
}

WECHAT_ALIASES = {
    "ts": ("交易时间",),
    "category_raw": ("交易类型",),
    "counterparty": ("交易对方",),
    "item": ("商品", "商品说明"),
    "direction": ("收/支", "收支"),
    "amount": ("金额",),
    "method": ("支付方式", "收/付款方式"),
    "status": ("当前状态", "交易状态"),
    "txn_id": ("交易单号", "交易订单号"),
    "merchant_order_id": ("商户单号", "商家订单号"),
    "note": ("备注",),
}

# 银行 APP 截图 OCR 出来的文本行，字段通常长得这样
BANK_OCR_ALIASES = {
    "ts": ("交易时间", "时间", "日期"),
    "counterparty": ("交易对方", "对方", "商户", "收款方"),
    "item": ("摘要", "说明", "交易类型", "商品"),
    "amount": ("金额", "发生额", "交易金额"),
    "direction": ("收/支", "收支", "方向"),
    "status": ("状态", "交易状态"),
    "txn_id": ("交易单号", "流水号"),
    "note": ("备注",),
}


def _resolve_ts(raw: str):
    """先按常规格式解析时间，失败再试 Excel 序列号。"""
    value = parse_datetime(raw)
    if value is None:
        value = maybe_excel_date(raw)
    return value


# --------------------------------------------------------------------------
# raw 里允许放什么（ML-010）
#
# 以前 raw 是 {"来源":…, "编码":…, "数据行":…, "原始": row} —— 整行原样入库。
# 账本因此变成「账单的一份完整副本 + 一份解析结果」：报表用不到的列
# （对方账号、交易分类原文、平台随手加的扩展列）全都被永久保存下来。
# 更麻烦的是它的性质：raw 看起来像调试信息，所以没人会去清理它。
#
# 现在 raw 只留三种东西：
#   1. 来源信息：从哪来、什么编码、第几行
#   2. 形状摘要：这一行有几个字段、几个非空、关键列有没有值
#   3. 方向列的值 —— 唯一保留的原始单元格值，因为 fixups 靠它重推收支方向，
#      而它的取值只有「支出 / 收入 / 不计收支」这几种固定词
#
# 形状摘要的用处很具体：「11 个字段但金额列是空的」和「只有 2 个字段」
# 是两种完全不同的故障（导出格式变了 / 切错了列），也都能一眼看出来，
# 但这两组数字说不出任何一笔消费。
# --------------------------------------------------------------------------

def _row_summary(row: list[str], index: Optional[dict] = None) -> dict:
    summary = {
        "字段数": len(row),
        "非空字段数": sum(1 for cell_value in row if cell_value),
    }
    if index:
        # 银行截图 / PDF 那条路没有「列」的概念（靠字段长相认列），这两项没意义
        summary["金额列有值"] = bool(cell(row, index, "amount"))
        summary["时间列有值"] = bool(cell(row, index, "ts"))
    return summary


def _parse_rows(
    rows: list[list[str]],
    aliases: dict,
    source: str,
    path: str,
    encoding: str,
    origin: str,
) -> ParseResult:
    """通用解析流程：找表头 → 按列名取值 → 校验 → 产出 Txn。"""
    header_row = find_header_row(rows)
    if header_row is None:
        return ParseResult(
            source=source, txns=[], declared_count=extract_declared_count(rows),
            skipped=[{"row": 0, "reason_code": "missing_header"}], path=path,
        )

    header = header_names(rows[header_row])
    index = build_index(header, aliases)

    # 必需字段缺失就直接失败，不要产出半成品账单
    missing = [f for f in ("ts", "amount") if f not in index]
    if missing:
        return ParseResult(
            source=source, txns=[], declared_count=extract_declared_count(rows),
            skipped=[{
                "row": header_row + 1,
                "reason_code": "missing_required_column",
                "fields": missing,
            }],
            path=path,
        )

    txns: list[Txn] = []
    skipped: list[dict] = []

    for offset, row in enumerate(rows[header_row + 1:]):
        row_no = header_row + 1 + offset
        if not any(row):
            continue

        # 文件末尾的统计行（收入合计/支出合计）不是交易，跳过
        joined = "".join(row)
        if "合计" in joined or "总计" in joined or "小计" in joined:
            continue

        amount = parse_amount(cell(row, index, "amount"))
        if amount is None or amount == 0:
            skipped.append({
                "row": row_no + 1,
                "reason_code": "invalid_amount",
                "field": "amount",
            })
            continue

        ts = _resolve_ts(cell(row, index, "ts"))
        if ts is None:
            skipped.append({
                "row": row_no + 1,
                "reason_code": "invalid_datetime",
                "field": "ts",
            })
            continue

        counterparty = cell(row, index, "counterparty")
        item = cell(row, index, "item")

        txns.append(Txn(
            source=source,
            ts=ts,
            amount=amount,
            direction=parse_direction(cell(row, index, "direction"), counterparty, item),
            counterparty=counterparty,
            item=item,
            method=cell(row, index, "method"),
            status=cell(row, index, "status"),
            txn_id=cell(row, index, "txn_id"),
            merchant_order_id=cell(row, index, "merchant_order_id"),
            note=cell(row, index, "note"),
            raw={
                "来源": origin,
                "编码": encoding,
                "表头行": header_row + 1,
                "数据行": row_no + 1,
                "方向列": cell(row, index, "direction"),   # 供 fixups 重推方向用
                **_row_summary(row, index),
            },
        ))

    return ParseResult(
        source=source,
        txns=txns,
        declared_count=extract_declared_count(rows),
        skipped=skipped,
        path=path,
    )


def parse_alipay(path: str, password: Optional[str] = None) -> ParseResult:
    """支付宝交易明细。"""
    rows, encoding, origin = load_table(path, password)
    return _parse_rows(rows, ALIPAY_ALIASES, "alipay", path, encoding, origin)


def parse_wechat(path: str, password: Optional[str] = None) -> ParseResult:
    """微信支付账单流水。"""
    rows, encoding, origin = load_table(path, password)
    return _parse_rows(rows, WECHAT_ALIASES, "wechat", path, encoding, origin)


def parse_bank_ocr(tsv_path: str, password: Optional[str] = None) -> ParseResult:
    """银行截图 OCR 后的 TSV。

    先按表头解析（万一某家银行的 OCR 结果带表头）；没有表头就走自动字段识别。
    自动识别是必须的——银行 APP 的截图里不存在「表头行」这个概念，
    OCR 出来就是一堆按坐标重组的文本行，只有靠字段长相判断哪个是时间、哪个是金额。
    """
    result = _parse_rows(load_table(tsv_path, None)[0], BANK_OCR_ALIASES,
                         "bank_ocr", tsv_path, "auto", "")
    # _parse_rows 找不到表头时返回 missing_header，这时改用自动识别
    if result.txns or not result.skipped or \
            result.skipped[0].get("reason_code") != "missing_header":
        return result
    rows, encoding, origin = load_table(tsv_path, None)
    return _parse_bank_rows_auto(rows, tsv_path, origin=origin, encoding=encoding)


# --------------------------------------------------------------------------
# 银行截图：无表头的自动字段识别
#
# 思路是完全确定的（不猜语义，只看字段长得像什么）：
#   1. 找形如日期 / 时间的单元格 → ts
#   2. 找形如金额的单元格 → amount（带符号或带「支出/收入」字样的同时定方向）
#   3. 剩下的中文长文本里最长的那个 → 交易对方
#   4. 时间和金额缺任何一个的行直接丢弃（那是「本月合计」这类标签行）
#
# 刻意不做的事：不去猜「哪个是商户名」。宁可把对方留空，
# 也好过把一串流水号当成商户名写进账本——那种错你不会发现。
# --------------------------------------------------------------------------

_RE_DATE = re.compile(r"^\s*(?:(\d{4})[-/.年])?(\d{1,2})[-/.月](\d{1,2})日?\s*$")
# 「在文本里找日期」用这个：09-15 / 2026-09-15 / 9.15，后面不能再接数字
_RE_DATE_TOKEN = re.compile(r"(?:(\d{4})[-/.年])?(\d{1,2})[-/.月](\d{1,2})日?(?!\d)")
_RE_TIME = re.compile(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$")

# 金额 token：**必须带两位小数**（可选货币符号 / 正负号）。
#
# 为什么死磕「两位小数」：这是整条银行截图链路里最强的判据。
#   * 银行金额永远是两位小数（38.50 / 0.00），不会写成 38.5
#   * 日期「09-15」没有小数点，余额「1234.56」格式相同但通常是另一个 token，
#     靠「取第一个 / 带符号的优先」区分
#   * 只要求「是个数字」会把日期、序号、卡号全吸进来 —— 那是灾难
_RE_AMOUNT_TOKEN = re.compile(r"[¥￥$]?\s*([+-])?\s*(\d{1,3}(?:,\d{3})*|\d+)\.(\d{2})(?!\d)")
_RE_HAS_CJK = re.compile(r"[\u4e00-\u9fff]")
_INCOME_WORDS = ("收入", "转入", "存入", "入账", "退款", "贷")
_EXPENSE_WORDS = ("支出", "转出", "消费", "付款", "扣款", "借")


def _extract_amounts(text: str) -> list:
    """从一段文本里抠出所有金额 token，返回 [(符号, 数字串)]。"""
    out = []
    for sign, integer, decimals in _RE_AMOUNT_TOKEN.findall(text):
        out.append((sign or "", f"{integer.replace(',', '')}.{decimals}"))
    return out


def _extract_dates(text: str) -> list:
    """从一段文本里抠出所有日期 token，返回 [(年或None, 月, 日)]。"""
    out = []
    for y, mo, d in _RE_DATE_TOKEN.findall(text):
        mo, d = int(mo), int(d)
        if 1 <= mo <= 12 and 1 <= d <= 31:
            out.append((int(y) if y else None, mo, d))
    return out


def _parse_bank_rows_auto(rows, path: str, default_year: Optional[int] = None,
                          origin: str = "", encoding: str = "",
                          source: str = "bank_ocr") -> ParseResult:
    from .model import DIRECTION_EXPENSE, DIRECTION_INCOME, DIRECTION_NEUTRAL

    now = dt.datetime.now()
    year = default_year or now.year

    txns: list[Txn] = []
    skipped: list[dict] = []
    year_guessed = 0

    for row_no, row in enumerate(rows, start=1):
        cells = [c for c in (normalize_text(c) for c in row) if c]
        if not cells:
            continue

        # ------------------------------------------------------------------
        # 逐格「找 token」，不是逐格「判定整格是什么」。
        #
        # 这是被真实数据逼出来的：把同一行的碎片按间距拼接后，
        # 日期和金额经常落进同一个格子里（「09-15 38.50」）。
        # 要求整格匹配金额，结果就是 31 笔真实交易全部解析失败。
        # 改成在文本里搜 token，合并与否都能捞出来。
        # ------------------------------------------------------------------
        dates: list = []
        amounts: list = []
        time_part = None
        for cell in cells:
            dates.extend(_extract_dates(cell))
            for sign, digits in _extract_amounts(cell):
                amounts.append((sign, digits))

        if not dates:
            continue
        if not amounts:
            if any(_RE_HAS_CJK.search(c) for c in cells):
                skipped.append({"row": row_no, "reason_code": "invalid_amount",
                                "field": "amount"})
            continue

        for cell in cells:
            m = _RE_TIME.match(cell)
            if m:
                h, mi, s = m.groups()
                time_part = (int(h), int(mi), int(s or 0))
                break

        # 一行出现多个两位小数时（银行流水常同时显示「金额」和「余额」）：
        # 优先取带正负号的；都没符号就取第一个，并在备注里标出来让人核。
        signed = [a for a in amounts if a[0]]
        sign, digits = signed[-1] if signed else amounts[0]
        multi_amount_note = ""
        if len(amounts) > 1 and not signed:
            multi_amount_note = f"该行识别到 {len(amounts)} 个金额（可能是余额），已取第一个，请核对"

        try:
            amount = Decimal(digits)
        except InvalidOperation:
            skipped.append({"row": row_no, "reason_code": "invalid_amount",
                            "field": "amount"})
            continue

        direction_hint = None
        blob = "".join(cells)
        if any(w in blob for w in _INCOME_WORDS):
            direction_hint = DIRECTION_INCOME
        elif any(w in blob for w in _EXPENSE_WORDS):
            direction_hint = DIRECTION_EXPENSE

        date_part = dates[0]
        y, mo, d = date_part
        if y is None:
            y = year
            year_guessed += 1
        h, mi, s = time_part or (0, 0, 0)
        try:
            ts = dt.datetime(y, mo, d, h, mi, s)
        except ValueError:
            skipped.append({"row": row_no, "reason_code": "invalid_datetime",
                            "field": "ts"})
            continue

        # 没有年份的截图：如果落到未来，说明是去年的账单
        if date_part[0] is None and ts > now:
            try:
                ts = ts.replace(year=y - 1)
            except ValueError:
                pass

        if direction_hint:
            direction = direction_hint
        elif sign == "+":
            direction = DIRECTION_INCOME
        else:
            direction = DIRECTION_EXPENSE   # 银行流水里绝大多数是支出，负数也归这里

        # 剩下的中文里最长的那个当交易对方。
        # 刻意「不猜」：宁可留空，也好过把一串流水号或「余额」当成商户名。
        candidates = [
            c for c in cells
            if _RE_HAS_CJK.search(c) and len(c) >= 2
            and not _extract_dates(c) and not _extract_amounts(c)
        ]
        counterparty = max(candidates, key=len) if candidates else ""

        notes = []
        if multi_amount_note:
            notes.append(multi_amount_note)
        if time_part is None:
            notes.append("截图未识别到时间，统一记为 00:00:00")

        txns.append(Txn(
            source=source,
            ts=ts,
            amount=amount,
            direction=direction,
            counterparty=counterparty,
            item="",
            method="",
            status="",
            txn_id="",              # 截图里通常没有可用的流水号，靠内容指纹去重
            merchant_order_id="",
            note="；".join(notes),
            raw={"来源": origin, "编码": encoding, "数据行": row_no,
                 **_row_summary(row)},
        ))

    if year_guessed:
        skipped.append({
            "row": 0,
            "reason_code": "year_guessed",
            "count": year_guessed,
            "year": year,
        })

    return ParseResult(
        source=source,
        txns=txns,
        declared_count=None,      # 截图/PDF 都没有「共 N 笔」自述
        skipped=skipped,
        path=path,
    )


def parse_bank_pdf(path: str, password: Optional[str] = None) -> ParseResult:
    """加密 PDF 银行账单。

    这是银行流水最靠谱的一条路：PDF 的文字是结构化存储的，
    抽出来是精确字符 + 精确坐标，没有 OCR 的识别误差。
    """
    from .bank_pdf import extract_rows

    rows, meta = extract_rows(path, password)
    notes = [f"PDF {meta['pages']} 页"]
    if meta.get("encrypted"):
        notes.append("已用密码解密")
    # source 必须一路传下去。之前只在结果对象上改 source，
    # 每笔 Txn 还是写死的 "bank_ocr" —— 界面和报表读的是 Txn.source，
    # 于是 PDF 导入的数据被显示成「银行卡截图」。
    return _parse_bank_rows_auto(rows, path, origin="、".join(notes),
                                 source="bank_pdf")


# --------------------------------------------------------------------------
# 数据源自动识别
# --------------------------------------------------------------------------

def detect_source(path: str) -> str:
    """按文件名和内容特征猜数据源。

    猜错的代价只是解析出 0 笔 + 明确报错，不会写坏数据，
    所以这里用启发式判断是安全的。
    """
    name = path.lower()

    # PDF 一律当银行账单。支付宝/微信导出的是 csv/xlsx/zip，不会是 PDF。
    if name.endswith(".pdf"):
        return "bank_pdf"

    # 「装着 PDF 的 zip」也当银行账单。银行寄账单喜欢包一层：
    # 文件名就是一堆数字，既不含渠道关键词，也不是 .pdf 后缀，
    # 靠下面的表头探测必然失败（zip 里没有表头）。
    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as zf:
                inner = [n for n in zf.namelist() if n.lower().endswith(".pdf")]
            if inner:
                return "bank_pdf"
        except Exception:
            pass        # 读不了就当普通压缩包，交给后面按表头判断

    if "支付宝" in name or "alipay" in name:
        return "alipay"
    if "微信" in name or "wechat" in name or "wxpay" in name or "tenpay" in name:
        return "wechat"

    # 文件名认不出来就看表头
    try:
        rows, _, _ = load_table(path, None)
    except Exception:
        return "unknown"
    header_row = find_header_row(rows)
    if header_row is None:
        return "unknown"
    header = "".join(header_names(rows[header_row]))
    if "交易单号" in header or "商户单号" in header:
        return "wechat"
    if "交易订单号" in header or "商家订单号" in header or "对方账号" in header:
        return "alipay"
    return "unknown"


PARSERS = {
    "alipay": parse_alipay,
    "wechat": parse_wechat,
    "bank_ocr": parse_bank_ocr,
    "bank_pdf": parse_bank_pdf,
}
