"""加密 PDF 账单解析（PyMuPDF）。

为什么 PDF 比截图靠谱一个量级：
    截图要靠 OCR 猜字形，糊掉一个字符金额就错，而且日期列经常整列读不出来。
    PDF 里的文字是**结构化存储**的，抽出来是精确的字符 + 精确的坐标，
    没有识别误差。所以这条链路是可靠的，截图那条不是。

为什么用 PyMuPDF 而不是 pypdf：
    pypdf 只给「一段文本」，拿不到每个词的坐标，还原不了「哪个金额属于哪个商户」。
    PyMuPDF 的 page.get_text("words") 给出 (x0, y0, x1, y1, 文本, 块, 行, 词)，
    这正是按行聚类、按列拼接所需要的东西。

隐私边界（本模块的设计前提）：
    本模块只做「PDF 字节 → 二维表」的搬运，**不打印任何内容**。
    调用方（导入器）也只统计笔数，不输出金额与商户名。
    要看真实内容请自己用 --dry-run，不要让内容进入任何外部工具的上下文。
"""

from __future__ import annotations

import os
import zipfile
from typing import Optional


class PdfError(RuntimeError):
    """PDF 打不开、需要密码、密码错、或者是扫描件（没有文字层）。"""


def _require_pymupdf():
    try:
        import pymupdf  # noqa: F401
        return pymupdf
    except ImportError:
        try:
            import fitz as pymupdf  # 老名字，PyMuPDF < 1.24
            return pymupdf
        except ImportError as exc:
            raise PdfError(
                "没有可用的 PDF 解析库。PDF 这条路需要 PyMuPDF 或 pypdf。\n"
                "  安装（装进隔离环境，不污染系统 Python）：\n"
                "    <venv>\\Scripts\\python.exe -m pip install pymupdf pypdf"
            ) from exc


def _cluster_rows(words, row_tolerance: float = 0.6, gap_ratio: float = 0.8):
    """把词聚成行，再把行内的词按 x 间距拼成字段。

    和截图那条链路用的是同一套几何逻辑（按 y 聚行、按 x 间距分列），
    区别只在于这里的词是精确的、不是猜出来的。
    """
    if not words:
        return []

    items = []
    for w in words:
        x0, y0, x1, y1, text = w[0], w[1], w[2], w[3], w[4]
        text = (text or "").strip()
        if not text:
            continue
        items.append({
            "text": text,
            "left": float(x0),
            "right": float(x1),
            "width": float(x1) - float(x0),
            "height": float(y1) - float(y0),
            "cy": (float(y0) + float(y1)) / 2.0,
        })
    if not items:
        return []

    heights = sorted(i["height"] for i in items)
    median_h = heights[len(heights) // 2] or 10.0
    tol = max(2.0, median_h * row_tolerance)

    items.sort(key=lambda i: i["cy"])
    rows = []
    for item in items:
        placed = False
        for row in rows:
            if abs(row["cy"] - item["cy"]) <= tol:
                row["items"].append(item)
                row["cy"] = sum(i["cy"] for i in row["items"]) / len(row["items"])
                placed = True
                break
        if not placed:
            rows.append({"cy": item["cy"], "items": [item]})

    widths = sorted(i["width"] for i in items if i["width"] > 0)
    median_w = widths[len(widths) // 2] if widths else 8.0

    # ------------------------------------------------------------------
    # 拼接阈值：用**行高**当基准，不要用「行内间距中位数」。
    #
    # 中位数试过，是错的。实测某一行内相邻词的间距是 8、10、30、35、40，
    # 中位数 30 把阈值抬到 12，结果只差 8pt 的两列被粘住了：
    # 日期和时间合成一格、金额和余额合成一格，整行报废。
    # 根因是排版里列宽不均，宽的那几列把中位数拽高了。
    #
    # 物理模型更可靠：格内空格的宽度 ≈ 0.3 倍字高，而列间距通常远大于此。
    # 用字高当基准不依赖列宽分布，换一种排版也不用改代码。
    # ------------------------------------------------------------------
    merge_threshold = max(1.0, median_h * 0.4)

    out = []
    for row in sorted(rows, key=lambda r: r["cy"]):
        seq = sorted(row["items"], key=lambda i: i["left"])
        cells, buf, prev_right = [], "", None
        for item in seq:
            if prev_right is not None:
                gap = item["left"] - prev_right
                # 两种情况都要分列：
                #   间距明显偏大        -> 换了列
                #   起点落在前一个词里面 -> 排版重叠（或列宽算错），必须分开，
                #                          否则负间距会被判成「同格」而粘成一坨
                if gap > merge_threshold or gap < -merge_threshold:
                    if buf:
                        cells.append(buf)
                        buf = ""
            buf += item["text"]
            prev_right = item["right"]
        if buf:
            cells.append(buf)
        if cells:
            out.append(cells)
    return out


def extract_rows(path: str, password: Optional[str] = None) -> tuple[list[list[str]], dict]:
    """抽出 (二维表, 元信息)。元信息里不含任何账单内容。

    path 可以是裸 PDF，也可以是「装着 PDF 的 zip」——
    银行寄来的账单经常是后者，那种情况下 pymupdf.open(path)
    会安静地开出一个 0 页的空文档（实测），比直接报错更坏：
    后面会因为「抽不到文字层」误判成扫描件。
    所以这里先判断是不是压缩包，是就取出里面的 PDF 字节再开。
    """
    pymupdf = _require_pymupdf()

    if not os.path.isfile(path):
        raise PdfError(f"找不到文件：{path}")

    if zipfile.is_zipfile(path):
        from .sniff import open_document
        try:
            data, origin = open_document(path, password, (".pdf",))
        except Exception as exc:
            raise PdfError(
                f"这个压缩包里取不出 PDF：{exc}\n"
                f"  确认它是银行寄来的账单包（里面应该是 .pdf），"
                f"并且密码填对了。"
            ) from exc
        try:
            doc = pymupdf.open(stream=data, filetype="pdf")
        except Exception as exc:
            raise PdfError(f"压缩包里的 PDF 打不开：{exc}") from exc
    else:
        try:
            doc = pymupdf.open(path)
        except Exception as exc:
            raise PdfError(f"打不开 PDF：{exc}") from exc

    meta = {
        "pages": doc.page_count,
        "encrypted": bool(getattr(doc, "needs_pass", False)),
        "authenticated": True,
        "text_layer": True,
    }

    if getattr(doc, "needs_pass", False):
        if not password:
            doc.close()
            raise PdfError(
                "这个 PDF 有密码。请在提示里输入（用完即忘，不保存、不落盘）。"
            )
        try:
            ok = doc.authenticate(password)
        except Exception as exc:
            doc.close()
            raise PdfError(f"密码校验失败：{exc}") from exc
        if not ok:
            doc.close()
            meta["authenticated"] = False
            raise PdfError("PDF 密码不对。")

    rows: list[list[str]] = []
    try:
        for page in doc:
            try:
                words = page.get_text("words")
            except Exception:
                words = []
            rows.extend(_cluster_rows(words))
    finally:
        doc.close()

    # 一页都没有文字层 —— 这是扫描件，得走 OCR，不是 PDF 文本那条路
    if not rows:
        meta["text_layer"] = False
        raise PdfError(
            "这个 PDF 里抽不到任何文字，说明它是**扫描件**（图片，没有文字层）。\n"
            "  扫描件要走 OCR 那条路：先把 PDF 的每页导出成图片，再用截图流程处理。\n"
            "  有文字层的 PDF（银行直接导出的账单通常是）不会有这个问题。"
        )

    return rows, meta
