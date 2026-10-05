"""平台自带分类 + 交易状态判定的回归测试。

为什么必须单独写这个文件：
    这两条判定都是**「漏了不会报错」**型的。

    · 平台分类不参与判定，交易照样入库、报表照样出，
      只是分类悄悄退回到关键词规则 —— 你看不出少了什么。
    · 状态不参与判定更隐蔽：钱没花出去的交易被算进支出，
      数字只会偏大一点点，趋势图上根本看不出来。

    而且这两条都踩在一个共同的反直觉点上：**平台给的信息比我们的规则准，
    但平台的某些值又完全没信息量**（微信「商户消费」占 90% 流水、
    支付宝「其他」）。所以真正要钉住的是「什么时候该信平台、
    什么时候必须不信」—— 只测「映射表能查到东西」是不够的。

用法：<venv>\\Scripts\\python.exe tests/test_platform_category.py
返回码 0 表示全部通过。
"""

from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from importer import fixups, platformmap  # noqa: E402
from importer.categorize import CATEGORIES, categorize  # noqa: E402
from importer.model import (  # noqa: E402
    DIRECTION_EXPENSE,
    DIRECTION_INCOME,
    DIRECTION_NEUTRAL,
    STATUS_FAILED,
    STATUS_KILLS_EXPENSE,
    STATUS_OK,
    STATUS_PENDING,
    STATUS_REFUNDED,
    STATUS_UNKNOWN,
    Txn,
    normalize_text,
    status_verdict,
)
from importer.sources import parse_alipay  # noqa: E402
from importer.store import Ledger  # noqa: E402

FAILURES: list[str] = []
FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "fixtures")


