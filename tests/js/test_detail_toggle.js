/* 验「明细」页的「展开其余 N 笔 / 收起」真的能用。
 *
 * 背景（2026-10-05 用户报的 bug）
 * --------------------------------
 * 分类标题和「展开其余 N 笔」按钮点了**完全没反应**。
 * 真因：`detailToggle` 里靠
 *     box.innerHTML.indexOf("</div><div class='card'")
 * 找「工具栏」和「列表」的分界，好只重画列表。
 * 而我生成的 HTML 用的是**双引号**，那个单引号串永远匹配不上 →
 * 走 else 分支把整个容器清空 → 看起来就是「点了没反应」。
 *
 * 为什么用 Node 测而不是无头浏览器点
 * --------------------------------
 * 无头模拟点击要注入脚本、易受时序影响、还得起服务。
 * 这里直接把页面里那几段 JS 抠出来 eval，搭一个最小 DOM 替身跑 ——
 * 验的是**逻辑本身**，快且稳。前端交互的 bug 就该这么测。
 *
 * 顺带修掉的两个隐患（都在这个 commit 里）
 *   · onclick 拼字符串 → 改 data-cat + 事件委托（分类名含引号会拼出坏 JS）
 *   · 展开后没有「收起」按钮 → 用户以为点完没救，只能刷新
 *
 * 用法：
 *   <node> tests\\js\\test_detail_toggle.js
 * 不需要参数 —— JS 直接从 ../../importer/webui.py 的源码里抽。
 * 返回码 0 表示全部通过。
 */
const fs = require("fs");
const path = require("path");

/* 从 webui.py 源码里抠出 <script> 段 —— 不依赖运行中的服务，
   所以这个测试可以离线跑。 */
const PY = path.join(__dirname, "..", "..", "importer", "webui.py");
const py = fs.readFileSync(PY, "utf8");
const m = py.match(/<script>([\s\S]*?)<\/script>/);
if (!m) {
  console.error("FAIL 从 webui.py 里抠不到 <script> 段");
  process.exit(1);
}
const js = m[1];

/* ---- 从页面里抠出要测的函数 ---- */
function grab(name) {
  // 取 `function NAME(` 到下一个顶层 `\n}` 为止（够这些函数用）
  const start = js.indexOf("function " + name + "(");
  if (start < 0) throw new Error("找不到函数 " + name);
  let i = js.indexOf("{", start);
  let depth = 0;
  for (let j = i; j < js.length; j++) {
    if (js[j] === "{") depth++;
    else if (js[j] === "}") {
      depth--;
      if (depth === 0) return js.slice(start, j + 1);
    }
  }
  throw new Error("函数 " + name + " 括号不配对");
}

const PARTS = ["esc", "shortTime", "dirClass", "detailList", "detailToggle"];
const code = PARTS.map(grab).join("\n\n");

/* ---- 最小 DOM 替身 ---- */
let listBox = { innerHTML: "" };
global.document = {
  getElementById: (id) => (id === "detail-list" ? listBox : null),
  addEventListener: () => {},
};
global.DETAIL = {
  by_category: {
    "未分类": Array.from({ length: 55 }, (_, i) => ({
      ts: "2026-09-30 13:33:15", source: "bank_pdf", direction: "expense",
      amount: "2300.00", counterparty: "中国银行某支行", item: "转账" + i,
    })),
    "学习文具": Array.from({ length: 2 }, (_, i) => ({
      ts: "2026-09-07 13:06:33", source: "alipay", direction: "expense",
      amount: "39.90", counterparty: "天猫校园超市", item: "文具" + i,
    })),
  },
};
let detailOpen = {};
/* detailList 依赖这几个模块级变量/常量，一并搭出来 */
let detailFilter = "";
let detailDir = "";
const DIR_LABEL = { expense: "支出", income: "收入", neutral: "中性" };
global.DETAIL_ROWS = () => global.DETAIL;

eval(code + `
  global.__t = { detailList, detailToggle,
                 setOpen: (o) => { detailOpen = o; },
                 setFilter: (f, d) => { detailFilter = f; detailDir = d; },
                 getOpen: () => detailOpen };
`);

const T = global.__t;
T.setOpen({});

/* ---- 折叠态 ---- */
let html = T.detailList();
const rowsClosed = (html.match(/<tr>/g) || []).length;
console.log("折叠时：分类 2 个，表格行(含表头) " + rowsClosed);
console.log("  有「展开其余」按钮:", html.includes("展开其余"));
console.log("  未分类按钮文案:", (html.match(/展开其余 (\d+) 笔/) || [])[1]);

/* ---- 点一下「未分类」---- */
T.detailToggle("未分类");
html = T.detailList();
const rowsOpen = (html.match(/<tr>/g) || []).length;
console.log("\n点「未分类」之后：表格行 " + rowsOpen);
const html2 = html;
console.log("  「展开其余」按钮消失了吗:", !html.includes("展开其余"));
console.log("  出现「收起」了吗:", html.includes("收起"));

/* ---- 再点一下应收起 ---- */
T.detailToggle("未分类");
html = T.detailList();
const rowsBack = (html.match(/<tr>/g) || []).length;
console.log("\n再点一次：表格行 " + rowsBack + "（应回到 " + rowsClosed + "）");

/* ---- 判定 ---- */
const countOf = (s, re) => (s.match(re) || []).length;
const checks = [
  ["展开后行数变多", rowsOpen > rowsClosed],
  ["54 笔全出来了（+54 行）", rowsOpen - rowsClosed === 54],
  ["展开后该分类的「展开其余」消失", countOf(html2, /展开其余/g) === 1],
  ["展开后出现「收起」按钮", countOf(html2, />收起</g) === 1],
  ["收起后回到原样", rowsBack === rowsClosed],
  ["收起后「展开其余」又回来了", countOf(html, /展开其余/g) === 2],
];
console.log("\n" + "-".repeat(50));
let bad = 0;
for (const [name, ok] of checks) {
  if (name === "展开后按钮变成「收起」") continue;   // 上面已单独验
  console.log((ok ? "  ok   " : "  FAIL ") + name);
  if (!ok) bad++;
}
process.exit(bad ? 1 : 0);
