"""格式嗅探：把「任意一种账单导出文件」变成二维表。

为什么不写死列号：
    支付宝和微信的导出格式每隔一两年就会变一次，写死列号（"金额在第 6 列"）
    在下次改版时会静默解析错位，把金额读到备注列上——比报错危险得多。
    这里改成按表头名字匹配：先找到表头行，再按列名反查下标。
    改版只影响别名表，不影响逻辑。

同时负责三件事：
    1. 编码探测（UTF-8 BOM / GBK / GB18030）
    2. 跳过前置说明行，定位真正的表头行
    3. 读取文件自述的笔数（"共119笔记录"），供导入后校验
"""

from __future__ import annotations

import csv
import io
import os
import re
import zipfile
from datetime import datetime
from typing import Optional

from .model import normalize_text, parse_excel_serial

# 表头必须至少命中其中一个，否则不认为是表头行
HEADER_ANCHORS = ("交易时间", "交易号", "交易单号", "交易订单号", "金额", "收/支")

# 文件里自述笔数的写法：「共119笔记录」「共87笔记录」
DECLARED_COUNT_RE = re.compile(r"共\s*(\d+)\s*笔")

# 这些行是说明文字，不是数据；命中即跳过
PREAMBLE_HINTS = (
    "说明", "注：", "特别提示", "微信支付账单", "支付宝交易记录",
    "导出信息", "起始时间", "导出类型", "导出时间", "本账单",
    "本回单", "请勿", "若交易记录", "充值/提现",
)


# --------------------------------------------------------------------------
# 编码与文件读取
# --------------------------------------------------------------------------

ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk", "utf-16")


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """按优先级尝试解码，返回 (文本, 编码名)。

    utf-8-sig 排第一：支付宝新导出带 BOM，用 utf-8 解会在首格残留 \\ufeff，
    导致「交易时间」匹配不上，整份文件解析为 0 笔。
    gb18030 排在 gbk 前面：gb18030 是 gbk 的超集，能多救回一些生僻字。
    """
    for enc in ENCODINGS:
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    # 最后兜底：容错解码，不丢数据但可能有替换字符
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


