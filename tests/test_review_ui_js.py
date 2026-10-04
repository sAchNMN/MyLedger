"""复核界面前端逻辑的行为测试（ML-008 的前端一半）。

为什么必须有这个文件：

    后端已经把契约修好了（404/409 有明确状态码），但真正让用户丢数据的
    是前端这一行：

        try { await fetch(...); } catch (e) {}
        idx++;            // ← 无论成没成都前进
        render();

    fetch 只在网络层失败时抛异常，HTTP 404/409/500 都会正常返回，
    所以那个 catch 基本不会进；进了也没用，因为 idx++ 在 catch 外面。
    结果是：请求失败了，页面照样跳到下一条，用户看到「下一条」就以为
    「这一笔记下了」。人工复核的结果就这么静默消失。

    这是个纯前端问题，Python 侧测不到。这里用一个最小的 DOM/fetch 桩，
    把页面里的 <script> 拿到 Node 里真跑一遍，逐场景断言：

        200            → 前进一条
        403/400/500    → 停在原地，提示重试
        网络断开        → 停在原地，提示重试
        404/409        → 停在原地，并且给「跳过这条」的出口
        连续双击        → 只发一次请求，只前进一条

    Node 找不到时会明确打印 SKIP，不会假装通过。

用法：<venv>\\Scripts\\python.exe tests/test_review_ui_js.py
返回码 0 表示全部通过。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from importer.review_ui import PAGE  # noqa: E402

FAILURES: list[str] = []

HARNESS = r"""
'use strict';
const fs = require('fs');

const scriptPath = process.argv[2];
const planPath = process.argv[3];
const scenarios = JSON.parse(fs.readFileSync(planPath, 'utf8'));

// 页面脚本是一个立即执行的 async IIFE。它内部没接住的 rejection 默认会让
// Node 直接退出，于是后面的场景一个都跑不到 —— 而「没接住」恰恰是要测的东西。
// 收起来继续跑，让每个场景都被完整走一遍。
const unhandled = [];
process.on('unhandledRejection', (err) => {
  unhandled.push(String((err && err.message) || err));
});

// ---------------- DOM 桩 ----------------
function makeEl() {
  return { textContent: '', innerHTML: '', hidden: false, style: {}, className: '' };
}
let nodes = new Map();
global.document = {
  getElementById: function (id) {
    if (!nodes.has(id)) nodes.set(id, makeEl());
    return nodes.get(id);
  },
  addEventListener: function () {},
};
global.window = global;

// ---------------- fetch 桩 ----------------
let plan = [];
let log = [];
global.fetch = function (url, opts) {
  log.push({ url: String(url), method: (opts && opts.method) || 'GET' });
  const step = plan.shift();
  if (!step) return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
  if (step.networkError) return Promise.reject(new TypeError('Failed to fetch'));
  return Promise.resolve({
    ok: step.status >= 200 && step.status < 300,
    status: step.status,
    json: () => Promise.resolve(step.body === undefined ? {} : step.body),
  });
};

// 探针必须和被测脚本在同一个作用域里，否则读不到内部的 idx / items / deciding
const PROBE = ';globalThis.__app = {' +
  'get idx() { return idx; },' +
  'get items() { return items; },' +
  'get deciding() { return deciding; },' +
  'decide: decide,' +
  // typeof 对未声明的标识符也安全：这样同一份桩可以拿去跑旧版本的页面，
  // 用来反向验证「这份测试确实抓得住那个缺陷」。
  'skipCurrent: (typeof skipCurrent === "function") ? skipCurrent : function () {},' +
  'errHTML: function () { return document.getElementById("err").innerHTML; },' +
  'errHidden: function () { return document.getElementById("err").hidden; },' +
  'mainHTML: function () { return document.getElementById("main").innerHTML; }' +
  '};';

const source = fs.readFileSync(scriptPath, 'utf8');
const sleep = () => new Promise((resolve) => setImmediate(resolve));

