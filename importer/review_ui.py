"""疑似重复的快速裁定界面：本地网页，两个大按钮。

为什么不在终端里做：
    「两个大按钮」要的是肌肉记忆级别的快——扫一眼、按一下、下一笔。
    终端里的 y/n 提示做不到这个，而且长单号、商户名在终端里对齐很难看。

为什么用本地网页而不是别的：
    * 只绑 127.0.0.1，外部访问不到；URI 里带一次性随机令牌，
      本机其它进程猜到地址也动不了你的账本（防的是「随便点一下」这类误操作）。
    * 页面完全自包含，不引任何外部资源，不联网。看完关掉就行。
    * 不留任何文件——页面是现生成的，不落盘。

交互设计：
    一次只出一笔，左右并排显示两笔交易，**差异字段高亮**。
    两个大按钮：左边「是同一笔」、右边「是两笔独立」。
    键盘 1 / 2（或 ← / →）即可，手不用离开键盘。

裁定结果只改数据库里的 review 标记，**不删除任何记录**：
    confirmed_dup  确认重复 → 从统计里剔除，原始记录仍在库里
    confirmed_ok   确认独立 → 不再提示
"""

from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .model import SOURCE_LABELS
from .render import money
from .store import Ledger

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MyLedger 疑似重复确认</title>
<style>
  :root {
    --bg: #f7f7f5; --card: #ffffff; --line: #e3e2dd; --text: #1f1f1d;
    --muted: #6b6a66; --accent: #b3261e; --accent2: #146c43;
    --hl: #fff3cd; --hlline: #e0b400;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font-family: system-ui, "Microsoft YaHei", sans-serif; }
  header { padding: 18px 28px; border-bottom: 1px solid var(--line);
           background: var(--card); display: flex; align-items: center;
           justify-content: space-between; gap: 16px; flex-wrap: wrap; }
  h1 { font-size: 17px; font-weight: 500; margin: 0; }
  .progress { font-size: 14px; color: var(--muted); }
  .bar { height: 4px; background: var(--line); border-radius: 2px; overflow: hidden;
         margin-top: 10px; }
  .bar > i { display: block; height: 100%; background: var(--accent2); }
  main { max-width: 1000px; margin: 0 auto; padding: 28px; }
  .reason { background: #eef4fb; border: 1px solid #cfe0f3; border-radius: 10px;
            padding: 12px 16px; font-size: 14px; margin-bottom: 20px; }
  .pair { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  @media (max-width: 760px) { .pair { grid-template-columns: 1fr; } }
  .txn { background: var(--card); border: 1px solid var(--line); border-radius: 14px;
         padding: 18px 20px; }
  .txn .src { font-size: 13px; color: var(--muted); margin-bottom: 12px; }
  .txn .amount { font-size: 30px; font-weight: 500; letter-spacing: -0.5px; }
  .txn dl { margin: 14px 0 0; display: grid; grid-template-columns: 68px 1fr;
            gap: 8px 12px; font-size: 14px; }
  .txn dt { color: var(--muted); }
  .txn dd { margin: 0; word-break: break-all; }
  .same { background: var(--hl); border-bottom: 2px solid var(--hlline);
          border-radius: 3px; padding: 0 3px; }
  .actions { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-top: 26px; }
  button.big { border: none; border-radius: 14px; padding: 26px 20px; font-size: 19px;
               font-weight: 500; cursor: pointer; color: #fff; text-align: center;
               line-height: 1.5; transition: filter .12s; }
  button.big:hover { filter: brightness(1.08); }
  button.big:active { filter: brightness(.94); }
  button.big small { display: block; font-size: 12px; font-weight: 400;
               opacity: .85; margin-top: 6px; }
  .dup { background: var(--accent); }
  .ok { background: var(--accent2); }
  .kbd { display: inline-block; border: 1px solid rgba(255,255,255,.5);
         border-radius: 4px; padding: 0 5px; font-size: 11px; margin-right: 6px; }
  .done { text-align: center; padding: 60px 20px; }
  .done h2 { font-size: 22px; font-weight: 500; }
  .done p { color: var(--muted); font-size: 14px; }
  .notice { max-width: 1000px; margin: 16px auto 0; padding: 12px 16px;
            border-radius: 10px; font-size: 14px; line-height: 1.6;
            background: #fdecea; border: 1px solid #f3b6b0; color: #7a1c15;
            display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .notice[hidden] { display: none; }
  .notice button { border: 1px solid currentColor; background: transparent;
                   color: inherit; border-radius: 8px; padding: 4px 12px;
                   font-size: 13px; cursor: pointer; font-family: inherit; }
  footer { text-align: center; color: var(--muted); font-size: 12px;
           padding: 24px; }
  .empty { text-align: center; padding: 60px; color: var(--muted); }
</style>
</head>
<body>
<header>
  <div>
    <h1>疑似重复确认</h1>
    <div class="progress" id="progress">正在加载…</div>
  </div>
  <div class="progress">按 <b>1</b> = 同一笔 &nbsp;·&nbsp; 按 <b>2</b> = 两笔独立</div>
</header>
<div class="bar"><i id="bar" style="width:0%"></i></div>
<div class="notice" id="err" hidden></div>
<main id="main"><div class="empty">正在加载…</div></main>
<footer>所有裁定只改标记，不删除任何记录。关掉本页即可，改动已即时保存。</footer>

<script>
const TOKEN = "__TOKEN__";
const TOTAL = __TOTAL__;
let items = [], idx = 0, deciding = false;

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function field(label, value, highlight) {
  const cls = highlight ? "same" : "";
  return "<dt>" + esc(label) + "</dt><dd class='" + cls + "'>" +
         (esc(value) || "—") + "</dd>";
}

function card(t, other) {
  const same = (k) => other && String(t[k]) === String(other[k]);
  return "<div class='txn'>" +
    "<div class='src'>" + esc(t.source_label) + "</div>" +
    "<div class='amount'>" + esc(t.amount) + " 元</div>" +
    "<dl>" +
      field("时间", t.ts, same("ts")) +
      field("对方", t.counterparty, same("counterparty")) +
      field("商品", t.item, same("item")) +
      field("方式", t.method, same("method")) +
      field("单号", t.txn_id || "（无）", same("txn_id")) +
    "</dl></div>";
}

function render() {
  const main = document.getElementById("main");
  document.getElementById("progress").textContent =
    "第 " + Math.min(idx + 1, TOTAL) + " / " + TOTAL + " 笔";
  document.getElementById("bar").style.width =
    (TOTAL ? (idx / TOTAL * 100) : 0) + "%";

  if (idx >= items.length) {
    main.innerHTML =
      "<div class='done'><h2>全部处理完了</h2>" +
      "<p>共确认 " + TOTAL + " 笔。可以关掉这个页面了。</p></div>";
    return;
  }

  const it = items[idx];
  const other = it.other;
  main.innerHTML =
    "<div class='reason'><b>为什么怀疑重复：</b>" + esc(it.reason) + "</div>" +
    "<div class='pair'>" + card(it.self, other) +
      (other ? card(other, it.self) : "<div class='txn'><div class='src'>未找到对应记录</div></div>") +
    "</div>" +
    "<div class='actions'>" +
      "<button class='big dup' onclick='decide(\"dup\")'>" +
        "<span class='kbd'>1</span>是同一笔<small>从统计里剔除这一笔，记录仍然保留</small></button>" +
      "<button class='big ok' onclick='decide(\"ok\")'>" +
        "<span class='kbd'>2</span>是两笔独立交易<small>保留两笔，以后不再提示</small></button>" +
    "</div>";
}

// 后端的固定错误码 → 给人看的话。
// 「永久性」的失败（这条已经不在列表里、不是待复核、已经裁定过）
// 重试一万次也不会成功，所以给一个「跳过这条」的出口；
// 网络抖动和服务端 5xx 只提示重试 —— 那两种真的会自己好。
const PERMANENT = {
  "not_found": "这条已经不在待确认列表里了（可能被另一个窗口处理过）。",
  "not_reviewable": "这条不是「疑似重复」，不能在这里裁定。",
  "already_decided": "这条已经裁定过了，不能翻案。"
};

function showError(text, canSkip) {
  const box = document.getElementById("err");
  box.innerHTML = "<span>" + esc(text) + "</span>" +
    (canSkip ? "<button onclick='skipCurrent()'>跳过这条</button>" : "");
  box.hidden = false;
}

function clearError() {
  const box = document.getElementById("err");
  box.hidden = true;
  box.innerHTML = "";
}

function skipCurrent() {
  clearError();
  idx++;
  render();
}

async function decide(decision) {
  if (deciding || idx >= items.length) return;
  deciding = true;
  clearError();
  const it = items[idx];
  let failure = null;
  try {
    const response = await fetch("/decide?t=" + encodeURIComponent(TOKEN), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: it.id, decision: decision })
    });

    // fetch 只在网络层失败时抛异常 —— HTTP 400/403/404/409/500 都会正常返回。
    // 不检查 response.ok 的话，「没改成功」和「改好了」在代码里长得一模一样，
    // 页面照样跳到下一条，用户以为这一笔记下了，裁定其实已经丢了。
    if (!response.ok) {
      let code = "";
      try {
        const body = await response.json();
        code = (body && body.error) || "";
      } catch (e) { code = ""; }
      failure = {
        permanent: Boolean(PERMANENT[code]),
        text: PERMANENT[code] || ("裁定没有保存（HTTP " + response.status + "），请重试。")
      };
    }
  } catch (e) {
    failure = { permanent: false, text: "连不上本地服务，裁定没有保存。请重试。" };
  }

  deciding = false;
  if (failure) {
    // 索引不动：当前这条留在屏幕上等重试或手动跳过。
    // 这里千万不能 idx++ —— 那正是「请求失败仍然跳过当前记录」的老毛病。
    showError(failure.text, failure.permanent);
    return;
  }
  idx++;
  render();
}

