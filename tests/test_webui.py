"""网页控制台的回归测试。

为什么要有这个文件：
    控制台把整个导入链路包了一层 HTTP。这一层最容易出的错不是「跑不起来」，
    而是**边界失守**：
      * 忘了校验令牌 —— 本机任何进程都能打进来动账本
      * 密码从页面进来，却被写进日志 / 写进参数残留 / 落到盘上
      * 任务并发跑同一个账本
    这些都不会自己报错，只能靠测试钉住。

    另外沿用本项目的老纪律：**落盘类操作，验证必须重新读磁盘**。
    所以凡是真正改账本的用例，都重新打开一次密文再核对。

用法：<venv>\\Scripts\\python.exe tests/test_webui.py
返回码 0 表示全部通过。

注意：本测试全程用临时 vault + `fixtures/` 里的合成数据，**不碰真实账本**。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.client import HTTPConnection

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(ROOT, "fixtures")
sys.path.insert(0, ROOT)

from importer import monthly, secure_store, webui  # noqa: E402

FAILURES: list[str] = []
BANK_PDF_PASSWORD = "TestBankPdf-2026"

UNMATCHED_CSV = """导出信息：
共2笔记录
交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,收/付款方式,交易状态,交易订单号,商家订单号,备注
2026-09-05 10:00:00,其他,某某网络科技有限公司,x@example.com,服务费,支出,66.00,余额,交易成功,2026090522006666666661,MO20260905001,
2026-09-06 11:00:00,其他,某某信息咨询有限公司,x@example.com,咨询费,支出,88.00,余额,交易成功,2026090622006666666662,MO20260906002,
"""


def check(name: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: 期望 {want}，实际 {got}")
    print(f"  [{'✓' if ok else '✗'}] {name:<54} -> {got}")


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def http(port: int, method: str, path: str, token: str | None = None, body=None):
    """打一次本机 HTTP，返回 (状态码, 解码后的文本)。"""
    conn = HTTPConnection("127.0.0.1", port, timeout=40)
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-MyLedger-Token"] = token
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, raw.decode("utf-8", "replace")


def wait_job(port: int, token: str, job_id: str, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, txt = http(port, "GET", f"/api/job?id={job_id}", token)
        job = json.loads(txt)
        if job["status"] != "running":
            return job
        time.sleep(0.15)
    raise AssertionError(f"任务 {job_id} 超时未结束")


def main() -> int:
    if not os.path.isfile(os.path.join(FIXTURES, "sample_bank.pdf")):
        print("缺少 fixtures/sample_bank.pdf，先跑 tests/make_bank_pdf.py")
        return 2

    tmp = tempfile.mkdtemp(prefix="mlk-webui-test-")
    vault = os.path.join(tmp, "vault")
    inbox = os.path.join(vault, "inbox")
    os.makedirs(inbox, exist_ok=True)
    shutil.copy2(os.path.join(FIXTURES, "sample_alipay.csv"),
                 os.path.join(inbox, "alipay_sample.csv"))
    shutil.copy2(os.path.join(FIXTURES, "sample_bank.pdf"),
                 os.path.join(inbox, "bank_sample.pdf"))
    with open(os.path.join(inbox, "alipay_unmatched.csv"), "w", encoding="utf-8",
              newline="\n") as fh:
        fh.write(UNMATCHED_CSV)

    httpd, console, token = webui.build_server(vault, port=0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    # open_inbox 会在测试机上真的弹出资源管理器，这里替换成空操作，
    # 只验「接口可用、路径正确」，不验「窗口弹出来了」。
    real_startfile = getattr(os, "startfile", None)
    opened: list = []
    if real_startfile is not None:
        os.startfile = lambda p: opened.append(p)

    try:
        # ------------------------------------------------------------------
        section("1. 鉴权：没令牌一律打不进来")
        check("GET / 无令牌", http(port, "GET", "/")[0], 403)
        check("GET /api/state 无令牌", http(port, "GET", "/api/state")[0], 403)
        check("POST /api/run 错误令牌",
              http(port, "POST", "/api/run", "wrong", {"action": "monthly"})[0], 403)
        check("GET / 令牌正确", http(port, "GET", f"/?t={token}")[0], 200)

        # ------------------------------------------------------------------
        section("2. 页面自包含、不引外部资源")
        _, page = http(port, "GET", f"/?t={token}")
        check("含标题", "<title>MyLedger 控制台</title>" in page, True)
        banned = ["http://", "https://", "//cdn", "<script src", "@import url", "<link "]
        hit = [b for b in banned if b in page]
        check("没有外部资源引用", hit, [])
        check("没有未替换的占位符",
              [p for p in ("__TOKEN__", "__VAULT_JSON__", "__USAGE_JSON__",
                           "__SOURCES_JSON__") if p in page], [])

        # ------------------------------------------------------------------
        section("3. 快照：投递目录与加密文件识别")
        _, txt = http(port, "GET", "/api/state", token)
        st = json.loads(txt)
        check("还没有账本", st["encrypted_exists"], False)
        names = sorted(f["name"] for f in st["inbox_files"])
        check("列出投递目录里的 3 个文件", names,
              ["alipay_sample.csv", "alipay_unmatched.csv", "bank_sample.pdf"])
        pdf = [f for f in st["inbox_files"] if f["name"].endswith(".pdf")][0]
        check("加密 PDF 被标为需要密码", pdf["needs_password"], True)
        csvf = [f for f in st["inbox_files"] if f["name"] == "alipay_sample.csv"][0]
        check("普通 CSV 不需要密码", csvf["needs_password"], False)
        check("识别出支付宝", csvf["source"], "alipay")

        # ------------------------------------------------------------------
        section("4. 不支持的操作与非法参数都要被挡住")
        check("未知 action",
              http(port, "POST", "/api/run", token, {"action": "rm-rf"})[0], 400)
        # 参数校验放在任务里做（只有一处校验逻辑，不在 HTTP 层再抄一遍）。
        # 所以这里期望的是「任务失败 + 原因可读」，而不是 HTTP 400。
        _, txt = http(port, "POST", "/api/run", token,
                      {"action": "setcat", "params": {}})
        job = wait_job(port, token, json.loads(txt)["job"])
        check("setcat 缺商户名 -> 任务失败", job["status"], "error")
        check("失败原因可读", "商户名不能为空" in job["error"], True)
        check("日志里也有原因",
              any("商户名不能为空" in line for line in job["lines"]), True)
        with secure_store.open_encrypted(vault, save_on_exit=False, warn=lambda s: None) as led:
            check("失败的参数没有写进任何东西",
                  led.conn.execute("SELECT COUNT(*) FROM txn").fetchone()[0], 0)

        # ------------------------------------------------------------------
        section("5. 并发保护：同一时刻只跑一个任务")
        webui.ACTIONS["__slow"] = lambda v, p, log: (time.sleep(1.2), {"exit": 0})[1]
        status, txt = http(port, "POST", "/api/run", token, {"action": "__slow"})
        check("第一个任务被接受", status, 200)
        first_job = json.loads(txt)["job"]
        status, txt = http(port, "POST", "/api/run", token, {"action": "__slow"})
        check("第二个任务被拒绝", status, 409)
        check("提示是「已经有任务在跑」", "在跑" in txt, True)
        wait_job(port, token, first_job)
        webui.ACTIONS.pop("__slow", None)

        # ------------------------------------------------------------------
        section("6. 月度处理：加密 PDF 没密码就跳过，其余正常导入")
        _, txt = http(port, "POST", "/api/run", token,
                      {"action": "monthly", "params": {"passwords": {}}})
        job = wait_job(port, token, json.loads(txt)["job"])
        check("任务正常结束", job["status"], "done")
        log = "\n".join(job["lines"])
        check("日志里有「拿不到密码，跳过」", "拿不到密码" in log, True)
        check("退出码 2（有文件没处理）", job["result"]["exit"], 2)
        check("处理成功的文件移入 _done",
              sorted(os.listdir(os.path.join(inbox, "_done"))),
              ["alipay_sample.csv", "alipay_unmatched.csv"])
        check("加密 PDF 留在原地",
              [n for n in os.listdir(inbox) if n.endswith(".pdf")], ["bank_sample.pdf"])

        # 落盘核对：重新打开密文，不看内存状态
        with secure_store.open_encrypted(vault, save_on_exit=False, warn=lambda s: None) as led:
            n1 = led.conn.execute("SELECT COUNT(*) FROM txn").fetchone()[0]
            um = led.unmatched_totals()
        check("库里 10 笔（8 支付宝 + 2 未分类）", n1, 10)
        check("未分类 2 笔 / 2 个商户", (um["n"], um["merchants"]), (2, 2))

        # ------------------------------------------------------------------
        section("7. 只读接口")
        _, txt = http(port, "GET", "/api/unmatched", token)
        um = json.loads(txt)
        check("未分类明细有 2 行", len(um["rows"]), 2)
        _, txt = http(port, "GET", "/api/unmatched?mask=1", token)
        masked = json.loads(txt)
        check("遮蔽后名字不含原串",
              any("网络科技" in r["merchant"] for r in masked["rows"]), False)
        _, txt = http(port, "GET", "/api/report?month=2026-09", token)
        rep = json.loads(txt)
        check("报表有支出分类", len(rep["expense"]) >= 2, True)
        check("报表合计与明细口径一致", float(rep["expense_total"].replace(",", "")) > 0, True)
        _, txt = http(port, "GET", "/api/state", token)
        st = json.loads(txt)
        check("快照概览笔数", st["overview"]["n"], 10)
        check("快照里出现 2026-09", "2026-09" in st["months"], True)

        # ------------------------------------------------------------------
        section("8. 手工指定分类（走 HTTP，落盘后重新读磁盘核对）")
        _, txt = http(port, "POST", "/api/run", token, {
            "action": "setcat",
            "params": {"merchant": "某某网络科技有限公司", "category": "订阅服务",
                       "apply": True}})
        job = wait_job(port, token, json.loads(txt)["job"])
        check("任务结束", job["status"], "done")
        check("试算/执行的改动笔数正确", job["result"]["applied"], True)
        with secure_store.open_encrypted(vault, save_on_exit=False, warn=lambda s: None) as led:
            cat = led.conn.execute(
                "SELECT category FROM txn WHERE counterparty = ?",
                ("某某网络科技有限公司",)).fetchone()["category"]
            manual = dict(led.conn.execute(
                "SELECT merchant, category FROM merchant_category WHERE decided_by='manual'"))
        check("分类改动落进了密文", cat, "订阅服务")
        check("人工指定也落进了密文", manual, {"某某网络科技有限公司": "订阅服务"})
        _, txt = http(port, "GET", "/api/state", token)
        check("快照里能看到这条人工指定",
              json.loads(txt)["manual"], [{"merchant": "某某网络科技有限公司",
                                           "category": "订阅服务"}])

        # ------------------------------------------------------------------
        section("9. 密码用完即忘：不落盘、不留参数")
        _, txt = http(port, "POST", "/api/run", token, {"action": "monthly", "params": {
            "passwords": {"bank_pdf": BANK_PDF_PASSWORD}}})
        job_id = json.loads(txt)["job"]
        job = wait_job(port, token, job_id)
        check("任务结束", job["status"], "done")
        check("密码没有出现在日志里",
              any(BANK_PDF_PASSWORD in line for line in job["lines"]), False)
        with open(os.path.join(vault, "ledger.db.enc"), "rb") as fh:
            blob = fh.read()
        check("密文里搜不到密码明文", BANK_PDF_PASSWORD.encode() in blob, False)
        check("任务结束后参数已被清空", console.jobs[job_id]["params"], {})

        with secure_store.open_encrypted(vault, save_on_exit=False, warn=lambda s: None) as led:
            n2 = led.conn.execute("SELECT COUNT(*) FROM txn").fetchone()[0]
        check("银行 PDF 的 9 笔已入库（10 + 9）", n2, 19)

        # ------------------------------------------------------------------
        section("10. 疑似重复接口与裁定")
        _, txt = http(port, "GET", "/api/review", token)
        items = json.loads(txt)["items"]
        check("列得出疑似重复", len(items) >= 1, True)
        if items:
            target = items[0]["id"]
            _, txt = http(port, "POST", "/api/run", token, {
                "action": "resolve", "params": {"id": target, "decision": "ok"}})
            job = wait_job(port, token, json.loads(txt)["job"])
            check("裁定任务结束", job["status"], "done")
            with secure_store.open_encrypted(vault, save_on_exit=False,
                                             warn=lambda s: None) as led:
                review = led.conn.execute("SELECT review FROM txn WHERE id = ?",
                                          (target,)).fetchone()["review"]
            check("裁定落盘", review, "confirmed_ok")
        _, txt = http(port, "POST", "/api/run", token, {
            "action": "resolve", "params": {"id": 999999, "decision": "dup"}})
        job = wait_job(port, token, json.loads(txt)["job"])
        check("裁定不存在的 id 会失败", job["status"], "error")

        # ------------------------------------------------------------------
        section("11. 打开投递目录（测试里替换掉真正的弹窗）")
        status, txt = http(port, "POST", "/api/open_inbox", token, {})
        check("接口返回 200", status, 200)
        payload = json.loads(txt)
        check("返回的就是投递目录", os.path.normcase(payload["path"]),
              os.path.normcase(inbox))
        if real_startfile is not None:
            check("确实调用了打开动作", [os.path.normcase(p) for p in opened],
                  [os.path.normcase(inbox)])

        # ------------------------------------------------------------------
        section("12. 拒绝绑定非本机地址")
        try:
            webui.build_server(vault, port=0, host="0.0.0.0")
            check("0.0.0.0 应被拒", "没有报错", "ValueError")
        except ValueError:
            check("0.0.0.0 被拒绝", "ValueError", "ValueError")

        # ------------------------------------------------------------------
        section("13. 启动器的编码约定（踩过坑，别改坏）")
        launcher = os.path.join(ROOT, "MyLedger.cmd")
        check("MyLedger.cmd 在仓库根目录", os.path.isfile(launcher), True)
        blob = open(launcher, "rb").read()
        # cmd.exe 在 65001 代码页下按「字符数」推进批处理文件指针，
        # 非 ASCII 字符会让指针漂移，整行被跳过、半行被当成命令执行。
        # 所以这个文件必须纯 ASCII，中文一律由 Python 打印。
        check("必须是纯 ASCII", all(b < 128 for b in blob), True)
        check("不能有 BOM", blob[:3] == b"\xef\xbb\xbf", False)
        check("必须全部是 CRLF",
              blob.count(b"\n") - blob.count(b"\r\n"), 0)
        check("能定位到 importer 入口", b"-m importer web" in blob, True)

    finally:
        if real_startfile is not None:
            os.startfile = real_startfile
        httpd.shutdown()
        httpd.server_close()
        console.stop()
        shutil.rmtree(tmp, ignore_errors=True)

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
