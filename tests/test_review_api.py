"""复核网页接口的契约测试（ML-008 的后端一半）。

为什么必须有这个文件：

    原来的 /decide 无论发生什么都回 `{"ok":true}` + HTTP 200：

        ledger.resolve(txn_id, decision)      # 返回值直接被丢掉
        self._send(200, b'{"ok":true}', ...)

    于是三种完全不同的情况在浏览器眼里一模一样：
        真的改掉了
        id 根本不存在
        这条压根不是待复核记录（把一笔正常消费标成重复，它会从报表里消失）
    前端拿不到任何区别，只能假定「成功了」然后跳到下一条 ——
    人工裁定的结果就这么静默丢了。

    更麻烦的是提交是可以被重放、被手写、被两个标签页同时发的。
    所以后端必须自己守住状态：只改 suspect_*，改过的不许翻案，
    重复提交同一个决定要幂等成功。

用法：<venv>\\Scripts\\python.exe tests/test_review_api.py
返回码 0 表示全部通过。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from importer import review_ui  # noqa: E402
from importer.model import Txn  # noqa: E402
from importer.store import Ledger  # noqa: E402

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: 期望 {want!r}，实际 {got!r}")
    print(f"  [{'✓' if ok else '✗'}] {name:<44} -> {got!r}")


BASE = dt.datetime(2026, 9, 3, 12, 0, 0)


def txn(ts, amount, counterparty, txn_id):
    return Txn(source="alipay", ts=ts, amount=Decimal(amount), direction="expense",
               counterparty=counterparty, txn_id=txn_id)


class Server:
    """把复核界面起在本机随机端口上，测真实的 HTTP 行为。"""

    def __init__(self, ledger: Ledger):
        self.httpd, self.token, self.items, self.state = review_ui.build_server(ledger)
        self.ledger = ledger
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def _url(self, path: str, token: str | None) -> str:
        tok = self.token if token is None else token
        return f"http://127.0.0.1:{self.port}{path}?t={tok}"

    def get(self, path: str, token: str | None = None):
        return self._call(urllib.request.Request(self._url(path, token)))

    def post(self, payload, token: str | None = None):
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self._url("/decide", token), data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        return self._call(request)

    def post_raw(self, body: bytes, token: str | None = None):
        request = urllib.request.Request(
            self._url("/decide", token), data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        return self._call(request)

    @staticmethod
    def _call(request):
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def body_of(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:
        return {"_raw": text}


def build_ledger() -> Ledger:
    """两个待复核 + 三条正常记录。

    同一个「时间到分 + 金额 + 对方」出现两次，第二笔就是 suspect_exact。

    连接必须带 check_same_thread=False：复核界面用 ThreadingHTTPServer，
    每个请求在新线程里处理，而连接是在主线程建的。
    生产路径由 secure_store._memory_conn() 统一加上这个参数（那里有详细注释），
    测试要自己加，否则点一次按钮就会撞上
    「SQLite objects created in a thread can only be used in that same thread」。
    """
    led = Ledger(sqlite3.connect(":memory:", check_same_thread=False), ":memory:")
    led.start_batch("alipay", "合成账单.csv", parsed=5)
    led.add(txn(BASE, "28.50", "合成商户甲", "SYN-R-0001"))
    led.add(txn(BASE, "28.50", "合成商户甲", "SYN-R-0002"))      # → suspect（id 2）
    led.add(txn(BASE + dt.timedelta(hours=1), "50.00", "合成商户乙", "SYN-R-0003"))
    led.add(txn(BASE + dt.timedelta(hours=2), "88.80", "合成商户丙", "SYN-R-0004"))
    led.add(txn(BASE + dt.timedelta(hours=2), "88.80", "合成商户丙", "SYN-R-0005"))
    return led


def review_of(ledger: Ledger, txn_id: int) -> str:
    return ledger.conn.execute(
        "SELECT review FROM txn WHERE id = ?", (txn_id,)).fetchone()["review"]


def main() -> int:
    ledger = build_ledger()
    suspects = [r["id"] for r in ledger.list_review()]
    check("合成数据里有 2 笔待复核", len(suspects), 2)

    server = Server(ledger)
    try:
        return run(server, ledger, suspects)
    finally:
        server.stop()


def run(server: Server, ledger: Ledger, suspects: list[int]) -> int:
    first, second = suspects

    print()
    print("=" * 78)
    print("鉴权与请求格式")
    print("=" * 78)
    status, text = server.get("/state", token="wrong-token-xyz")
    check("错令牌读状态 → 403", status, 403)
    status, text = server.post({"id": first, "decision": "dup"}, token="wrong-token-xyz")
    check("错令牌提交 → 403", status, 403)
    status, text = server.post_raw(b"not json")
    check("请求体不是 JSON → 400", status, 400)
    status, text = server.post({"id": first, "decision": "随便写的"})
    check("decision 不认识 → 400", status, 400)
    check("400 时库里没被改动", review_of(ledger, first).startswith("suspect"), True)

    print()
    print("=" * 78)
    print("正常裁定：200 + ok/id/updated")
    print("=" * 78)
    status, text = server.post({"id": first, "decision": "dup"})
    body = body_of(text)
    check("HTTP 200", status, 200)
    check("ok", body.get("ok"), True)
    check("回显 id", body.get("id"), first)
    check("updated 为真", body.get("updated"), True)
    check("库里的标记", review_of(ledger, first), "confirmed_dup")
    check("计数器 +1", server.state["resolved"], 1)

    print()
    print("=" * 78)
    print("重复提交：幂等成功，但 updated 为假，计数不再涨")
    print("=" * 78)
    status, text = server.post({"id": first, "decision": "dup"})
    body = body_of(text)
    check("HTTP 200（重放不算错误）", status, 200)
    check("ok", body.get("ok"), True)
    check("updated 为假", body.get("updated"), False)
    check("计数器没变", server.state["resolved"], 1)
    check("库里的标记没被改坏", review_of(ledger, first), "confirmed_dup")

    print()
    print("=" * 78)
    print("翻案：已定案的记录不允许被另一个决定覆盖")
    print("=" * 78)
    status, text = server.post({"id": first, "decision": "ok"})
    body = body_of(text)
    check("HTTP 409", status, 409)
    check("错误码", body.get("error"), "already_decided")
    check("ok 为假", body.get("ok"), False)
    check("库里的标记没变", review_of(ledger, first), "confirmed_dup")

    print()
    print("=" * 78)
    print("不存在的 id：404，且不能说 ok")
    print("=" * 78)
    status, text = server.post({"id": 987654, "decision": "dup"})
    body = body_of(text)
    check("HTTP 404", status, 404)
    check("错误码", body.get("error"), "not_found")
    check("ok 为假（原来的实现这里回的是 ok:true）", body.get("ok"), False)

    print()
    print("=" * 78)
    print("不是待复核的记录：409，绝不改动")
    print("=" * 78)
    normal = [r["id"] for r in ledger.conn.execute(
        "SELECT id FROM txn WHERE review = '' ORDER BY id")]
    check("有正常记录可测", len(normal) > 0, True)
    status, text = server.post({"id": normal[0], "decision": "dup"})
    body = body_of(text)
    check("HTTP 409", status, 409)
    check("错误码", body.get("error"), "not_reviewable")
    check("正常记录仍是正常记录", review_of(ledger, normal[0]), "")
    check("没有被误标成重复", review_of(ledger, normal[0]) == "confirmed_dup", False)

    print()
    print("=" * 78)
    print("另一笔待复核照常能处理")
    print("=" * 78)
    status, text = server.post({"id": second, "decision": "ok"})
    body = body_of(text)
    check("HTTP 200", status, 200)
    check("updated 为真", body.get("updated"), True)
    check("库里的标记", review_of(ledger, second), "confirmed_ok")
    check("counter", server.state["resolved"], 2)

    print()
    print("=" * 78)
    print("裁定不该抹掉「为什么怀疑重复」")
    print("=" * 78)
    note = ledger.conn.execute(
        "SELECT review_note FROM txn WHERE id = ?", (first,)).fetchone()["review_note"]
    check("判定依据还在", bool(note), True)

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
    sys.exit(main())