def looks_like_xlsx(data: bytes) -> bool:
    """判断这段字节是不是一个 xlsx 文档。

    必须判断，不能只看 PK 头：xlsx 本身就是 zip，
    只看 magic bytes 会把微信账单 xlsx 误当成账单压缩包，
    然后去找「压缩包里的 csv」——永远找不到。
    """
    if not data.startswith(b"PK"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
    except Exception:
        return False
    return any(n.startswith("xl/") for n in names) or "[Content_Types].xml" in names


def open_source(path: str, password: Optional[str] = None) -> tuple[bytes, str]:
    """读取源文件；如果是账单压缩包，自动取出里面的账单文件。

    支付宝/微信的账单经常是以「带密码的 zip」形式下载的，
    所以这里必须支持 zip 解包，否则用户每次都要手动解压一遍。
    返回 (内容字节, 来源说明)。
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"找不到文件：{path}")

    if not zipfile.is_zipfile(path):
        with open(path, "rb") as fh:
            return fh.read(), "直接读取"

    with open(path, "rb") as fh:
        data = fh.read()

    # 本身就是 Office 文档，不是账单压缩包
    if looks_like_xlsx(data):
        return data, "直接读取"

    zf = zipfile.ZipFile(io.BytesIO(data))
    candidates = [
        n for n in zf.namelist()
        if not n.endswith("/") and n.lower().endswith((".csv", ".txt", ".xlsx", ".xlsm"))
    ]
    if not candidates:
        raise ValueError(f"压缩包 {os.path.basename(path)} 里没有找到 csv/xlsx 文件")

    # 先试用户给的密码，再试无密码（有些包不加密）
    passwords: list[Optional[bytes]] = []
    if password:
        passwords.append(password.encode("utf-8"))
    passwords.append(None)

    last_error: Optional[Exception] = None
    for name in candidates:
        for pwd in passwords:
            try:
                content = zf.read(name, pwd=pwd)
                return content, f"压缩包内 {name}"
            except RuntimeError as exc:      # 密码错误 / 需要密码
                last_error = exc
            except Exception as exc:          # 其他损坏情况
                last_error = exc

    raise ValueError(
        f"压缩包 {os.path.basename(path)} 解不开：需要密码，或给定密码不对。"
        f"下载账单时支付宝/微信会让你设置一个密码，用 --password 传进来。"
        f"（原始错误：{last_error}）"
    )


# --------------------------------------------------------------------------
# CSV / TSV
# --------------------------------------------------------------------------

def parse_delimited(text: str) -> list[list[str]]:
    """把 CSV/TSV 文本切成二维表。自动判定分隔符。"""
    head = text[:4000]
    comma = head.count(",")
    tab = head.count("\t")
    delim = "\t" if tab > comma else ","
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    return [[normalize_text(c) for c in row] for row in reader]


# --------------------------------------------------------------------------
# XLSX（纯标准库实现，不引入 openpyxl）
# --------------------------------------------------------------------------

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _col_index(ref: str) -> int:
    """把单元格引用里的列字母转成 0-based 下标。'A1'->0, 'AA3'->26。"""
    letters = re.sub(r"[^A-Za-z]", "", ref).upper()
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def parse_xlsx(data: bytes) -> list[list[str]]:
    """读取 xlsx 第一个工作表，返回二维表。

    自己解析而不是依赖 openpyxl，是为了让整个工具只依赖 Python 标准库——
    本地隐私工具装一堆第三方包，本身就是额外的供应链风险。
    """
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()

        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall(f"{_NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))

        sheets = sorted(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        if not sheets:
            raise ValueError("xlsx 里找不到工作表")

        root = ET.fromstring(zf.read(sheets[0]))

    grid: dict[int, dict[int, str]] = {}
    max_col = 0
    for row in root.iter(f"{_NS}row"):
        r_idx = int(row.get("r", "0")) - 1
        cells: dict[int, str] = {}
        for cell in row.findall(f"{_NS}c"):
            c_idx = _col_index(cell.get("r", ""))
            if c_idx < 0:
                continue
            max_col = max(max_col, c_idx)
            ctype = cell.get("t")
            v = cell.find(f"{_NS}v")
            inline = cell.find(f"{_NS}is")
            if ctype == "s" and v is not None and v.text and v.text.isdigit():
                pos = int(v.text)
                value = shared[pos] if pos < len(shared) else ""
            elif ctype == "inlineStr" and inline is not None:
                value = "".join(t.text or "" for t in inline.iter(f"{_NS}t"))
            elif v is not None:
                value = v.text or ""
            else:
                value = ""
            cells[c_idx] = normalize_text(value)
        if cells:
            grid[r_idx] = cells

    if not grid:
        return []
    max_row = max(grid)
    return [[grid.get(r, {}).get(c, "") for c in range(max_col + 1)] for r in range(max_row + 1)]


# --------------------------------------------------------------------------
# 统一入口 + 表头定位
# --------------------------------------------------------------------------

def load_table(path: str, password: Optional[str] = None):
    """读取任意账单文件 → (二维表, 编码说明, 来源说明)。"""
    data, origin = open_source(path, password)

    if looks_like_xlsx(data):
        return parse_xlsx(data), "xlsx", origin

    text, enc = decode_bytes(data)
    return parse_delimited(text), enc, origin


def extract_declared_count(rows: list[list[str]]) -> Optional[int]:
    """从文件头部的说明文字里抠出「共 N 笔记录」。"""
    for row in rows[:40]:
        for cell in row:
            match = DECLARED_COUNT_RE.search(cell)
            if match:
                return int(match.group(1))
    return None


def find_header_row(rows: list[list[str]], min_fields: int = 5) -> Optional[int]:
    """定位表头行。

    判据：这一行的非空字段数 >= min_fields，且至少命中 2 个表头锚点词。
    要求命中 2 个而不是 1 个，是因为说明文字里也可能出现「金额」这种词
    （例如「收入：27笔 1234元」），单个关键词容易误判。
    """
    for i, row in enumerate(rows[:60]):
        cells = [c for c in row if c]
        if len(cells) < min_fields:
            continue
        joined = "".join(cells)
        hits = sum(1 for anchor in HEADER_ANCHORS if anchor in joined)
        if hits >= 2:
            return i
    return None


def header_names(row: list[str]) -> list[str]:
    """把表头行规范成便于匹配的列名。

    「金额(元)」「金额（元）」「金额 」统一成「金额」，这样别名表不用写一堆变体。
    """
    names = []
    for cell in row:
        name = normalize_text(cell)
        name = re.sub(r"[（(]\s*元\s*[)）]", "", name)
        name = re.sub(r"[（(].*?[)）]", "", name)
        names.append(name.strip())
    return names


def build_index(header: list[str], aliases: dict[str, tuple[str, ...]]) -> dict[str, int]:
    """按别名表反查列下标。同一字段取第一个命中的列。"""
    index: dict[str, int] = {}
    for field, names in aliases.items():
        for i, col in enumerate(header):
            if not col:
                continue
            for name in names:
                if col == name or (name in col and len(name) >= 3):
                    index[field] = i
                    break
            if field in index:
                break
    return index


def cell(row: list[str], index: dict[str, int], field: str) -> str:
    """按字段名取值，越界或缺失返回空串。"""
    pos = index.get(field)
    if pos is None or pos >= len(row):
        return ""
    return row[pos] or ""


def maybe_excel_date(value: str) -> Optional[datetime]:
    """有些导出把时间存成 Excel 序列号，这里兜底转换。"""
    text = normalize_text(value)
    if not re.fullmatch(r"\d{5}(?:\.\d+)?", text):
        return None
    return parse_excel_serial(float(text))