(async function main() {
  const results = [];
  for (const sc of scenarios) {
    nodes = new Map();
    log = [];
    plan = sc.responses.slice();

    // 每次场景都重新构造一次脚本作用域：idx / items 必须是干净的
    new Function(source + PROBE)();
    const app = globalThis.__app;
    await sleep(); await sleep(); await sleep();

    const before = app.idx;
    if (sc.clicks === 2) {
      const a = app.decide(sc.decision);
      const b = app.decide(sc.decision);        // 第二次点击应被 deciding 挡住
      await Promise.all([a, b]);
      if (sc.thenSkip) { app.skipCurrent(); }
    } else if (sc.thenSkip) {
      await app.decide(sc.decision);
      app.skipCurrent();
    } else {
      await app.decide(sc.decision);
    }

    results.push({
      name: sc.name,
      itemCount: app.items.length,
      idxBefore: before,
      idxAfter: app.idx,
      advanced: app.idx - before,
      deciding: app.deciding,
      errHTML: app.errHTML(),
      errHidden: app.errHidden(),
      mainHTML: app.mainHTML(),
      postCalls: log.filter((c) => c.method === 'POST').length,
    });
  }
  process.stdout.write(JSON.stringify(results));
})();
"""

ITEMS = [
    {"id": 2, "reason": "内容指纹一致", "self": {"source_label": "支付宝", "amount": "28.50",
     "ts": "2026-09-03 12:31:05", "counterparty": "合成商户甲", "item": "午餐",
     "method": "余额宝", "txn_id": "SYN-2"}, "other": None},
    {"id": 5, "reason": "内容指纹一致", "self": {"source_label": "支付宝", "amount": "88.80",
     "ts": "2026-09-03 14:00:00", "counterparty": "合成商户丙", "item": "日用品",
     "method": "余额", "txn_id": "SYN-5"}, "other": None},
]

STATE_OK = {"status": 200, "body": {"items": ITEMS, "total": len(ITEMS)}}


def check(name: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: 期望 {want!r}，实际 {got!r}")
    print(f"  [{'✓' if ok else '✗'}] {name:<48} -> {got!r}")


def check_in(name: str, needle: str, haystack: str) -> None:
    ok = needle in (haystack or "")
    if not ok:
        FAILURES.append(f"{name}: {haystack!r} 里找不到 {needle!r}")
    print(f"  [{'✓' if ok else '✗'}] {name:<48} -> {ok}")


def scenarios() -> list[dict]:
    return [
        {"name": "200 成功", "responses": [STATE_OK, {"status": 200,
          "body": {"ok": True, "id": 2, "updated": True}}], "decision": "dup"},
        {"name": "400 请求不合法", "responses": [STATE_OK, {"status": 400,
          "body": {}}], "decision": "dup"},
        {"name": "403 令牌不对", "responses": [STATE_OK, {"status": 403,
          "body": {}}], "decision": "dup"},
        {"name": "500 服务端出错", "responses": [STATE_OK, {"status": 500,
          "body": {}}], "decision": "dup"},
        {"name": "网络断开", "responses": [STATE_OK, {"networkError": True}],
         "decision": "dup"},
        {"name": "404 记录不存在", "responses": [STATE_OK, {"status": 404,
          "body": {"ok": False, "error": "not_found"}}], "decision": "dup"},
        {"name": "409 不是待复核", "responses": [STATE_OK, {"status": 409,
          "body": {"ok": False, "error": "not_reviewable"}}], "decision": "dup"},
        {"name": "409 已经裁定过", "responses": [STATE_OK, {"status": 409,
          "body": {"ok": False, "error": "already_decided"}}], "decision": "ok"},
        {"name": "连续双击", "responses": [STATE_OK, {"status": 200,
          "body": {"ok": True, "id": 2, "updated": True}}], "decision": "dup",
         "clicks": 2},
        {"name": "失败后手动跳过", "responses": [STATE_OK, {"status": 404,
          "body": {"ok": False, "error": "not_found"}}], "decision": "dup",
         "thenSkip": True},
        {"name": "读不到待确认列表", "responses": [{"networkError": True}],
         "decision": "dup"},
    ]


def main() -> int:
    node = shutil.which("node")
    if not node:
        print()
        print("=" * 78)
        print("SKIP：找不到 Node，前端逻辑这一半没有验证。")
        print("      后端契约由 tests/test_review_api.py 覆盖；")
        print("      前端「失败不前进」这条规则需要装 Node 才能自动验。")
        print("=" * 78)
        return 0

    tmp = tempfile.mkdtemp(prefix="myledger-js-")
    try:
        return run(node, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(node: str, tmp: str) -> int:
    match = re.search(r"<script>(.*?)</script>", PAGE, re.S)
    if not match:
        print("页面里找不到 <script> —— 测试前提被破坏了。")
        return 1

    script_path = os.path.join(tmp, "page.js")
    plan_path = os.path.join(tmp, "plan.json")
    harness_path = os.path.join(tmp, "harness.js")
    # PAGE 里的 __TOKEN__ / __TOTAL__ 是 build_server 运行时替换的占位符，
    # 这里替换成固定值即可 —— 脚本读它们只是为了发请求和显示进度。
    script = (match.group(1)
              .replace("__TOKEN__", "test-token")
              .replace("__TOTAL__", str(len(ITEMS))))
    with open(script_path, "w", encoding="utf-8") as fh:
        fh.write(script)
    with open(plan_path, "w", encoding="utf-8") as fh:
        json.dump(scenarios(), fh, ensure_ascii=False)
    with open(harness_path, "w", encoding="utf-8") as fh:
        fh.write(HARNESS)

    proc = subprocess.run([node, harness_path, script_path, plan_path],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)
    if proc.returncode != 0:
        print("Node 跑挂了：")
        print(proc.stderr)
        return 1

    results = {item["name"]: item for item in json.loads(proc.stdout)}

    print()
    print("=" * 78)
    print("成功：前进一条，不留错误提示")
    print("=" * 78)
    ok = results["200 成功"]
    check("前进一条", ok["advanced"], 1)
    check("没有错误提示", ok["errHidden"], True)
    check("deciding 复位（能接着点下一条）", ok["deciding"], False)
    print()
    print("=" * 78)
    print("失败：必须停在原地（这条是本次修复的核心）")
    print("=" * 78)
    for name in ("400 请求不合法", "403 令牌不对", "500 服务端出错", "网络断开",
                 "404 记录不存在", "409 不是待复核", "409 已经裁定过"):
        item = results[name]
        check(f"{name} → 不前进", item["advanced"], 0)
        check(f"{name} → 错误提示已显示", not item["errHidden"], True)
        check(f"{name} → deciding 复位", item["deciding"], False)

    print()
    print("=" * 78)
    print("提示文案：临时失败说重试，永久失败给「跳过」出口")
    print("=" * 78)
    check_in("500 提示重试", "重试", results["500 服务端出错"]["errHTML"])
    check_in("网络断开提示重试", "连不上本地服务",
             results["网络断开"]["errHTML"])
    check_in("404 说明原因", "已经不在待确认列表",
             results["404 记录不存在"]["errHTML"])
    check_in("404 给跳过按钮", "跳过这条", results["404 记录不存在"]["errHTML"])
    check_in("409 不是待复核给跳过按钮", "跳过这条",
             results["409 不是待复核"]["errHTML"])
    check_in("409 已裁定给跳过按钮", "跳过这条",
             results["409 已经裁定过"]["errHTML"])
    check("临时失败不给跳过按钮（避免误跳过）",
          "跳过这条" in results["500 服务端出错"]["errHTML"], False)

    print()
    print("=" * 78)
    print("双击与手动跳过")
    print("=" * 78)
    double = results["连续双击"]
    check("只发了一次请求", double["postCalls"], 1)
    check("只前进一条", double["advanced"], 1)
    skip = results["失败后手动跳过"]
    check("跳过前进一条", skip["advanced"], 1)
    check("跳过后错误提示收起", skip["errHidden"], True)

    print()
    print("=" * 78)
    print("读不到列表时不能装作正常")
    print("=" * 78)
    dead = results["读不到待确认列表"]
    check("没有内容可裁定", dead["itemCount"], 0)
    check_in("页面给出明确说明", "连不上本地服务", dead["mainHTML"])

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
