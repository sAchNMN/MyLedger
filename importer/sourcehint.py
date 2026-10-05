"""渠道记忆：把「这个形状的文件是哪个渠道」记下来，下次自动认。

为什么需要这一层：
    `detect_source` 认的是**形状**（后缀、压缩包里的后缀、CSV 表头关键词）。
    总有认不出来的：文件名是一串数字、压缩包里是银行自己导出的固定格式、
    表头用的是银行内部叫法而不是「交易时间/金额」那套词。
    这时只有人知道答案。

    而「让人每次都手动指定」是不可持续的：一个月一次还好，
    一年下来你就会开始跳过这一步，然后文件一直躺在投递目录里，
    红点一直亮着，你也已经习惯了红点。

所以这里存一条「形状 → 渠道」的映射，指定一次，以后同类文件自动认。

为什么键是「形状」而不是文件名：
    银行的账单文件名是 `26100501103672708650.zip` 这种流水号，
    下个月一定变。按文件名记，等于每个月都要重新指定一次，
    记忆就没有意义了。形状（`zip:pdf`）才是稳定的那个维度。

它为什么比改detect_source 的启发式安全：
    启发式是全局的、猜的；这里是用户明确指定的一条映射，
    只影响形状完全相同的文件，不会吸走别的渠道。
    跟 categorize 的「人工指定压过规则」是同一个设计取向。

存哪儿：跟着账本一起进加密库（store.py 的 SCHEMA 里建表），
    所以既不落在仓库里（不进 Git），也享受明文不落盘。
"""

from __future__ import annotations

import os
import zipfile
from typing import Optional

# 形状键的取值范围。写死在这里是为了让 --list 的输出可枚举、可校验。
SHAPE_EXT = "ext"          # ext:csv / ext:xlsx / ext:pdf
SHAPE_ZIP = "zip"          # zip:pdf / zip:csv
SHAPE_NAME = "name"        # name:某个具体文件名（兜底，形状也认不出来时）

# 能当渠道记的。跟 sources.PARSERS 的键一致。
KNOWN_SOURCES = ("alipay", "wechat", "bank_ocr", "bank_pdf")


def shape_key(path: str) -> str:
    """算出这个文件的「形状键」。同一形状的文件得到同一个键。

    优先级：压缩包看里面装什么 > 单文件看后缀 > 都不认就退回文件名。
    退回文件名是刻意的：宁可窄一点（只对这一个文件生效），
    也不要猜一个宽的形状然后吸走别的渠道的文件。
    """
    name = os.path.basename(path).lower()

    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as zf:
                inner = [
                    os.path.splitext(n)[1].lstrip(".").lower()
                    for n in zf.namelist()
                    if not n.endswith("/")
                ]
        except Exception:
            inner = []
        # 出现最多的那种后缀算这个包的形状。银行包通常只有一种。
        if inner:
            top = max(set(inner), key=inner.count)
            if top:
                return f"{SHAPE_ZIP}:{top}"
        return f"{SHAPE_NAME}:{name}"

    ext = os.path.splitext(name)[1].lstrip(".")
    if ext:
        return f"{SHAPE_EXT}:{ext}"
    return f"{SHAPE_NAME}:{name}"


# --------------------------------------------------------------------------
# 读写
# --------------------------------------------------------------------------

def hint_map(ledger) -> dict[str, str]:
    """读出全部「形状 → 渠道」。键是形状键。"""
    return {
        row["hint_key"]: row["source"]
        for row in ledger.conn.execute("SELECT hint_key, source FROM source_hint")
    }


def resolve(ledger, path: str) -> Optional[str]:
    """这个文件有没有被用户指定过渠道。返回渠道名或 None。"""
    row = ledger.conn.execute(
        "SELECT source FROM source_hint WHERE hint_key = ?", (shape_key(path),)
    ).fetchone()
    return row["source"] if row else None


def set_hint(ledger, key: str, source: str) -> str:
    """记一条「形状 → 渠道」。返回确认后的键。"""
    if source not in KNOWN_SOURCES:
        raise ValueError(
            f"不认识的渠道「{source}」。可用：{'、'.join(KNOWN_SOURCES)}"
        )
    if not key or ":" not in key:
        raise ValueError(
            f"形状键格式不对：「{key}」。应该是 ext:csv / zip:pdf / name:xxx.zip"
        )
    ledger.conn.execute(
        "INSERT INTO source_hint (hint_key, source, decided_at) VALUES (?, ?, ?) "
        "ON CONFLICT(hint_key) DO UPDATE SET source = excluded.source, "
        "decided_at = excluded.decided_at",
        (key, source, _now()),
    )
    ledger.conn.commit()
    return key


def clear_hint(ledger, key: str) -> bool:
    """撤销一条记忆。返回是否确实删掉了一行。"""
    cur = ledger.conn.execute("DELETE FROM source_hint WHERE hint_key = ?", (key,))
    ledger.conn.commit()
    return cur.rowcount > 0


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
