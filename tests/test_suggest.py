"""未分类商户的「建议分类」测试。

这里钉的核心不是「建议得准不准」，而是三件事：

    1. **照抄已知的同店**：名字包含关系 / 整体相似，都能给出建议
    2. **没有依据就不猜**。这是最要紧的一条 ——
       一个看似合理的错误答案，比空白危险得多：
       人对着「建议：餐饮」点一下头就过去了，而空白会让他真的想一想。
    3. **AI 永远排在人已知的信息之后**，且置信度压得比本地依据低。
       AI 可能胡说，人是照抄，两者不该同权。

用法：<venv>\\Scripts\\python.exe tests/test_suggest.py
返回码 0 表示全部通过。
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from importer import suggest  # noqa: E402
from importer.categorize import DEFAULT_CATEGORY, normalize_text  # noqa: E402
from importer.model import DIRECTION_EXPENSE, Txn  # noqa: E402
from importer.store import Ledger  # noqa: E402

FAILURES: list[str] = []
BASE = dt.datetime(2026, 9, 3, 12, 0, 0)


def check(name: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: 期望 {want!r}，实际 {got!r}")
    print(f"  [{'✓' if ok else '✗'}] {name:<46} -> {got!r}")


def fresh_ledger() -> Ledger:
    ledger = Ledger(sqlite3.connect(":memory:"), ":memory:")
    ledger.start_batch("suggest-test", "synthetic.csv", parsed=0)
    return ledger


def add(ledger, counterparty: str, category: str, amount: str = "10.00",
        direction: str = DIRECTION_EXPENSE) -> None:
    """插一笔，然后强行指定它的分类（跳过规则，测试只关心建议逻辑）。"""
    ledger.add(Txn(source="alipay", ts=BASE, amount=Decimal(amount),
                   direction=direction, counterparty=counterparty, item=""))
    ledger.conn.execute(
        "UPDATE txn SET category = ? WHERE counterparty = ?",
        (category, counterparty))
    ledger.conn.commit()


def run() -> int:
    print()
    print("=" * 78)
    print("1. 同一家店的不同写法：照抄已知的")
    print("=" * 78)
    ledger = fresh_ledger()
    add(ledger, "美团外卖", "餐饮", amount="30.00")
    add(ledger, "美团外卖（北京分公司）", DEFAULT_CATEGORY, amount="40.00")
    add(ledger, "星巴克咖啡", "餐饮", amount="35.00")
    add(ledger, "星巴克", DEFAULT_CATEGORY, amount="25.00")

    items = suggest.build_suggestions(ledger)
    by_name = {i.merchant: i for i in items}
    # 商户名在库里是归一化后的（全角括号会变半角），查找时同样归一
    branch = normalize_text("美团外卖（北京分公司）")
    check("两个未分类的都挑出来了", sorted(by_name),
          sorted([branch, normalize_text("星巴克")]))

    meituan = by_name[branch]
    check("分公司的建议来自总店", meituan.category, "餐饮")
    check("来源是子串关系", meituan.source, suggest.SOURCE_CONTAINS)
    check("依据里点明了参照的是谁", "美团外卖" in meituan.evidence, True)

    starbucks = by_name[normalize_text("星巴克")]
    check("简称也能对应上", starbucks.category, "餐饮")
    check("简称走的是子串（不是相似度）", starbucks.source, suggest.SOURCE_CONTAINS)

    # 错别字的场景才轮到相似度：「万佳」和「万家」只差一个字，
    # 不是子串关系，整体相似又能过门槛。
    ledger2 = fresh_ledger()
    add(ledger2, "华润万家", "日用百货", amount="88.00")
    add(ledger2, "华润万佳", DEFAULT_CATEGORY, amount="66.00")
    typo = suggest.build_suggestions(ledger2)[0]
    check("错别字也能对上", typo.category, "日用百货")
    check("走的是相似度", typo.source, suggest.SOURCE_SIMILAR)
    check("置信度就是相似度", 0.72 <= typo.confidence <= 1.0, True)

    print()
    print("=" * 78)
    print("2. 没依据就不猜（最要紧的一条）")
    print("=" * 78)
    ledger = fresh_ledger()
    add(ledger, "美团外卖", "餐饮", amount="30.00")
    add(ledger, "完全不相干的一个名字", DEFAULT_CATEGORY, amount="99.00")
    add(ledger, "F", DEFAULT_CATEGORY, amount="5.00")

    items = {i.merchant: i for i in suggest.build_suggestions(ledger)}
    check("不相干的名字没有建议", items["完全不相干的一个名字"].category, "")
    check("来源标成 none", items["完全不相干的一个名字"].source, suggest.SOURCE_NONE)
    check("依据为空（不编理由）", items["完全不相干的一个名字"].evidence, "")
    check("单字母也没有建议", items["F"].category, "")

    print()
    print("=" * 78)
    print("3. AI 排在已知信息之后，且置信度更低")
    print("=" * 78)
    ledger = fresh_ledger()
    add(ledger, "美团外卖", "餐饮", amount="30.00")
    add(ledger, "美团外卖分公司", DEFAULT_CATEGORY, amount="40.00")
    add(ledger, "看不懂的公司名", DEFAULT_CATEGORY, amount="60.00")

    ai_map = {"美团外卖分公司": "数码电子",      # AI 胡说
              "看不懂的公司名": "数码电子"}
    items = {i.merchant: i for i in suggest.build_suggestions(ledger, ai_map=ai_map)}

    check("库里有依据时，AI 的话不算数",
          items["美团外卖分公司"].category, "餐饮")
    check("库里没依据时才用 AI",
          items["看不懂的公司名"].category, "数码电子")
    check("AI 建议标成 ai 来源", items["看不懂的公司名"].source, suggest.SOURCE_AI)
    check("AI 的置信度低于本地依据",
          items["看不懂的公司名"].confidence
          < items["美团外卖分公司"].confidence, True)

    print()
    print("=" * 78)
    print("4. AI 给出的不是合法分类就丢掉")
    print("=" * 78)
    ledger = fresh_ledger()
    add(ledger, "另一个公司", DEFAULT_CATEGORY, amount="60.00")
    items = {i.merchant: i for i in
             suggest.build_suggestions(ledger, ai_map={"另一个公司": "不存在的分类"})}
    check("非法分类不采纳", items["另一个公司"].category, "")

    print()
    print("=" * 78)
    print("5. 排序：金额大的先来")
    print("=" * 78)
    ledger = fresh_ledger()
    add(ledger, "小笔", DEFAULT_CATEGORY, amount="1.00")
    add(ledger, "大笔", DEFAULT_CATEGORY, amount="900.00")
    add(ledger, "中笔", DEFAULT_CATEGORY, amount="50.00")
    order = [i.merchant for i in suggest.build_suggestions(ledger)]
    check("按金额倒序", order, ["大笔", "中笔", "小笔"])

    print()
    print("=" * 78)
    print("6. 已经归类的不会出现在这个清单里")
    print("=" * 78)
    ledger = fresh_ledger()
    add(ledger, "已归类的店", "餐饮", amount="20.00")
    add(ledger, "没归类的店", DEFAULT_CATEGORY, amount="20.00")
    check("只有没归类的被列出来",
          [i.merchant for i in suggest.build_suggestions(ledger)], ["没归类的店"])

    print()
    print("=" * 78)
    print("7. 商品名样本：取信息量最多的那个")
    print("=" * 78)
    ledger = fresh_ledger()
    add(ledger, "脱敏了的对方", DEFAULT_CATEGORY, amount="12.00")
    ledger.conn.execute("UPDATE txn SET item = '转账' WHERE counterparty = '脱敏了的对方'")
    ledger.add(Txn(source="alipay", ts=BASE, amount=Decimal("9.00"),
                   direction=DIRECTION_EXPENSE,
                   counterparty="脱敏了的对方", item="餐饮美食-午餐"))
    ledger.conn.commit()
    item = suggest.build_suggestions(ledger)[0]
    check("带上了商品名", item.sample_item, "餐饮美食-午餐")
    check("取的是最长的那个（更有信息量）", len(item.sample_item) > 2, True)

    print()
    print("=" * 78)
    print("7. 商户自己的历史归类就是依据（不会因为「跳过自己」而漏掉）")
    print("=" * 78)
    # 真实账本里就有这种：同一个商户，早先某批被规则认出来了，
    # 后面某批没认出来 —— 于是它「既已归类又未分类」，答案就在库里。
    #
    # 用裸 SQL 而不是 add()：那个助手按商户名整体覆盖分类，
    # 造不出「同一名下既有已归类、又有未分类」的混杂状态。
    ledger = fresh_ledger()
    ledger.conn.executemany(
        "INSERT INTO txn (source, ts, amount, direction, counterparty,"
        " item, category, review, fingerprint) VALUES (?,?,?,?,?,?,?,?,?)",
        [("alipay", "2026-09-01 12:00:00", "30.00", "expense",
          "反复出现的店", "午餐", "餐饮", "", "fp-self-1"),
         ("alipay", "2026-09-11 12:00:00", "31.00", "expense",
          "反复出现的店", "晚餐", DEFAULT_CATEGORY, "", "fp-self-2")])
    ledger.conn.commit()

    items = suggest.build_suggestions(ledger)
    check("只挑出这一个商户", len(items), 1)
    item = items[0]
    check("挑出了这一个商户", item.merchant, normalize_text("反复出现的店"))
    check("笔数是 1（只数未分类的）", item.count, 1)
    check("建议来自它自己的历史", item.category, "餐饮")
    check("来源是 self", item.source, suggest.SOURCE_SELF)
    check("self 比子串命中更可信", item.confidence > suggest.CONF_CONTAINS, True)
    # count 数的是**已归类**的笔数（未分类的行根本没进 known），这里是 1 笔
    check("依据写明有多少笔", "1 笔" in item.evidence, True)

    print()
    print("=" * 78)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(run())
