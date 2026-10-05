"""本地 AI 分类的边界（ML-005）。

AI 兜底是本项目唯一一处**主动把账单内容交给另一个程序**的地方。
它的数据去了哪里、带了什么出去，必须钉死。

这里验四件事：

    1. **非本机地址一律拒绝**。这是整个隐私模型的第一道防线 ——
       工具按设计「全程离线」，唯一的例外是本机 Ollama。
       所以各种绕写法都要试：大写、 IPv6、形似本机的域名、
       藏在 userinfo 里的域名、十进制 IP。
    2. **拒绝的原因要是真原因**。传了非本机地址却被说成
       「模型没响应」，用户就会去反复重启服务 —— 处置方式完全反了。
       见项目铁律：报错必须指向真实原因。
    3. **发出去的只有商户名**。不许夹带金额、时间、单号、备注。
       这条防的是后来者为「提高准确率」往 prompt 里加字段 ——
       加了之后账单就多了一份明文副本在别人的日志里，而且没人知道。
    4. **发出去之前要说清代价**。提示里必须点明「明文」「日志」「不加密」。
       前提很硬：商户名不能遮（遮了分类就失效），所以只能讲清楚。

用法：<venv>\\Scripts\\python.exe tests/test_ai_boundary.py
返回码 0 表示全部通过。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from importer import categorize  # noqa: E402
from importer.categorize import ai_classify  # noqa: E402
from importer.model import DIRECTION_EXPENSE, Txn  # noqa: E402
from importer.store import Ledger  # noqa: E402

FAILURES: list[str] = []

MERCHANTS = ["星际网络科技有限公司", "爪哇岛某某服务有限公司"]

# 这俩是本该带的东西。任何一个出现在 AI 请求里，都说明有人为了「更准」
# 往 prompt 里塞了交易字段 —— 那样账单就多了一份明文副本在别人的日志里。
FORBIDDEN = ("50.00", "2026-09-03", "SYN-CL-0001", "日用品")


def fresh_ledger() -> Ledger:
    ledger = Ledger(sqlite3.connect(":memory:"), ":memory:")
    ledger.start_batch("ai-test", "synthetic.csv", parsed=0)
    return ledger


def txn(counterparty: str) -> Txn:
    import datetime as dt
    from decimal import Decimal
    return Txn(source="alipay", ts=dt.datetime(2026, 9, 3, 12, 0, 0),
               amount=Decimal("50.00"), direction=DIRECTION_EXPENSE,
               counterparty=counterparty, item="日用品", txn_id="SYN-CL-0001")


def check(name: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: 期望 {want!r}，实际 {got!r}")
    print(f"  [{'✓' if ok else '✗'}] {name:<46} -> {got!r}")


def captured(fn):
    """跑一个函数，把它的标准输出捞回来。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn()
    return result, buf.getvalue()