def check(name: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: 期望 {want}，实际 {got}")
    print(f"  [{'✓' if ok else '✗'}] {name:<58} -> {got}")


def section(title: str) -> None:
    print()
    print("=" * 80)
    print(title)
    print("=" * 80)


def fresh_ledger() -> Ledger:
    led = Ledger(sqlite3.connect(":memory:"), ":memory:")
    led.start_batch("test", "test-batch", parsed=0)
    return led


def quiet(*_a, **_k) -> None:
    """吞掉输出，测试时只关心返回值。"""


def txn(source="alipay", counterparty="某商户", item="", amount="10.00",
        direction=DIRECTION_EXPENSE, platform_category="", status="") -> Txn:
    from decimal import Decimal
    import datetime as dt
    return Txn(
        source=source, ts=dt.datetime(2026, 9, 3, 12, 0, 0),
        amount=Decimal(amount), direction=direction, counterparty=counterparty,
        item=item, status=status, platform_category=platform_category,
    )


# ==========================================================================
# 1. 状态判定本身
# ==========================================================================

def case_status_verdict() -> None:
    section("1. 状态判定：四种结论")

    # 这些是「确认完成」，钱真的花出去了
    for value in ("交易成功", "支付成功", "对方已收钱", "朋友已收钱",
                  "已存入零钱", "充值完成", "已到账"):
        check(f"{value!r} 判为已完成", status_verdict(value), STATUS_OK)

    # 「已收钱」是 2026-10-05 在真实账本里发现的状态值：
    # 微信转账收款完成后就是这个词。认不出来会让这笔一直挂在诊断的
    # 「谜题」列表里，而它对方向的处理（不压）和 unknown 完全一样。
    check("'已收钱' 判为已完成", status_verdict("已收钱"), STATUS_OK)
    check("'已收钱' 不压支出（和 unknown 一个效果）",
          status_verdict("已收钱") in STATUS_KILLS_EXPENSE, False)

    # 这些是「没花出去」
    for value in ("交易关闭", "已关闭", "交易取消", "已取消", "支付失败",
                  "已过期", "订单已关闭"):
        check(f"{value!r} 判为未成立", status_verdict(value), STATUS_FAILED)

    for value in ("已全额退款", "已退款", "退款成功", "已退回"):
        check(f"{value!r} 判为已退款", status_verdict(value), STATUS_REFUNDED)

    for value in ("交易中", "处理中", "待确认"):
        check(f"{value!r} 判为未完成", status_verdict(value), STATUS_PENDING)

    # 认不出来就明确说认不出来，绝不猜成「已完成」
    for value in ("", "   ", "某平台没见过的值"):
        check(f"{value!r} 判为无法识别", status_verdict(value), STATUS_UNKNOWN)


def case_status_order_matters() -> None:
    section("2. 判定顺序：先 failed 再 refunded，最后才 ok")

    # 这一组是整个模块最容易写错、错了也最难发现的地方。
    #
    # 「退款成功」里含有「成功」两个字。如果先匹配 ok 词表，
    # 一笔全额退款会被判成正常消费，88 元稳稳地算进当月支出 ——
    # 而从任何角度看这条数据都「很正常」，没有异常会暴露这个错。
    check("「退款成功」不是已完成（它含'成功'二字）",
          status_verdict("退款成功"), STATUS_REFUNDED)
    check("「已全额退款」不是已完成",
          status_verdict("已全额退款"), STATUS_REFUNDED)
    check("「退款成功」在压支出的集合里",
          status_verdict("退款成功") in STATUS_KILLS_EXPENSE, True)
    check("「交易成功」不在压支出的集合里",
          status_verdict("交易成功") in STATUS_KILLS_EXPENSE, False)


# ==========================================================================
# 3. 平台分类映射
# ==========================================================================

def case_platform_map() -> None:
    section("3. 平台分类映射")

    check("映射表的值全都是真实分类",
          platformmap.validate(), [])

    # 支付宝：结构化行业分类，映射得很干脆
    for value, want in (("餐饮美食", "餐饮"), ("交通出行", "交通"),
                        ("日用百货", "日用百货"), ("数码电器", "数码电子"),
                        ("交通出行", "交通"), ("服饰装扮", "购物")):
        check(f"支付宝 {value!r} → {want}", platformmap.from_alipay(value), want)

    # 微信：有区分度的值才有意义
    for value, want in (("转账", "转账还款"), ("群收款", "人情往来"),
                        ("零钱充值", "提现充值"), ("信用卡还款", "转账还款")):
        check(f"微信 {value!r} → {want}", platformmap.from_wechat(value), want)

    # 银行：靠交易名称/渠道里的通用说法
    for value, want in (("转账还款", "转账还款"), ("电费", "生活缴费"),
                        ("手机话费充值", "通讯网络"), ("房租", "住房")):
        check(f"银行 {value!r} → {want}", platformmap.from_bank(value), want)


def case_platform_map_refuses_noise() -> None:
    section("4. 零信息量的值必须拒绝（这是本模块的核心纪律）")

    # 微信九成流水都是「商户消费」。拿它当分类依据 = 把所有消费压成一个类，
    # 比不做平台分类还糟糕。必须返回 None，让流程落到关键词规则上。
    for value in ("商户消费", "二维码收款", "扫钱码", "扫二维码"):
        check(f"微信 {value!r} 拒绝参与判定",
              platformmap.from_wechat(value), None)

    # 支付宝自己都说「其他」了
    for value in ("其他", "其他交易", ""):
        check(f"支付宝 {value!r} 拒绝参与判定",
              platformmap.from_alipay(value), None)

    # 银行渠道里的平台名不代表分类 —— 支付宝那边的账单才是权威。
    # 在这儿硬猜会把「支付宝-某某超市」判成购物，比留给规则更差。
    for value in ("支付宝", "财付通", "云闪付", "第三方支付"):
        check(f"银行渠道 {value!r} 拒绝参与判定",
              platformmap.from_bank(value), None)


def case_platform_map_robustness() -> None:
    section("5. 改版耐受：未知值不炸、包含匹配生效")

    # 平台改版会冒出没见过的值。必须返回 None，不能抛异常 ——
    # 一个 KeyError 会让整份账单导入失败，那比判错更糟。
    check("没见过的支付宝类目不抛异常",
          platformmap.from_alipay("某个全新的类目"), None)
    check("没见过的微信类型不抛异常",
          platformmap.from_wechat("某个全新的类型"), None)

    # 用包含而不是等号：支付宝前后改过分类体系，等号匹配改版当天就全失效
    check("「餐饮」这种短名也认（等号匹配会漏）",
          platformmap.from_alipay("餐饮"), "餐饮")
    check("带后缀的长名也认",
          platformmap.from_alipay("餐饮美食-新版"), "餐饮")
    check("全角字符也认（normalize 之后匹配）",
          platformmap.from_alipay(normalize_text("餐饮美食")), "餐饮")

    # 多值：第一个命中的赢（调用方按信息量从大到小传）
    check("银行多值取第一个命中",
          platformmap.from_bank("未知交易", "电费", "房租"), "生活缴费")


# ==========================================================================
# 6. 优先级：人工 > 平台 > 规则
# ==========================================================================

def case_priority() -> None:
    section("6. 优先级：人工指定 > 平台分类 > 关键词规则")

    led = fresh_ledger()

    # 平台分类压过规则：商户名/商品名里一个规则词都没有，
    # 只有平台分类能认出它是生活缴费。
    t = txn(counterparty="某某城市服务有限公司", item="物业费",
            platform_category="生活服务")
    categorize([t], led)
    check("平台分类压过规则（规则认不出时靠平台认出）", t.category, "生活缴费")

    # 平台分类压过规则：这里两边都会判成餐饮，用一个能分出高下的组合
    # 商户名带「便利店」（规则 → 餐饮），平台分类给「日用百货」（→ 日用百货）
    t = txn(counterparty="某某便利店", item="关东煮", platform_category="日用百货")
    categorize([t], led)
    check("平台分类与规则冲突时平台赢", t.category, "日用百货")

    # 人工指定压过平台分类 —— 这是项目铁律，不能被任何新层破坏
    led2 = fresh_ledger()
    led2.conn.execute(
        "INSERT INTO merchant_category (merchant, category, decided_by, decided_at)"
        " VALUES (?,?,?,?)", ("某某便利店", "餐饮", "manual", "2026-09-03 12:00:00"))
    led2.conn.commit()
    t = txn(counterparty="某某便利店", item="关东煮", platform_category="日用百货")
    categorize([t], led2)
    check("人工指定压过平台分类（铁律第 5 条）", t.category, "餐饮")

    # 平台分类为空值时，规则照常工作
    t = txn(counterparty="某某星巴克咖啡", item="拿铁", platform_category="")
    categorize([t], led2)
    check("平台分类为空时规则正常工作", t.category, "餐饮")


def case_platform_never_cached() -> None:
    section("7. 平台分类绝不写进商户缓存")

    led = fresh_ledger()
    # 同一个商户，两笔完全不同的平台分类 —— 现实里很常见：
    # 连锁超市既卖日用品也卖水果蔬菜、在门店里还能吃饭。
    t1 = txn(counterparty="某某连锁超市", item="洗衣液", platform_category="日用百货")
    t2 = txn(counterparty="某某连锁超市", item="盒饭", platform_category="餐饮美食")
    categorize([t1, t2], led)

    check("第一笔按平台分类判", t1.category, "日用百货")
    check("第二笔按自己的平台分类判，没被第一笔的缓存带偏",
          t2.category, "餐饮")

    cached = dict(led.conn.execute(
        "SELECT merchant, category FROM merchant_category WHERE merchant = ?",
        ("某某连锁超市",)).fetchall())
    check("商户缓存里没有它（平台分类是逐笔的，缓存是一对一）", cached, {})


# ==========================================================================
# 8. 状态把「没花出去」的支出压成中性
# ==========================================================================

def case_status_neutralises() -> None:
    section("8. 状态：没真花出去的支出被压成中性")

    led = fresh_ledger()
    closed = txn(counterparty="甲商户", amount="88.00", status="交易关闭",
                 platform_category="餐饮美食")
    refunded = txn(counterparty="乙商户", amount="45.00", status="已全额退款",
                   platform_category="餐饮美食")
    ok = txn(counterparty="丙商户", amount="21.50", status="交易成功",
             platform_category="餐饮美食")
    unknown = txn(counterparty="丁商户", amount="30.00", status="某平台新状态",
                  platform_category="餐饮美食")
    income = txn(counterparty="戊商户", amount="45.00", direction=DIRECTION_INCOME,
                 status="已全额退款")

    stats = categorize([closed, refunded, ok, unknown, income], led)

    check("交易关闭 → 中性", closed.direction, DIRECTION_NEUTRAL)
    check("已全额退款 → 中性", refunded.direction, DIRECTION_NEUTRAL)
    check("交易成功 → 仍然是支出", ok.direction, DIRECTION_EXPENSE)
    check("认不出的状态**不改方向**（宁可放过也不误伤真消费）",
          unknown.direction, DIRECTION_EXPENSE)
    check("收入那一侧不受状态影响（退款回来的钱是真入账）",
          income.direction, DIRECTION_INCOME)
    check("统计里如实记了改动笔数", stats["status_neutralised"], 2)
    check("分类不受状态影响（平台分类照常生效）",
          (closed.category, refunded.category), ("餐饮", "餐饮"))


def case_expense_total_impact() -> None:
    section("9. 报表口径：这才是这套判定存在的理由")

    led = fresh_ledger()
    real = txn(counterparty="真消费", amount="100.00", status="交易成功",
               platform_category="餐饮美食")
    closed = txn(counterparty="没成交", amount="88.00", status="交易关闭",
                 platform_category="餐饮美食")
    categorize([real, closed], led)
    for t in (real, closed):
        led.add(t)

    total = led.conn.execute(
        "SELECT SUM(CAST(amount AS REAL)) AS v FROM txn WHERE direction = 'expense'"
    ).fetchone()["v"]
    check("月支出只算真花出去的那笔", round(total, 2), 100.0)
    check("没成交那笔仍在库里（可追溯、能对账）",
          led.conn.execute("SELECT COUNT(*) AS n FROM txn").fetchone()["n"], 2)


# ==========================================================================
# 10. recompute 的双向性
# ==========================================================================

def case_recompute_is_bidirectional() -> None:
    section("10. recompute：状态与平台分类都能双向重推")

    led = fresh_ledger()
    t = txn(counterparty="某某连锁超市", item="洗衣液", amount="60.00",
            status="交易成功", platform_category="日用百货")
    categorize([t], led)
    led.add(t)

    row = led.conn.execute(
        "SELECT category, direction, status, platform_category FROM txn").fetchone()
    check("初始：日用百货 / 支出",
          (row["category"], row["direction"]), ("日用百货", "expense"))
    check("平台分类原文确实落库了", row["platform_category"], "日用百货")

    # 模拟「状态列被平台改回交易关闭」：改库里的状态，再 recompute
    led.conn.execute("UPDATE txn SET status = '交易关闭'")
    led.conn.commit()
    fixups.recompute(led, apply=True, emit=quiet)
    row = led.conn.execute("SELECT direction FROM txn").fetchone()
    check("状态变了之后 recompute 跟着改", row["direction"], "neutral")

    # 改回来 —— 这是双向的关键。单向压死的实现在这里就回不去了。
    led.conn.execute("UPDATE txn SET status = '交易成功'")
    led.conn.commit()
    fixups.recompute(led, apply=True, emit=quiet)
    row = led.conn.execute("SELECT direction FROM txn").fetchone()
    check("状态改回来之后方向能还原（双向，不是单向压死）",
          row["direction"], "expense")

    # 平台分类也参与重推：改成一个规则能认出的值，看分类是否跟着规则走。
    # 「某某连锁超市」的商品名是「洗衣液」，规则表里「超市」属于日用百货
    # —— 所以这里验的是「平台分类一撤，规则立刻接管」这条链路，
    # 用一个规则本来就会给**不同**答案的组合才有说服力。
    led.conn.execute(
        "UPDATE txn SET counterparty = ?, item = ?", ("某某便利店", "关东煮"))
    led.conn.execute("UPDATE txn SET platform_category = '其他'")
    led.conn.commit()
    fixups.recompute(led, apply=True, emit=quiet)
    row = led.conn.execute("SELECT category FROM txn").fetchone()
    check("平台分类撤掉后 recompute 退回关键词规则（「便利店」→ 餐饮）",
          row["category"], "餐饮")

    # 再把平台分类放回去，规则必须让位
    led.conn.execute("UPDATE txn SET platform_category = '日用百货'")
    led.conn.commit()
    fixups.recompute(led, apply=True, emit=quiet)
    row = led.conn.execute("SELECT category FROM txn").fetchone()
    check("平台分类放回去后重新压过规则（改回来也要能还原）",
          row["category"], "日用百货")


# ==========================================================================
# 11. 端到端：真读一份带各种状态的账单文件
# ==========================================================================

def case_end_to_end_alipay_status_file() -> None:
    section("11. 端到端：读真实的支付宝状态样本文件")

    path = os.path.join(FIXTURES, "sample_alipay_status.csv")
    if not os.path.exists(path):
        check("状态样本文件存在", False, True)
        return

    result = parse_alipay(path)
    check("解析笔数与文件自述一致", len(result.txns), result.declared_count)

    led = fresh_ledger()
    categorize(result.txns, led)

    rows = {t.status: t for t in result.txns}
    check("「交易成功」那笔仍是支出",
          rows.get("交易成功").direction, DIRECTION_EXPENSE)
    closed = [t for t in result.txns if t.status == "交易关闭" and t.amount > 100]
    check("大额「交易关闭」被压成中性（消费那笔）",
          closed[0].direction if closed else "无", DIRECTION_NEUTRAL)

    # 平台分类原文进了 Txn
    cats = {t.platform_category for t in result.txns}
    check("平台分类原文被解析出来", "餐饮美食" in cats, True)

    # 已全额退款那笔：分类照常是服饰→购物，方向被压中性
    refunded = [t for t in result.txns if t.status == "已全额退款"]
    check("已全额退款那笔：分类正常、方向中性",
          (refunded[0].category, refunded[0].direction) if refunded else "无",
          ("购物", DIRECTION_NEUTRAL))


def case_end_to_end_old_rows_unaffected() -> None:
    section("12. 老数据（没有平台分类）行为完全不变")

    led = fresh_ledger()
    t = txn(counterparty="某某星巴克咖啡", item="拿铁", platform_category="")
    categorize([t], led)
    check("没有平台分类的老数据照旧走规则", t.category, "餐饮")

    # 老库里 platform_category 是补出来的空串，查询必须正常
    led.add(t)
    row = led.conn.execute("SELECT platform_category FROM txn").fetchone()
    check("老行补出来的空串可读", row["platform_category"], "")

    # 反向也验一次：模拟老库（没有这一列）被打开
    import importer.store as store_mod
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(store_mod.SCHEMA.replace(
        "    platform_category TEXT DEFAULT '',\n", ""))
    conn.execute(
        "INSERT INTO txn (source, ts, amount, direction, counterparty, fingerprint)"
        " VALUES ('alipay','2026-09-03 12:00:00','10.00','expense','某商户','fp1')")
    conn.commit()
    led2 = Ledger(conn, ":memory:", init_schema=False)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(txn)")]
    check("缺列的老库被自动补上 platform_category",
          "platform_category" in cols, True)
    check("补列不影响原有数据",
          conn.execute("SELECT counterparty FROM txn").fetchone()["counterparty"],
          "某商户")


def main() -> int:
    case_status_verdict()
    case_status_order_matters()
    case_platform_map()
    case_platform_map_refuses_noise()
    case_platform_map_robustness()
    case_priority()
    case_platform_never_cached()
    case_status_neutralises()
    case_expense_total_impact()
    case_recompute_is_bidirectional()
    case_end_to_end_alipay_status_file()
    case_end_to_end_old_rows_unaffected()

    print()
    print("=" * 80)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
