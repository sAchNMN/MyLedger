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

from . import archive
from .model import normalize_text, parse_excel_serial

# 表头必须至少命中其中一个，否则不认为是表头行
HEADER_ANCHORS = ("交易时间", "交易号", "交易单号", "交易订单号", "金额", "收/支")

# 判定「这一行像表头」的**弱信号**词。
#
# 为什么需要它们（实测踩到）：
#   原来只认 HEADER_ANCHORS 那 6 个词，于是下面这些表头**全部认不出来**
#   （find_header_row 返回 None → 整份文件解析 0 笔 + 报 missing_header）：
#     日期 | 时间 | 摘要 | 发生额 | 余额 | 对方户名 | 流水号 | 渠道 | 附言
#     交易日期 | 交易时间 | 交易摘要 | 交易金额            （只有 4 列）
#     交易日期 | 交易金额 | 对方户名                      （只有 3 列）
#   原因是那些词太短（2 个字），而 `min_fields=5` + 「命中 2 个锚点」
#   把它挡在门外了。银行 PDF 的表头叫法千差万别，不能只认一种。
#
# 下面这些是**列名里高频出现**的词，命中它们说明「这一行是一排列名」
# 而不是「这是一句话」。它们单独不够（说明文字里也会有），
# 必须配合「多数格子短且互不相同」这个条件。
_HEADER_LIKE_WORDS = (
    "日期", "时间", "金额", "对方", "摘要", "类型", "状态", "备注", "附言",
    "用途", "渠道", "方式", "余额", "发生额", "笔数", "序号", "卡号",
    "账户", "户名", "姓名", "编号", "单号", "流水", "标志", "方向",
    "交易", "明细", "商品", "名称", "分类", "商户",
)

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


TABLE_SUFFIXES = (".csv", ".txt", ".xlsx", ".xlsm")
DOCUMENT_SUFFIXES = TABLE_SUFFIXES + (".tsv", ".pdf")


def _read_from_zip(path: str, zf, names: list,
                   password: Optional[str]) -> tuple[bytes, str]:
    """从压缩包里取出第一份能读出来的文档。返回 (字节, 包内路径)。

    **WinZip AES 的包自动降级到 7-Zip**（见 archive.py）。

    为什么必须有这条降级路：实测真有一家银行导出的账单是 AES-256 加密，
    `zipfile` 在 Python 3.13 上完全解不了，而它抛的是一句含糊的
    「Bad password」——把「算法不支持」误报成「密码不对」。
    用户于是反复重输正确密码，永远解不开，最后开始怀疑自己记错了密码。
    报错必须指向真实原因，这是本项目不可违背的一条。
    """
    try:
        return archive.extract_member(path, names, password)
    except archive.ArchiveError:
        raise
    except Exception:
        # archive 模块自己都崩了（不该发生）——退到标准库，
        # 至少保住「传统 ZipCrypto」这条主路径。
        return _read_from_zip_stdlib(zf, names, password)


def _read_from_zip_stdlib(zf, names: list,
                          password: Optional[str]) -> tuple[bytes, str]:
    """纯标准库解法。密码错时按老规矩报错。"""
    passwords: list[Optional[bytes]] = []
    if password:
        passwords.append(password.encode("utf-8"))
    passwords.append(None)

    last_error: Optional[Exception] = None
    for name in names:
        for pwd in passwords:
            try:
                return zf.read(name, pwd=pwd), name
            except RuntimeError as exc:      # 密码错误 / 需要密码
                last_error = exc
            except Exception as exc:          # 其他损坏情况
                last_error = exc

    raise ValueError(
        f"压缩包 {zf.filename} 解不开：需要密码，或给定密码不对。"
        f"下载账单时支付宝/微信会让你设置一个密码，用 --password 传进来。"
        f"（原始错误：{last_error}）"
    )