class _FakeResponse:
    def __init__(self, body: dict):
        self._raw = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def run() -> int:
    print()
    print("=" * 78)
    print("1. 非本机地址一律拒绝")
    print("=" * 78)

    for url in ("http://192.168.1.5:11434", "http://ollama.internal:11434",
                "https://api.openai.com/v1", "http://localhost.evil.com",
                "http://127.0.0.1.nip.io:11434", "http://2130706433",
                # 带凭据的一律拒：本机模型不需要用户名密码，
                # 出现 userinfo 说明要么是抄错配置，要么是在试探能不能投毒。
                "http://user:pw@127.0.0.1:11434", "http://evil.com@127.0.0.1:11434"):
        _, err = captured(lambda: None)
        try:
            ai_classify(MERCHANTS, url=url)
            check(f"{url} 被拒掉了", "没有拒绝", "应当抛 ValueError")
        except ValueError:
            check(f"{url} 被拒掉了", True, True)
        except Exception as exc:                      # noqa: BLE001
            check(f"{url} 被拒掉了", type(exc).__name__, "ValueError")

    print()
    print("=" * 78)
    print("2. 本机地址的常见写法要放行")
    print("=" * 78)
    for url in ("http://127.0.0.1:11434", "http://LOCALHOST:11434",
                "http://[::1]:11434", "http://127.0.0.1"):
        try:
            categorize._assert_local(url)
            check(f"{url} 放行", True, True)
        except ValueError as exc:
            check(f"{url} 放行", f"被拒：{exc}", True)

    print()
    print("=" * 78)
    print("3. 拒绝的原因必须是真原因（不能被说成「模型没响应」）")
    print("=" * 78)
    try:
        categorize.ai_available("http://192.168.1.5:11434")
        check("非法地址抛异常而不是返回 False", "返回了值", "应当抛 ValueError")
    except ValueError as exc:
        check("非法地址抛异常而不是返回 False", "拒绝连" in str(exc), True)

    print()
    print("=" * 78)
    print("4. 发出去的只有商户名")
    print("=" * 78)
    seen: dict = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = req.data.decode("utf-8")
        return _FakeResponse({"response": json.dumps(
            {MERCHANTS[0]: "日用百货", MERCHANTS[1]: "数码电子"},
            ensure_ascii=False)})

    real_urlopen = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen            # type: ignore[assignment]
    try:
        mapping = ai_classify(MERCHANTS, url="http://127.0.0.1:11434")
    finally:
        urllib.request.urlopen = real_urlopen        # type: ignore[assignment]

    check("模型判定的结果拿回来了", len(mapping), 2)

    # json.dumps 默认会把中文转成 \uXXXX，所以先解回来再看内容 ——
    # 直接搜二进制字节会得出「商户名没在里面」的错误结论。
    payload = json.loads(seen.get("body", "{}"))
    prompt = payload.get("prompt", "")
    check("prompt 里有两个商户名",
          all(m in prompt for m in MERCHANTS), True)
    check("prompt 里没有金额/日期/单号/备注",
          [word for word in FORBIDDEN if word in prompt], [])
    check("请求体里除 model/prompt/format/options 外不多带东西",
          sorted(payload), sorted(["model", "prompt", "stream", "format", "options"]))
    check("温度设成 0（分类要稳定，不要每次跑出不同结果）",
          payload.get("options", {}).get("temperature"), 0)
    check("只找了 generate，没有顺手带别的端点",
          seen.get("url", "").endswith("/api/generate"), True)

    print()
    print("=" * 78)
    print("5. 发之前必须讲清代价（ML-005 的核心）")
    print("=" * 78)

    # 真跑一遍 categorize() 的 AI 分支，看它到底打印了什么。
    # 关键是：**发出去之前**必须先把代价讲清楚，而不是悄悄发完。
    ledger = fresh_ledger()
    rows = [txn(counterparty=m) for m in MERCHANTS]

    sent: list[bool] = []

    def fake_urlopen(req, timeout=None):
        sent.append(True)
        return _FakeResponse({"response": json.dumps(
            {MERCHANTS[0]: "日用百货", MERCHANTS[1]: "数码电子"},
            ensure_ascii=False)})

    real_urlopen = urllib.request.urlopen
    real_power = categorize.require_ac
    urllib.request.urlopen = fake_urlopen            # type: ignore[assignment]
    categorize.require_ac = lambda *a, **k: True     # 电源闸门测试里不拦
    try:
        _, printed = captured(
            lambda: categorize.categorize(rows, ledger, use_ai=True,
                                          ai_url="http://127.0.0.1:11434"))
    finally:
        urllib.request.urlopen = real_urlopen        # type: ignore[assignment]
        categorize.require_ac = real_power

    check("确实发起了请求（否则下面的断言都没意义）", len(sent) > 0, True)
    for keyword in ("明文", "日志", "不加密", "setcat"):
        check(f"提示里提到了「{keyword}」", keyword in printed, True)
    check("提示里点明了数据到本地模型的去向",
          "127.0.0.1:11434" in printed, True)
    # 这件事是 ML-005 的核心：原因是遮不了，所以只能讲清楚
    check("提示里解释了为什么不脱敏商户名", "遮掉" in printed, True)
    check("AI 判定的分类落到了交易上",
          "数码电子" in [t.category for t in rows], True)

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