document.addEventListener("keydown", (e) => {
  if (e.key === "1" || e.key === "ArrowLeft") decide("dup");
  if (e.key === "2" || e.key === "ArrowRight") decide("ok");
});

(async function load() {
  try {
    const r = await fetch("/state?t=" + encodeURIComponent(TOKEN));
    if (!r.ok) throw new Error("HTTP " + r.status);
    const d = await r.json();
    items = d.items || [];
  } catch (e) {
    document.getElementById("main").innerHTML =
      "<div class='empty'>读不到待确认列表：连不上本地服务。<br>" +
      "关掉本页，重新运行一次确认界面即可。</div>";
    return;
  }
  render();
})();
</script>
</body>
</html>
"""


def _txn_dict(row) -> dict:
    return {
        "id": row["id"],
        "source_label": SOURCE_LABELS.get(row["source"], row["source"]),
        "ts": row["ts"],
        "amount": money(row["amount"]),
        "counterparty": row["counterparty"],
        "item": row["item"],
        "method": row["method"],
        "txn_id": row["txn_id"],
    }


def collect(ledger: Ledger, limit: int = 500) -> list:
    """把待确认的疑似重复整理成界面需要的数据（两笔并排）。"""
    out = []
    for row in ledger.list_review(limit=limit):
        other = ledger.review_counterpart(row)
        out.append({
            "id": row["id"],
            "reason": row["review_note"] or "疑似重复",
            "self": _txn_dict(row),
            "other": _txn_dict(other) if other else None,
        })
    return out


def build_server(ledger: Ledger, *, port: int = 0, host: str = "127.0.0.1",
                 timeout_seconds: int = 3600):
    """建好服务但不启动。返回 (httpd, token, items)。

    单独拆出来是为了能测：`run()` 会开浏览器、阻塞在 serve_forever 上，
    在自动化测试里没法用。有了这个函数，测试可以直接打 HTTP 接口验证。
    """
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(f"拒绝绑定 {host}：这个界面只允许本机访问。")

    items = collect(ledger)
    token = secrets.token_urlsafe(24)
    page = PAGE.replace("__TOKEN__", token).replace("__TOTAL__", str(len(items)))
    state = {"resolved": 0, "total": len(items)}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "MyLedgerReview/1.0"

        def _authorised(self) -> bool:
            from urllib.parse import parse_qs, urlparse
            query = parse_qs(urlparse(self.path).query)
            return query.get("t", [""])[0] == token

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass    # 默认日志会刷屏，还会把 URL（含令牌）写进控制台

        def do_GET(self) -> None:
            from urllib.parse import urlparse
            path = urlparse(self.path).path
            if not self._authorised():
                self._send(403, "forbidden".encode(), "text/plain; charset=utf-8")
                return
            if path == "/":
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/state":
                body = json.dumps({"items": items, "total": state["total"]},
                                  ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")

        def do_POST(self) -> None:
            from urllib.parse import urlparse
            if not self._authorised():
                self._send(403, b"forbidden", "text/plain; charset=utf-8")
                return
            if urlparse(self.path).path != "/decide":
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                txn_id = int(payload["id"])
                if payload["decision"] not in ("dup", "ok"):
                    raise ValueError("decision 只能是 dup 或 ok")
                decision = "confirmed_dup" if payload["decision"] == "dup" else "confirmed_ok"
            except Exception:
                self._send(400, b"bad request", "text/plain; charset=utf-8")
                return

            # 一把锁守住所有账本操作。
            # ThreadingHTTPServer 每个请求一个线程，而 sqlite 连接是跨线程共用的
            # （见 secure_store._memory_conn）。理论上一次只点一个按钮不会撞，
            # 但浏览器可能重发请求、或者你手快点了两下——不加锁迟早撞上。
            with lock:
                result = ledger.resolve_review(txn_id, decision)
                if result == "updated":
                    state["resolved"] += 1
                # 关界面的判据是「库里还剩几笔待确认」，不是「收到过几次提交」。
                #
                # 用提交次数当判据会出两种错：双击一次就把界面提前关掉
                # （同一个决定被数了两遍），以及列表里有重复项时永远关不掉。
                # 直接问库要真话最省心。
                remaining = ledger.conn.execute(
                    "SELECT COUNT(*) AS n FROM txn WHERE review LIKE 'suspect%'"
                ).fetchone()["n"]
                done = remaining == 0

            # 每一种结局都给出不同的 HTTP 状态码。
            #
            # 为什么不能都回 200：fetch() 不会因为 4xx/5xx 抛异常，
            # 前端只能靠 response.ok 判断成败。全都回 200 的话，
            # 「改好了」「id 不存在」「这条不该由你改」在页面上长得一模一样，
            # 用户看到跳到了下一条，就以为这一笔已经记下了。
            if result == "not_found":
                self._reply({"ok": False, "error": "not_found", "id": txn_id}, 404)
                return
            if result == "not_reviewable":
                self._reply({"ok": False, "error": "not_reviewable", "id": txn_id}, 409)
                return
            if result == "already_decided":
                self._reply({"ok": False, "error": "already_decided", "id": txn_id}, 409)
                return

            body = {"ok": True, "id": txn_id, "updated": result == "updated"}
            if done:
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            self._reply(body, 200)

        def _reply(self, payload: dict, code: int) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(code, body, "application/json; charset=utf-8")

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.timeout = timeout_seconds
    return httpd, token, items, state


def run(ledger: Ledger, *, port: int = 0, open_browser: bool = True,
        host: str = "127.0.0.1", timeout_seconds: int = 3600) -> int:
    """起一个只对本机可见的小服务，浏览器里做裁定。返回处理的笔数。

    host 默认且只允许 127.0.0.1。绑到 0.0.0.0 等于把你的账单摊给整个局域网，
    这里不提供这个选项——真要这么做，请自己改代码并承担后果。
    """
    httpd, token, items, state = build_server(
        ledger, port=port, host=host, timeout_seconds=timeout_seconds)

    if not items:
        httpd.server_close()
        print("没有待确认的疑似重复。")
        return 0

    actual_port = httpd.server_address[1]
    url = f"http://{host}:{actual_port}/?t={token}"

    print(f"已启动本地确认界面：{url}")
    print(f"  共 {len(items)} 笔待确认。")
    print("  只监听本机（127.0.0.1），不联网，不写任何临时文件。")
    print("  一次只出一笔，点左边「是同一笔」或右边「是两笔独立」即可；")
    print("  也可以按键盘 1 / 2。全部确认完会自动关闭。")

    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n手动中断。")
    finally:
        httpd.server_close()

    print(f"本轮确认了 {state['resolved']} 笔。")
    return state["resolved"]
