"""消费分类：规则优先，本地 AI 兜底。

为什么要分两层：
    规则能覆盖 80%+ 的流水，而且结果可复现、可解释、零耗电。
    剩下 20% 是「某某商务服务有限公司」这类看不出来的收款方，才轮到 AI。

AI 的三个硬约束：
    1. 只能连 127.0.0.1 / localhost。整个工具不允许任何形式的联网。
    2. 必须插电（见 power.py）。跑模型是密集任务。
    3. 结果写进 merchant_category 表缓存。同一个商户只问一次，
       以后连 AI 都不用开——省电、可离线、结果稳定。

AI 在这里只做一件事：给商户名分配一个分类。
它不参与去重，不参与金额计算，不碰任何精确逻辑。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Iterable, Optional

from .model import (
    DIRECTION_EXPENSE,
    DIRECTION_NEUTRAL,
    STATUS_KILLS_EXPENSE,
    Txn,
    normalize_text,
    status_verdict,
)
from .platformmap import platform_category as _platform_category
from .power import require_ac

# --------------------------------------------------------------------------
# 分类体系
# --------------------------------------------------------------------------

CATEGORIES = (
    "餐饮", "交通", "购物", "日用百货", "生活缴费", "通讯网络",
    "学习文具", "医疗健康", "休闲娱乐", "人情往来", "住房",
    "数码电子", "订阅服务", "运动健身", "美容个护", "旅行",
    "转账还款", "提现充值", "退款", "其他",
)

DEFAULT_CATEGORY = "未分类"

# merchant_category 表里 decided_by 的取值。
# manual 是最高优先级：它压过规则，也压过本地 AI。
# 用户手工纠正过的分类，程序**任何路径**都不许改回去。
DECIDED_BY_MANUAL = "manual"
DECIDED_BY_RULE = "rule"
DECIDED_BY_AI = "ai"

# 这几个分类按定义就不是「消费」：钱只是在自己的账户之间搬，或者是在还自己的账。
#
# 判定为这几个分类的交易，方向一律压成中性，不管平台在「收/支」列里写了什么。
# 为什么必须这么做：实测微信把「零钱通转出」在收/支里标成**支出**——
# 从那个钱包的角度钱确实出去了，但它不是消费。
# 用户 9 月账单里这一类占了 16,617 元，是当月"总支出"的 61%，全是虚的。
NON_CONSUMPTION_CATEGORIES = frozenset({"提现充值", "转账还款", "退款"})

# 规则表：(分类, 关键词元组)
#
# 顺序有讲究：**先具体后笼统**。
# 「提现充值」「转账还款」放在最后 —— 它们的关键词（充值 / 转账 / 转入 / 转出）
# 太宽，放前面会把「话费充值」（真实消费，该进通讯网络）之类的东西吸走。
# 这条顺序错误实测让用户的支出报表多算了一笔。
RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("餐饮", ("餐饮", "外卖", "餐厅", "饭店", "小吃", "快餐", "烧烤", "火锅", "面馆",
              "米饭", "食堂", "咖啡", "奶茶", "茶饮", "星巴克", "瑞幸", "蜜雪",
              "肯德基", "麦当劳", "汉堡", "面包", "早餐", "便利店")),
    ("通讯网络", ("话费", "流量", "宽带", "移动", "联通", "电信", "中国移动",
                  "中国联通", "中国电信")),
    # 学习文具要排在生活缴费前面：「学费」比「缴费」更具体，
    # 一笔大学学费的商品描述里两个词都有，让更具体的那个赢。
    #
    # 「平台缴费」是用户学校缴费系统的商品名（格式是「学号+平台缴费」）。
    # 它字面含「缴费」，不加进来的话会被生活缴费抢走。
    ("学习文具", ("学费", "学杂费", "平台缴费", "教务", "书店", "图书", "文具",
                  "教育", "培训", "网课", "慕课", "学习", "当当", "考试",
                  "报名费", "知网")),
    ("生活缴费", ("水电", "电费", "水费", "燃气", "物业", "取暖", "暖气", "缴费",
                  "国家电网", "供电", "自来水")),
    ("医疗健康", ("医院", "药房", "药店", "诊所", "医疗", "体检", "挂号", "医药",
                  "口腔", "眼科")),
    ("休闲娱乐", ("电影", "影城", "KTV", "游戏", "网吧", "娱乐", "剧本", "演出",
                  "门票", "哔哩", "bilibili", "腾讯视频", "爱奇艺", "优酷", "网易云",
                  "QQ音乐", "音乐", "会员")),
    ("订阅服务", ("订阅", "会员费", "云服务", "空间", "iCloud", "百度网盘",
                  "阿里云", "腾讯云", "GitHub", "OpenAI", "JetBrains", "Notion")),
    ("运动健身", ("健身", "游泳", "球馆", "运动", "keep", "跑步", "羽毛球", "球场")),
    ("美容个护", ("理发", "美发", "美容", "美甲", "洗浴", "护肤", "化妆")),
    ("数码电子", ("数码", "电脑", "手机", "耳机", "键盘", "鼠标", "显示器", "京东",
                  "小米", "华为", "苹果", "Apple", "联想")),
    ("住房", ("房租", "租金", "中介", "公寓", "住宿", "酒店", "民宿")),
    ("旅行", ("旅游", "旅行", "景区", "携程", "去哪儿", "飞猪", "酒店预订")),
    ("人情往来", ("礼金", "随礼", "份子", "慰问", "捐赠")),
    ("交通", ("地铁", "公交", "出行", "打车", "滴滴", "出租车", "高铁", "火车", "机票",
              "航空", "加油", "停车", "高速", "单车", "共享", "哈啰", "青桔", "车费")),
    ("日用百货", ("超市", "百货", "生鲜", "菜市场", "蔬菜", "水果", "便利店", "永辉",
                  "华润", "丹尼斯", "大润发", "杂货")),
    ("购物", ("淘宝", "天猫", "拼多多", "唯品会", "商城", "服饰", "服装", "鞋",
              "优衣库", "商场", "购物")),
    # ↓ 以下是「非消费」分类，必须放最后
    ("退款", ("退款", "退货", "赔付")),
    ("转账还款", ("还款", "花呗", "借呗", "信用卡", "备用金", "转账", "红包", "亲属卡")),
    ("提现充值", ("充值", "提现", "零钱通", "余额宝", "理财通", "转入", "转出")),
)


def classify_by_rules(counterparty: str, item: str = "") -> Optional[str]:
    """用关键词规则分类。命中返回分类名，没命中返回 None。"""
    blob = normalize_text(f"{counterparty} {item}").lower()
    if not blob:
        return None
    for category, keywords in RULES:
        for keyword in keywords:
            if keyword.lower() in blob:
                return category
    return None


# --------------------------------------------------------------------------
# 人工指定（优先级最高）
#
# 为什么需要这一层：
#   规则靠「关键词 + 顺序」，它解决的是常见商户。总有认不出来的：
#   商户名只有「某某网络科技有限公司」这类公司全称、商品描述只有一个
#   「缴费」而看不出是什么费。给规则加词能解一时，但每加一个词都在
#   赌它不会吸走别的交易（「缴费」抢走「学费」就是这么来的）。
#
#   所以留一个**人说了算**的口子：用户自己看一眼就知道这是什么，
#   直接指定分类，不写代码、不改规则表，也不会误伤别的商户。
#
# 它为什么比「改规则表」安全：
#   规则是全局的、按关键词匹配的；人工指定是**按商户精确匹配**的。
#   指定「某某餐饮管理有限公司 = 餐饮」不会影响任何其它商户。
#
# 存哪儿：直接复用 merchant_category 这张缓存表，decided_by='manual'。
#   categorize() 本来就会先读这张表，所以指定完**下次导入自动生效**；
#   已经入库的历史交易要靠 fixups.apply_manual 才会跟着变。
# --------------------------------------------------------------------------

def manual_map(ledger) -> dict[str, str]:
    """读出全部「人工指定」的商户 → 分类。键是归一化后的商户名。"""
    return {
        row["merchant"]: row["category"]
        for row in ledger.conn.execute(
            "SELECT merchant, category FROM merchant_category WHERE decided_by = ?",
            (DECIDED_BY_MANUAL,),
        )
    }


def set_manual(ledger, merchant: str, category: str) -> str:
    """手工指定一个商户的分类。返回归一化后的商户键。

    只写缓存表。新导入的交易下一次就会用上；
    已经在库里的历史交易需要 fixups.apply_manual 才会跟着变。
    """
    key = normalize_text(merchant)
    if not key:
        raise ValueError("商户名不能为空")
    if category not in CATEGORIES:
        raise ValueError(
            f"不认识的分类「{category}」。可用分类：{'、'.join(CATEGORIES)}"
        )
    _remember(ledger, key, category, DECIDED_BY_MANUAL)
    ledger.conn.commit()
    return key


def clear_manual(ledger, merchant: str) -> bool:
    """撤销某个商户的人工指定，退回规则判定。返回是否确实删掉了一行。"""
    cur = ledger.conn.execute(
        "DELETE FROM merchant_category WHERE merchant = ? AND decided_by = ?",
        (normalize_text(merchant), DECIDED_BY_MANUAL),
    )
    ledger.conn.commit()
    return cur.rowcount > 0


def category_for(merchant_key: str, counterparty: str, item: str = "",
                 manual: Optional[dict[str, str]] = None,
                 source: str = "", platform_raw: str = "") -> Optional[str]:
    """分类优先级：人工指定 > 平台分类 > 规则 > None（没命中）。

    把优先级集中在这一处，recompute 与导入两条路径就不会各自漂移。

    平台分类放在规则**之上**、人工指定**之下**：
    支付宝的「餐饮美食」是平台判好的，比我们靠「食堂」两个字猜更准；
    但用户手工纠正过的必须压过平台——人永远是最权威的（见 MEMORY 铁律第 5 条）。
    """
    if manual and merchant_key in manual:
        return manual[merchant_key]
    if platform_raw:
        hit = _platform_category(source, platform_raw)
        if hit:
            return hit
    return classify_by_rules(counterparty, item)


# --------------------------------------------------------------------------
# 本地 AI 兜底
# --------------------------------------------------------------------------

LOCAL_AI_HOSTS = ("127.0.0.1", "localhost", "::1")
DEFAULT_AI_URL = "http://127.0.0.1:11434"     # Ollama 默认端口
DEFAULT_AI_MODEL = "qwen2.5:7b"


def _assert_local(url: str) -> None:
    """硬闸门：只允许连本机。整个工具不允许任何形式的联网。"""
    from urllib.parse import urlparse
    parts = urlparse(url)
    # URL 里带 userinfo 一律拒：本机模型不需要用户名密码。
    # 「evil.com@127.0.0.1」这种写法虽然 urlparse 解析出的 hostname 仍是
    # 127.0.0.1（连的本机），但它会把一串凭据塞进 Authorization 头里。
    # 没有正当场景需要它，拒掉比判断「这算不算安全」省事。
    if parts.username or parts.password:
        raise ValueError(
            "拒绝连接：地址里带了用户名/密码，本机模型不需要凭据。"
        )
    host = (parts.hostname or "").lower()
    if host not in LOCAL_AI_HOSTS:
        raise ValueError(
            f"拒绝连接 {host}：本工具的 AI 分类只允许本地模型。"
            f"账单数据一旦离开这台机器就不再受你控制。"
        )


def ai_available(url: str = DEFAULT_AI_URL, timeout: float = 1.5) -> bool:
    """本机模型在不在。地址不合法时**抛出**，不伪装成「没响应」。

    `_assert_local` 刻意放在 try 外面：它抛的 ValueError 说的是
    「拒绝连这个地址」，和「对方没开机」是完全不同的两件事，
    处置方式也相反（改配置 vs 去装 Ollama）。
    把它混进 catch-all 里，用户会照着「模型没响应」去反复重启服务。
    """
    _assert_local(url)
    try:
        req = urllib.request.Request(url.rstrip("/") + "/api/tags")
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:
        return False


def ai_classify(merchants: list[str], url: str = DEFAULT_AI_URL,
                model: str = DEFAULT_AI_MODEL, timeout: float = 120) -> dict[str, str]:
    """让本地模型给一批商户名分配分类。返回 {商户: 分类}。"""
    _assert_local(url)
    if not merchants:
        return {}

    prompt = (
        "你是个人记账助手。下面是一批中国用户的支付交易对方名称，"
        "请给每一个分配一个最合适的消费分类。\n"
        f"分类只能从这些里面选：{'、'.join(CATEGORIES)}\n"
        "商户名可能是公司全称（如「某某网络科技有限公司」），请按业务实质判断。\n"
        "只输出 JSON，格式为 {\"商户名\": \"分类\"}，不要输出任何解释。\n\n"
        "商户列表：\n" + "\n".join(f"- {m}" for m in merchants)
    )

    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
    }).encode("utf-8")

    req = urllib.request.Request(
        url.rstrip("/") + "/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))

    text = body.get("response", "")
    try:
        mapping = json.loads(text)
    except json.JSONDecodeError:
        # 模型偶尔会包一层 markdown，抠出第一个 JSON 对象
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < 0:
            return {}
        mapping = json.loads(text[start:end + 1])

    return {
        normalize_text(k): v for k, v in mapping.items()
        if isinstance(v, str) and v in CATEGORIES
    }


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------

def _apply_status(txns) -> int:
    """把「状态显示这笔没真花出去」的**支出**压成中性。返回改动笔数。

    支付宝「交易关闭」、微信「已全额退款」「已过期」都属于这一类。
    记录留在账本里（可追溯、能对账），但不再计入支出 ——
    它的钱要么从没出去，要么已经回来了。

    **只对支出生效，收入一律保留。**
    理由和 _apply_non_consumption 一样：退款回来的 100 元是真实入账，
    压成中性会让「收入」凭空少一块。

    状态认不出来（STATUS_UNKNOWN）时**不动**方向：
    宁可放过一笔可疑的，也不要凭猜把一笔真消费从报表里抹掉 ——
    后者这种错你永远不会发现。
    """
    changed = 0
    for txn in txns:
        if (txn.direction == DIRECTION_EXPENSE
                and status_verdict(txn.status) in STATUS_KILLS_EXPENSE):
            txn.direction = DIRECTION_NEUTRAL
            changed += 1
    return changed


def _apply_non_consumption(txns) -> int:
    """把落在「非消费」分类里的**支出**压成中性。返回改动笔数。

    必须在分类之后做 —— 方向是解析时定的，那时还不知道分类。

    **只对支出生效，收入一律保留。**
    理由：家里给的生活费、朋友还的钱，在平台里都表现为「转账」进账，
    它们归到「转账还款」分类，但那是真金白银进来的，必须算收入。
    只有支出一侧才存在「钱在自己账户之间搬」这种假消费。
    """
    changed = 0
    for txn in txns:
        if (txn.category in NON_CONSUMPTION_CATEGORIES
                and txn.direction == DIRECTION_EXPENSE):
            txn.direction = DIRECTION_NEUTRAL
            changed += 1
    return changed


def categorize(txns: Iterable[Txn], ledger, use_ai: bool = False,
               ai_url: str = DEFAULT_AI_URL, ai_model: str = DEFAULT_AI_MODEL,
               ignore_power: bool = False) -> dict:
    """给交易打分类标签。返回统计信息。"""
    stats = {"manual": 0, "platform": 0, "rules": 0, "cache": 0, "ai": 0,
             "unmatched": 0, "ai_used": False, "neutralised": 0,
             "status_neutralised": 0}

    # 先读缓存：同一个商户只判定一次。
    # manual 必须单独拆出来 —— 它压过一切（包括平台分类），
    # 混在同一个 dict 里就没法保证它先被查到。
    cache: dict[str, str] = {}
    manual: dict[str, str] = {}
    for row in ledger.conn.execute(
            "SELECT merchant, category, decided_by FROM merchant_category"):
        if row["decided_by"] == DECIDED_BY_MANUAL:
            manual[row["merchant"]] = row["category"]
        else:
            cache[row["merchant"]] = row["category"]

    unmatched: set[str] = set()

    for txn in txns:
        merchant = normalize_text(txn.counterparty)

        # 1) 人工指定压过一切（含平台分类）
        if merchant and merchant in manual:
            txn.category = manual[merchant]
            stats["manual"] += 1
            continue

        # 2) 平台自带分类
        #
        # 刻意**不查也不写** cache：cache 是「商户 → 分类」的一对一映射，
        # 而平台分类是**逐笔**的。同一个商户完全可能在餐饮店给「餐饮美食」、
        # 在超市给「日用百货」—— 写进 cache 等于把其中一笔的答案固化成
        # 这个商户「永远」的分类，另一笔就被这个 cache 误伤了。
        # 代价只是同一商户每笔多一次 dict 查找，可以忽略。
        if txn.platform_category:
            hit = _platform_category(txn.source, txn.platform_category)
            if hit:
                txn.category = hit
                stats["platform"] += 1
                continue

        # 3) 商户缓存（之前由规则/AI 判定的结果）
        if merchant and merchant in cache:
            txn.category = cache[merchant]
            stats["cache"] += 1
            continue

        # 4) 关键词规则
        hit = classify_by_rules(txn.counterparty, txn.item)
        if hit:
            txn.category = hit
            stats["rules"] += 1
            if merchant:
                _remember(ledger, merchant, hit, DECIDED_BY_RULE)
                cache[merchant] = hit
            continue

        txn.category = DEFAULT_CATEGORY
        stats["unmatched"] += 1
        if merchant:
            unmatched.add(merchant)

    stats["status_neutralised"] = _apply_status(txns)

    if not use_ai or not unmatched:
        stats["neutralised"] = _apply_non_consumption(txns)
        return stats

    if not require_ac(f"本地 AI 分类（{len(unmatched)} 个未识别商户）", override=ignore_power):
        return stats

    try:
        reachable = ai_available(ai_url)
    except ValueError as exc:
        # 地址不合法：把拒绝的原因原样说出来，然后跳过 AI，不要崩掉整轮
        print(f"{exc}")
        return stats
    if not reachable:
        print(f"本地模型没响应（{ai_url}）。想用 AI 分类的话先装 Ollama 并拉一个模型：")
        print(f"  ollama pull {ai_model}")
        return stats

    merchants = sorted(unmatched)

    # ML-005：把「数据要去哪」先说清楚，再真的发。
    #
    # 这里刻意**不**对商户名做遮罩。AI 分类的全部依据就是商户名 ——
    # 遮掉其中任何一个字，模型就认不出这是哪家店，这个功能等于被阉割掉。
    # （对比 `unmatched --mask`：那个输出是给人列的清单，脱敏无副作用。）
    #
    # 所以能做的只有两件事：**发之前讲明白代价**，以及确保不要扩散到第二处。
    # 后者已经成立：AuditLog 的白名单里只有 `ai` 这个布尔值，
    # 商户名不会进本项目自己的任何日志。剩下的风险在 Ollama 那侧。
    print(f"把 {len(merchants)} 个未识别商户的**商户名**发给本机模型 {ai_model}（{ai_url}）。")
    print("  为什么必须发完整名称：遮掉任何一个字，模型就认不出这是哪家店，")
    print("  分类也就没用了 —— 这条路要么完整发，要么别开。")
    print("  代价：这些名字会以明文留在 Ollama 自己的日志里（不加密，也没有清理期限）。")
    print("  更保守的路：不开 AI，用 unmatched 看一眼 + setcat 指定，商户名一步都不出本项目。")

    print(f"调用本地模型 {ai_model} 分类 {len(merchants)} 个商户……")
    mapping: dict[str, str] = {}
    # 分批，避免一次塞太多把上下文撑爆
    for i in range(0, len(merchants), 40):
        chunk = merchants[i:i + 40]
        try:
            mapping.update(ai_classify(chunk, url=ai_url, model=ai_model))
        except Exception as exc:
            print(f"  第 {i // 40 + 1} 批失败：{exc}")

    stats["ai_used"] = True
    for txn in txns:
        merchant = normalize_text(txn.counterparty)
        if txn.category == DEFAULT_CATEGORY and merchant in mapping:
            txn.category = mapping[merchant]
            stats["ai"] += 1
            _remember(ledger, merchant, mapping[merchant], DECIDED_BY_AI)
            cache[merchant] = mapping[merchant]

    ledger.conn.commit()
    stats["neutralised"] = _apply_non_consumption(txns)
    return stats


def _remember(ledger, merchant: str, category: str, decided_by: str) -> None:
    """写进商户→分类缓存。

    内置一道守卫：**人工指定的记录不许被规则或 AI 覆盖。**
    没有这道闸，用户手工改完分类，下次导入同一商户时规则一命中就把它
    悄悄改回去 —— 没有任何提示，用户只会觉得「我明明改过了，怎么又变回来了」。
    """
    if decided_by != DECIDED_BY_MANUAL:
        existing = ledger.conn.execute(
            "SELECT decided_by FROM merchant_category WHERE merchant = ?",
            (merchant,),
        ).fetchone()
        if existing is not None and existing["decided_by"] == DECIDED_BY_MANUAL:
            return

    ledger.conn.execute(
        "INSERT INTO merchant_category (merchant, category, decided_by, decided_at)"
        " VALUES (?,?,?,?) ON CONFLICT(merchant) DO UPDATE SET"
        " category=excluded.category, decided_by=excluded.decided_by,"
        " decided_at=excluded.decided_at",
        (merchant, category, decided_by,
         datetime.now().isoformat(sep=" ", timespec="seconds")),
    )
