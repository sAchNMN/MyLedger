"""各数据源的解析适配器。

每个适配器只做一件事：把自家导出的二维表映射成统一的 Txn。
所有脏活（编码、找表头、压缩包）都在 sniff 里做完了。

新增一个数据源的成本 = 加一张别名表 + 写 10 行映射。
"""

from __future__ import annotations

from typing import Optional

from .model import (
    ParseResult,
    Txn,
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
            skipped=[{"row": 0, "reason": "整份文件里找不到表头行"}], path=path,
        )

    header = header_names(rows[header_row])
    index = build_index(header, aliases)

    # 必需字段缺失就直接失败，不要产出半成品账单
    missing = [f for f in ("ts", "amount") if f not in index]
    if missing:
        return ParseResult(
            source=source, txns=[], declared_count=extract_declared_count(rows),
            skipped=[{
                "row": header_row,
                "reason": f"表头缺少必需列 {missing}，实际表头：{header}",
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
                "reason": f"金额解析失败：{cell(row, index, 'amount')!r}",
                "raw": joined[:120],
            })
            continue

        ts = _resolve_ts(cell(row, index, "ts"))
        if ts is None:
            skipped.append({
                "row": row_no + 1,
                "reason": f"时间解析失败：{cell(row, index, 'ts')!r}",
                "raw": joined[:120],
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
                "原始": row,
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


def parse_bank_ocr(tsv_path: str) -> ParseResult:
    """银行截图 OCR 后的 TSV。OCR 环节见 scripts/bank-ocr.ps1。"""
    rows, encoding, origin = load_table(tsv_path, None)
    return _parse_rows(rows, BANK_OCR_ALIASES, "bank_ocr", tsv_path, encoding, origin)


# --------------------------------------------------------------------------
# 数据源自动识别
# --------------------------------------------------------------------------

def detect_source(path: str) -> str:
    """按文件名和内容特征猜数据源。

    猜错的代价只是解析出 0 笔 + 明确报错，不会写坏数据，
    所以这里用启发式判断是安全的。
    """
    name = path.lower()
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
}
