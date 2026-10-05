"""未分类商户的**建议分类**（人机协作，不是自动分类）。

为什么需要这一层
----------------
`unmatched` 能列出没归类的商户，但归位要自己想、自己敲：
50 个商户 = 50 次「这是什么店 → 属于哪个分类 → 敲 setcat」。

而这活儿里有一大半其实**已经有答案了**：
账本里那些已归类的交易，很多和未分类的商户是同一家店的不同写法
（「美团外卖」/「美团」/「北京美团科技」）。这一类不需要模型，
也不需要人想 —— 照抄已知的就行。

所以这里是**给建议 + 人来拍板**，而不是「自动分类」：
建议只是把「已经存在的信息」搬到眼前，决定权永远在人手里。
换句话说，它减少的是**打字次数**，不是**判断次数**。

来源与可信度
------------
1. `contains`  一家名字是另一家的子串（至少 2 个字）→ 最可信。
                中文短名的编辑距离很不可靠，子串关系比相似度稳妥得多。
2. `similar`    整体相似（difflib ratio）→ 次之，ratio 即置信度。
3. `ai`         本地模型 → 置信度**刻意压到 0.6**。
                它可能胡说，所以永远排在人已知的信息之后。

没有建议就是没有建议，绝不瞎猜一个凑数 —— 宁可让人自己填，
也不能让人对着一个看似合理的错误答案点头。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from difflib import SequenceMatcher

from .categorize import CATEGORIES, DEFAULT_CATEGORY, normalize_text

# 子串匹配要求的最短长度。1 个字太容易误伤（「美」会命中「美团」和「美容」），
# 2 个字是中文商户名里能表达「同一家店」的最小单位。
MIN_CONTAINS_LEN = 2

# 相似度门槛。低于这个值就当不认识。
MIN_RATIO = 0.72

# 三种来源的置信度。AI 刻意最低 —— 见模块注释。
CONF_CONTAINS = 0.92
CONF_AI = 0.6

SOURCE_CONTAINS = "contains"
SOURCE_SIMILAR = "similar"
SOURCE_AI = "ai"
SOURCE_NONE = "none"


@dataclass
class Suggestion:
    """一个未分类商户的建议。

    evidence 是给人看的依据 —— **没有依据的建议等于没有建议**。
    人拍板之前得知道这个分类是从哪儿来的，否则就成了盲从。
    """

    merchant: str
    count: int
    expense: float
    income: float
    category: str = ""
    source: str = SOURCE_NONE
    confidence: float = 0.0
    evidence: str = ""
    sample_item: str = ""

    @property
    def total(self) -> float:
        return round(self.expense + self.income, 2)


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def known_merchants(ledger) -> dict[str, str]:
    """库里**已经归类**的商户 → 分类。

    只看 txn 表里 category 不是「其他」的那些：那是实际生效的判定结果，
    比 merchant_category 缓存表更全（后者只记人工/AI/规则命中的项）。
    """
    out: dict[str, str] = {}
    for row in ledger.conn.execute(
            "SELECT counterparty, category FROM txn"):
        merchant = normalize_text(row["counterparty"] or "")
        category = row["category"] or ""
        if not merchant or category in ("", DEFAULT_CATEGORY):
            continue
        if merchant not in out:
            out[merchant] = category
    return out


def unclassified(ledger) -> list[dict]:
    """还没归类的商户，按金额倒序 —— 大头先看。

    顺带记下**商品名**的一个样本。为什么要有它：
    平台导出的「对方」字段经常是「****8」「x**」「发给某人」这种没法认的东西，
    而商品名那一栏往往写着「餐饮美食」「地铁出行」—— 人一看就知道是什么。
    这条信息一直躺在库里没人用，而它恰恰是判断分类时最好用的那一条。

    取最长的那个样本：短商品名多半是「转账」这种没信息量的词。
    """
    rows: dict[str, dict] = {}
    for row in ledger.conn.execute(
            "SELECT counterparty, amount, direction, category, item FROM txn"):
        category = row["category"] or ""
        if category not in ("", DEFAULT_CATEGORY):
            continue
        merchant = normalize_text(row["counterparty"] or "")
        if not merchant:
            continue
        bucket = rows.setdefault(
            merchant, {"merchant": merchant, "count": 0,
                       "expense": 0.0, "income": 0.0, "item": ""})
        bucket["count"] += 1
        amount = float(row["amount"] or 0)
        if row["direction"] == "expense":
            bucket["expense"] += amount
        else:
            bucket["income"] += amount
        item = (row["item"] or "").strip()
        if len(item) > len(bucket["item"]):
            bucket["item"] = item
    return sorted(rows.values(),
                  key=lambda b: (-(b["expense"] + b["income"]), b["merchant"]))


def _guess_from_known(merchant: str, known: dict[str, str]) -> tuple[str, str, float, str]:
    """在已归类的商户里找依据。返回 (分类, 来源, 置信度, 依据)。

    先找子串关系，再退到整体相似度 —— 顺序不能反：
    「美团」和「美团外卖科技有限公司」的 ratio 可能只有 0.5（长度差太多），
    但它们是同一家店；反过来两个长度接近的不同品牌 ratio 能到 0.75。
    """
    best: tuple[str, str, float, str] = ("", SOURCE_NONE, 0.0, "")

    for other, category in known.items():
        if other == merchant:
            continue

        # 1) 子串：谁包含谁都算，但必须够长
        shorter, longer = (merchant, other) if len(merchant) <= len(other) \
            else (other, merchant)
        if len(shorter) >= MIN_CONTAINS_LEN and shorter in longer:
            return (category, SOURCE_CONTAINS, CONF_CONTAINS,
                    f"名字里含已归类的「{other}」（{category}）")

        # 2) 整体相似
        ratio = _ratio(merchant, other)
        if ratio >= MIN_RATIO and ratio > best[2]:
            best = (category, SOURCE_SIMILAR, ratio,
                    f"和已归类的「{other}」（{category}）很像，相似度 {ratio:.2f}")

    return best


def build_suggestions(ledger, ai_map: dict[str, str] | None = None) -> list[Suggestion]:
    """给每个未分类商户一个建议。

    ai_map 是外部（本地模型）给出的 {商户: 分类}。
    **只有在库里找不到依据时才用它** —— 人已知的信息永远优先于模型猜测。
    """
    known = known_merchants(ledger)
    ai_map = ai_map or {}

    out: list[Suggestion] = []
    for bucket in unclassified(ledger):
        merchant = bucket["merchant"]
        item = Suggestion(merchant=merchant, count=bucket["count"],
                          expense=round(bucket["expense"], 2),
                          income=round(bucket["income"], 2),
                          sample_item=bucket.get("item", ""))

        category, source, conf, evidence = _guess_from_known(merchant, known)
        if not category and merchant in ai_map:
            # 模型可能给出不在分类表里的值（幻觉），直接丢掉 ——
            # 把「不存在的分类」写进库里，后面每个环节都会跟着错。
            guess = ai_map[merchant]
            if guess in CATEGORIES:
                category, source, conf = guess, SOURCE_AI, CONF_AI
                evidence = "本地模型猜的（可信度一般，请自己判断）"

        if category:
            item.category, item.source = category, source
            item.confidence, item.evidence = conf, evidence
        out.append(item)
    return out


def with_ai(ledger, use_ai: bool = False, ai_url: str = "",
            ai_model: str = "") -> list[Suggestion]:
    """要 AI 就在这里接。Ollama 没装/没响应就退回纯本地建议，不报错。"""
    suggestions = build_suggestions(ledger)
    if not use_ai:
        return suggestions

    pending = [s.merchant for s in suggestions if not s.category]
    if not pending:
        return suggestions

    try:
        from .categorize import ai_available, ai_classify
        if ai_available(ai_url):
            mapping = ai_classify(pending, url=ai_url, model=ai_model)
        else:
            mapping = {}
    except Exception:                        # noqa: BLE001 - AI 是增强项，不能拖垮主流程
        mapping = {}

    return build_suggestions(ledger, ai_map=mapping)
