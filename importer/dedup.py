"""去重：三层判定，只有第一层允许自动丢弃。

为什么不让 AI 判断重复：
    「这两笔是不是同一笔」是个确定性判断问题——金额、时间、单号都是精确值。
    让概率模型去决定删哪条数据，既不可复现也不可解释，出差错时你连为什么都不知道。
    这里只用可复现的规则，AI 的能力留给真正模糊的问题（商户→分类）。

三层的分工：

    L1  同源 + 同交易单号             → identical  ：自动跳过（重复导入同一文件是幂等的）
    L2  时间(到分)+金额+对方 完全一致  → suspect_exact：标记待确认，不删
    L3  金额相同 + 时间接近 + 对方相似 → suspect_near ：标记待确认，不删

L2/L3 之所以只标记不删：跨渠道的同一笔支出（支付宝扣款 + 银行卡流水）
和「同一分钟在同一家店买了两杯咖啡」在数据上无法区分。
机器没有资格替你判断，所以把判断权交回给你。
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal, Optional

from .model import SOURCE_LABELS, Txn, normalize_text


def _label(source: str) -> str:
    return SOURCE_LABELS.get(source, source)

Verdict = Literal["new", "identical", "suspect_exact", "suspect_near"]

# L3 的时间窗：同一笔交易在不同渠道的记账时间可能差几十秒到几分钟
NEAR_WINDOW_SECONDS = 300
# L3 的对方名称相似度下限
NEAR_RATIO_THRESHOLD = 0.60


@dataclass
class DedupResult:
    verdict: Verdict
    reason: str = ""
    existing_id: Optional[int] = None
    existing_desc: str = ""

    @property
    def is_duplicate_skip(self) -> bool:
        return self.verdict == "identical"

    @property
    def needs_review(self) -> bool:
        return self.verdict in ("suspect_exact", "suspect_near")


def _similarity(a: str, b: str) -> float:
    """归一化后算字符串相似度。确定性算法，同输入必同输出。"""
    na, nb = normalize_text(a).lower(), normalize_text(b).lower()
    if not na and not nb:
        return 1.0
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def classify(txn: Txn, ledger) -> DedupResult:
    """判定一笔新交易与已有数据的关系。

    ledger 需要提供三个查询：
        by_match_key(key)      -> 记录或 None
        by_fingerprint(fp)     -> 记录列表
        near(ts, amount, win)  -> 记录列表
    """
    # ---------------- L1：同源同单号 ----------------
    key = txn.match_key()
    if key:
        hit = ledger.by_match_key(key)
        if hit:
            return DedupResult(
                verdict="identical",
                reason=f"同一交易单号已存在（{key}）",
                existing_id=hit["id"],
                existing_desc=ledger.describe(hit),
            )

    # ---------------- L2：内容指纹完全一致 ----------------
    for hit in ledger.by_fingerprint(txn.fingerprint()):
        if hit["match_key"] and hit["match_key"] == key:
            continue
        return DedupResult(
            verdict="suspect_exact",
            reason=(
                f"时间(到分)、金额、交易对方与已有记录完全一致，"
                f"但单号不同（{_label(txn.source)} vs {_label(hit['source'])}）"
            ),
            existing_id=hit["id"],
            existing_desc=ledger.describe(hit),
        )

    # ---------------- L3：金额相同 + 时间接近 + 对方相似 ----------------
    for hit in ledger.near(txn.ts, txn.amount, NEAR_WINDOW_SECONDS):
        ratio = _similarity(txn.counterparty, hit["counterparty"])
        if ratio >= NEAR_RATIO_THRESHOLD:
            delta = abs((txn.ts - hit["ts_dt"]).total_seconds())
            return DedupResult(
                verdict="suspect_near",
                reason=(
                    f"金额相同、时间相差 {int(delta)} 秒、"
                    f"交易对方相似度 {ratio:.0%}（{_label(hit['source'])}）"
                ),
                existing_id=hit["id"],
                existing_desc=ledger.describe(hit),
            )

    return DedupResult(verdict="new")