def open_document(path: str, password: Optional[str] = None,
                  suffixes: tuple = DOCUMENT_SUFFIXES) -> tuple[bytes, str]:
    """读取任意账单源文件，返回 (内容字节, 来源说明)。

    压缩包会被自动解开。suffixes 决定在包里找什么：
        表格链路传 TABLE_SUFFIXES（csv/xlsx），
        PDF 链路传 (".pdf",) —— 银行寄来的账单经常是「一个装 PDF 的 zip」，
        只认表格的话会直接报「压缩包里没有找到 csv/xlsx 文件」。

    裸文件（非压缩包）原样返回，suffixes 不参与判断。
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
        if not n.endswith("/") and n.lower().endswith(tuple(suffixes))
    ]
    if not candidates:
        wanted = "/".join(suffixes)
        raise ValueError(f"压缩包 {os.path.basename(path)} 里没有找到 {wanted} 文件")

    # 传 path 而不是 zf：AES 包要调 7-Zip，它需要的是真实文件路径。
    # zf.filename 在这里是那个 BytesIO 对象，不是路径。
    content, name = _read_from_zip(path, zf, candidates, password)
    return content, f"压缩包内 {name}"


def open_source(path: str, password: Optional[str] = None) -> tuple[bytes, str]:
    """读取源文件；如果是账单压缩包，自动取出里面的**表格**文件。

    支付宝/微信的账单经常是以「带密码的 zip」形式下载的，
    所以这里必须支持 zip 解包，否则用户每次都要手动解压一遍。
    PDF 不在这条链路上（走 bank_pdf），所以只找表格类后缀。
    返回 (内容字节, 来源说明)。
    """
    return open_document(path, password, TABLE_SUFFIXES)


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


def find_header_row(rows: list[list[str]], min_fields: int = 5,
                    aliases: Optional[dict] = None) -> Optional[int]:
    """定位表头行。返回行号，找不到返回 None。

    三级判据（从严到宽依次尝试）
    --------------------------
    1. **锚点词**（最严）：非空字段 >= min_fields，且命中 >= 2 个
       `HEADER_ANCHORS`。要求 2 个而不是 1 个，是因为说明文字里也会出现
       「金额」（例如「收入：27笔 1234元」），单个关键词容易误判。

    2. **别名表**（给了 aliases 时）：这一行按别名表能认出 >= 2 个字段。
       这是**最可靠的宽判据** —— 别名表里有「交易日期/日期/记账日期/
       入账日期」等一整串同义写法，无论银行怎么叫都认得出。
       不需要猜「像不像列名」。

    3. **弱信号词 + 形状**（最宽）：命中 >= 2 个 `_HEADER_LIKE_WORDS`，
       且这一行**多数格子短且互不相同**（表头的特征）。
       「短且互不相同」是关键 —— 说明文字「收入：27笔 1234元」只有一格，
       而表头是「交易日期」「交易时间」这样一堆 2-4 字的短词。

       加上「互不相同」是因为像
       `**********  **********  **********`（掩码后的账号）这种行
       长度一样、内容重复，是数据行不是表头。

    为什么要三级：实测的失败几乎都是「表头认不出来 → 整份文件 0 笔」，
    而报的是 `missing_header`（指向「文件里没有表头」）——
    实际文件**有**表头，只是叫法没被认出来。
    那个报错方向是错的，会让人以为导出坏了。
    """
    if not rows:
        return None

    # ---- 1. 锚点词 ----
    for i, row in enumerate(rows[:60]):
        cells = [c for c in row if c]
        if len(cells) < min_fields:
            continue
        joined = "".join(cells)
        hits = sum(1 for anchor in HEADER_ANCHORS if anchor in joined)
        if hits >= 2:
            return i

    # ---- 2. 别名表 ----
    if aliases:
        for i, row in enumerate(rows[:60]):
            cells = [c for c in row if c]
            if len(cells) < 2:
                continue
            named = [c for c in cells if _matches_any_alias(c, aliases)]
            if len(named) >= 2:
                return i

    # ---- 3. 弱信号词 + 形状 ----
    for i, row in enumerate(rows[:60]):
        cells = [c for c in row if c and c.strip()]
        if len(cells) < 2:
            continue
        joined = "".join(cells)
        hits = sum(1 for w in _HEADER_LIKE_WORDS if w in joined)
        if hits < 2:
            continue
        # 形状判据：多数格子是短词（<= 6 字），且不重复
        short = [c for c in cells if len(c) <= 6]
        if len(short) < max(2, len(cells) * 0.6):
            continue
        if len(set(short)) < len(short) * 0.8:
            continue        # 大量重复 -> 是数据行（账号/金额序列），不是表头
        return i

    return None


def _matches_any_alias(text: str, aliases: dict) -> bool:
    """这个格子像不像某个字段的列名。

    刻意要求**基本相等或互为前缀/后缀的短词**，而不是随便包含。
    实测踩到的误判：

        ['户名：TEST', '币种：人民币', '起止日期：2026-09-01～2026-09-30',
         '账户：ACCT-TEST-001', '电子流水号：LOG-TEST-001']

    这一行是账户信息，**不是表头**，但按「子串包含」判：
        '起止日期：2026-09-01～2026-09-30'  含 '日期'   -> 算命中
        '电子流水号：LOG-TEST-001'            含 '流水号' -> 算命中
    两个就够「>= 2」，于是这行被当成表头 —— 而它后面一行才是真表头，
    真表头反而被跳过（`find_header_row` 只返回第一个命中）。

    修法：**带分隔符的「键：值」形式直接否掉**。
    列名是「交易日期」，不长这样；「起止日期：2026-09-01」是数据。
    """
    t = normalize_text(text)
    if not t:
        return False
    # 「键：值」形态是数据行（账户信息、说明文字），不是列名。
    # 全角冒号也认 —— 银行 PDF 里两种都有。
    for sep in ("：", ":"):
        if sep in t:
            return False
    for names in aliases.values():
        for n in names:
            if t == n:
                return True
            # 「交易日期明细」这种带后缀的也算，但要求**短**——
            # 太长的匹配基本不会是列名。
            if len(n) >= 3 and (t.startswith(n) or t.endswith(n)) and len(t) <= 8:
                return True
    return False
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
