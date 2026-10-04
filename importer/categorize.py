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

from .model import DIRECTION_EXPENSE, DIRECTION_NEUTRAL, Txn, normalize_text
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
    ("学习文具", ("学费", "书店", "图书", "文具", "教育", "培训", "网课", "慕课", "学习",
                  "当当", "考试", "报名费", "知网")),
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
# 本地 AI 兜底
# --------------------------------------------------------------------------

LOCAL_AI_HOSTS = ("127.0.0.1", "localhost", "::1")
DEFAULT_AI_URL = "http://127.0.0.1:11434"     # Ollama 默认端口
DEFAULT_AI_MODEL = "qwen2.5:7b"


def _assert_local(url: str) -> None:
    """硬闸门：只允许连本机。整个工具不允许任何形式的联网。"""
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if host not in LOCAL_AI_HOSTS:
        raise ValueError(
            f"拒绝连接 {host}：本工具的 AI 分类只允许本地模型。"
            f"账单数据一旦离开这台机器就不再受你控制。"
        )


def ai_available(url: str = DEFAULT_AI_URL, timeout: float = 1.5) -> bool:
    try:
        _assert_local(url)
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

def _apply_non_consumption(txns) -> int:
    """把落在「非消费」分类里的交易，方向压成中性。返回改动笔数。

    必须在分类之后做 —— 方向是解析时定的，那时还不知道分类。
    """
    changed = 0
    for txn in txns:
        if txn.category in NON_CONSUMPTION_CATEGORIES and txn.direction != DIRECTION_NEUTRAL:
            txn.direction = DIRECTION_NEUTRAL
            changed += 1
    return changed


def categorize(txns: Iterable[Txn], ledger, use_ai: bool = False,
               ai_url: str = DEFAULT_AI_URL, ai_model: str = DEFAULT_AI_MODEL,
               ignore_power: bool = False) -> dict:
    """给交易打分类标签。返回统计信息。"""
    stats = {"rules": 0, "cache": 0, "ai": 0, "unmatched": 0, "ai_used": False,
             "neutralised": 0}

    # 先读缓存：同一个商户只判定一次
    cache: dict[str, str] = {
        row["merchant"]: row["category"]
        for row in ledger.conn.execute("SELECT merchant, category FROM merchant_category")
    }

    unmatched: set[str] = set()

    for txn in txns:
        merchant = normalize_text(txn.counterparty)
        if merchant and merchant in cache:
            txn.category = cache[merchant]
            stats["cache"] += 1
            continue

        hit = classify_by_rules(txn.counterparty, txn.item)
        if hit:
            txn.category = hit
            stats["rules"] += 1
            if merchant:
                _remember(ledger, merchant, hit, "rule")
                cache[merchant] = hit
            continue

        txn.category = DEFAULT_CATEGORY
        stats["unmatched"] += 1
        if merchant:
            unmatched.add(merchant)

    if not use_ai or not unmatched:
        stats["neutralised"] = _apply_non_consumption(txns)
        return stats

    if not require_ac(f"本地 AI 分类（{len(unmatched)} 个未识别商户）", override=ignore_power):
        return stats

    if not ai_available(ai_url):
        print(f"本地模型没响应（{ai_url}）。想用 AI 分类的话先装 Ollama 并拉一个模型：")
        print(f"  ollama pull {ai_model}")
        return stats

    merchants = sorted(unmatched)
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
            _remember(ledger, merchant, mapping[merchant], "ai")
            cache[merchant] = mapping[merchant]

    ledger.conn.commit()
    stats["neutralised"] = _apply_non_consumption(txns)
    return stats


def _remember(ledger, merchant: str, category: str, decided_by: str) -> None:
    ledger.conn.execute(
        "INSERT INTO merchant_category (merchant, category, decided_by, decided_at)"
        " VALUES (?,?,?,?) ON CONFLICT(merchant) DO UPDATE SET"
        " category=excluded.category, decided_by=excluded.decided_by,"
        " decided_at=excluded.decided_at",
        (merchant, category, decided_by,
         datetime.now().isoformat(sep=" ", timespec="seconds")),
    )
