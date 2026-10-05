"""本地网页控制台：不用记命令，点按钮就能用。

为什么要做这个：
    命令行把「记住 8 条命令 + 每次敲对参数」的成本转嫁给了用户。
    记账是每个月都要做的事，反复查命令是纯粹的浪费。
    网页控制台把同样的能力摆成按钮和表格，顺手还能把日志摆在眼前。

不变的几条底线（和 review_ui 一样，不是复制粘贴的巧合）：

1. **只绑 127.0.0.1。** 绑别的地址等于把账本摊给整个局域网，这里不提供这个选项。
2. **URI 里带一次性随机令牌。** 防的是本机其它进程「猜端口随便打一下」。
3. **页面完全自包含**，不引任何外部资源（没有 CDN、没有字体、没有图标库），不联网。
4. **zip / PDF 密码只在内存里活一次。** 页面填的密码随本次任务用完即丢，
   不落盘、不进日志、任务结束就从内存里清掉。控制台不提供「记住密码」。
5. **不落任何文件。** 日志只活在服务进程的内存里，关掉窗口就没了——
   想留档请用命令行重定向。

架构：只有一个工作线程，所有「会动账本」的活都排进同一个队列串行执行；
只读查询共用同一把锁，任务在跑的时候读接口直接返回上一次的快照（标 stale），
不会把界面卡住。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import queue
import secrets
import sqlite3
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from . import fixups, monthly, review_ui, secure_store, sourcehint
from .categorize import CATEGORIES, clear_manual, manual_map, set_manual
from .model import SOURCE_LABELS
from .pipeline import import_file
from .power import describe, is_on_ac
from .render import mask_merchant, money
from .sources import PARSERS, detect_source

DEFAULT_PORT = 8765
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

SOURCE_LABELS_ALL = {**SOURCE_LABELS, "bank_pdf": "银行卡PDF", "unknown": "识别不出"}

# 导入页「渠道」下拉框的选项。顺序按用户遇到问题的频率排：
# 支付宝、微信最常见，银行 PDF 次之，截图 OCR 最后（它要先生成 TSV）。
SOURCE_CHOICES = ("alipay", "wechat", "bank_pdf", "bank_ocr")


# ==========================================================================
# 日志收集
# ==========================================================================

class LogSink(io.TextIOBase):
    """把 print 出去的东西按行喂给回调。

    用途：monthly.run() 是照命令行输出的写法写的（几十个 print），
    为了在网页里也看到同一份日志，最省事也最不容易出错的办法就是
    **重定向 stdout**，而不是把那些 print 全部改成 emit 参数。
    只有一个工作线程，所以重定向全局 stdout 是安全的
    （HTTP 处理线程不打印任何东西）。
    """

    def __init__(self, append: Callable[[str], None]):
        self._append = append
        self._buf = ""

    def write(self, text) -> int:
        self._buf += str(text)
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._append(line)
        return len(str(text))

    def flush(self) -> None:
        if self._buf:
            self._append(self._buf)
            self._buf = ""

    def isatty(self) -> bool:      # noqa: A003 - 保持和文件对象一致
        return False


# ==========================================================================
# 各项操作
#
# 每个操作都是 (vault, params, log) -> dict。params 来自页面，
# **只在这个函数执行期间存在**，执行完调用方会把它从内存里清掉。
# ==========================================================================

def act_monthly(vault: str, params: dict, log) -> dict:
    """月度批处理：扫投递目录 → 逐个导入 → 出月报。

    sources 是用户在导入页逐文件选的渠道 {路径: 渠道}。
    指定了的文件走指定渠道，没指定的照常自动识别/查记忆。
    """
    passwords = {k: v for k, v in (params.get("passwords") or {}).items() if v}
    sources = {str(k): str(v) for k, v in (params.get("sources") or {}).items() if v}
    stream = LogSink(log)
    with contextlib.redirect_stdout(stream):
        rc = monthly.run(
            vault,
            use_ai=bool(params.get("ai")),
            keep=bool(params.get("keep")),
            dry_run=bool(params.get("dry_run")),
            # 网页控制台自己就有确认页面，绝不能让 monthly 再拉起一个服务
            # （会嵌套、会阻塞、还可能在 cmd 窗口里弹密码提示）。
            auto_review=False,
            interactive=False,
            passwords=passwords,
            source_overrides=sources,
        )
    stream.flush()
    return {"exit": rc, "passthrough": True}


def act_setsrc(vault: str, params: dict, log) -> dict:
    """记住「某个形状的文件是哪个渠道」，以后同类文件自动认。"""
    key = str(params.get("key") or "").strip()
    source = str(params.get("source") or "").strip()
    if params.get("clear"):
        with secure_store.open_encrypted(vault, save_on_exit=True) as ledger:
            removed = sourcehint.clear_hint(ledger, key)
        return {"removed": removed, "key": key}

    if source not in sourcehint.KNOWN_SOURCES:
        raise ValueError(
            f"不认识的渠道「{source}」。可用：{'、'.join(sourcehint.KNOWN_SOURCES)}"
        )
    with secure_store.open_encrypted(vault, save_on_exit=True) as ledger:
        saved = sourcehint.set_hint(ledger, key, source)
    log(f"已记住：{saved} → {SOURCE_LABELS_ALL.get(source, source)}")
    log("以后形状相同的文件会自动按这个渠道解析，不用再选。")
    return {"key": saved, "source": source}


def act_import_one(vault: str, params: dict, log) -> dict:
    """只导入指定的一个文件（月度批处理失败后单独重试用）。"""
    path = str(params.get("path") or "")
    if not os.path.isfile(path):
        raise ValueError(f"找不到文件：{path}")
    password = params.get("password") or None
    dry_run = bool(params.get("dry_run"))
    chosen = str(params.get("source") or "auto")
    with secure_store.open_encrypted(vault, save_on_exit=not dry_run) as ledger:
        if params.get("remember"):
            # 先落记忆再导：这样即使这次解析失败，下次也认得了。
            sourcehint.set_hint(ledger, sourcehint.shape_key(path), chosen)
        source = chosen
        if source == "auto":
            source = detect_source(path, sourcehint.hint_map(ledger))
        if source not in PARSERS:
            raise ValueError("这个文件识别不出渠道，请手动选一个数据源")
        outcome = import_file(
            path, ledger, source=source, password=password,
            use_ai=bool(params.get("ai")), archive=True, dry_run=dry_run,
            emit=log,
        )
    return {
        "exit": 0 if outcome.ok else 2,
        "ok": outcome.ok,
        "error": outcome.error,
        "inserted": outcome.inserted,
        "identical": outcome.identical,
        "suspect": outcome.suspect,
        "source": SOURCE_LABELS_ALL.get(outcome.source, outcome.source),
    }


def act_recompute(vault: str, params: dict, log) -> dict:
    """按当前规则重算全部历史数据。默认只试算。"""
    apply_ = bool(params.get("apply"))
    if apply_:
        enc = secure_store.encrypted_path(vault)
        if os.path.exists(enc):
            fixups.backup(enc, emit=log)
    with secure_store.open_encrypted(vault, save_on_exit=apply_) as ledger:
        result = fixups.recompute(ledger, apply=apply_, emit=log)
    result["exit"] = 0
    return result


def act_setcat(vault: str, params: dict, log) -> dict:
    """手工指定某个商户的分类。可选同时回填历史。"""
    merchant = str(params.get("merchant") or "").strip()
    if not merchant:
        raise ValueError("商户名不能为空")
    apply_ = bool(params.get("apply"))
    clearing = bool(params.get("clear"))

    if apply_:
        enc = secure_store.encrypted_path(vault)
        if os.path.exists(enc):
            fixups.backup(enc, emit=log)

    with secure_store.open_encrypted(vault, save_on_exit=True) as ledger:
        if clearing:
            removed = clear_manual(ledger, merchant)
            log("已撤销该商户的人工指定，退回规则判定。" if removed
                else "这个商户本来就没有人工指定。")
            category = None
        else:
            category = str(params.get("category") or "")
            if category not in CATEGORIES:
                raise ValueError("请选择一个分类")
            key = set_manual(ledger, merchant, category)
            log(f"已记下缓存：{key} → {category}（以后导入这个商户自动用它）")
        result = fixups.apply_manual(ledger, merchant, category, apply=apply_, emit=log)
    result["exit"] = 0
    if not apply_:
        log("上面是试算：缓存已写，但**已入库的历史交易没动**。确认后点「执行」。")
    return result


def act_resolve(vault: str, params: dict, log) -> dict:
    """疑似重复的人工裁定。只改标记，不删记录。"""
    txn_id = int(params.get("id"))
    decision = {"dup": "confirmed_dup", "ok": "confirmed_ok"}.get(params.get("decision"))
    if not decision:
        raise ValueError("decision 只能是 dup 或 ok")
    with secure_store.open_encrypted(vault, save_on_exit=True) as ledger:
        if not ledger.resolve(txn_id, decision):
            raise ValueError(f"找不到 #{txn_id}")
        remaining = ledger.conn.execute(
            "SELECT COUNT(*) AS n FROM txn WHERE review LIKE 'suspect%'"
        ).fetchone()["n"]
    log(f"#{txn_id} 已标记为 {decision}，还剩 {remaining} 笔待确认。")
    return {"remaining": remaining, "exit": 0}


ACTIONS = {
    "monthly": act_monthly,
    "import_one": act_import_one,
    "setsrc": act_setsrc,
    "recompute": act_recompute,
    "setcat": act_setcat,
    "resolve": act_resolve,
}


# ==========================================================================
# 只读快照
# ==========================================================================

def inbox_files(vault: str, hints: Optional[dict] = None) -> list:
    """投递目录里的待处理文件，附「要不要密码」「渠道能不能定」这些关键信息。

    hints 是渠道记忆（sourcehint.hint_map 的返回值）。
    读不出来账本时传 None，只走启发式 —— 页面上照常显示，只是
    「记住的渠道」那列会显示成没记住。
    """
    out = []
    for path in monthly.scan_inbox(vault):
        source = detect_source(path, hints)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        try:
            needs = monthly.file_needs_password(path)
        except Exception:
            needs = False
        out.append({
            "path": path,
            "name": os.path.basename(path),
            "source": source,
            "source_label": SOURCE_LABELS_ALL.get(source, source),
            "needs_password": needs,
            "size_kb": round(size / 1024, 1),
            "shape": sourcehint.shape_key(path),
            # 记住的是哪个形状：页面上要显示给用户看，
            # 否则「下次自动认」是空话，用户不知道自己在承诺什么。
            "remembered": bool(hints and sourcehint.shape_key(path) in hints),
        })
    return out


def snapshot(vault: str, warn: Callable[[str], None] = print) -> dict:
    """账本现状。只读，不会创建任何东西。"""
    info = secure_store.status(vault)
    out = {
        "vault": vault,
        "inbox": os.path.join(vault, monthly.INBOX_DIRNAME),
        "now": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "power": describe(),
        "ac": is_on_ac(),
        "categories": list(CATEGORIES),
        "source_choices": [
            {"value": v, "label": SOURCE_LABELS_ALL.get(v, v)} for v in SOURCE_CHOICES
        ],
        "encrypted_exists": info["encrypted_exists"],
        "encrypted_size_kb": round(info["encrypted_size"] / 1024, 1),
        "encrypted_path": info["encrypted_path"],
        "plaintext_exists": info["plaintext_exists"],
        "plaintext_path": info["plaintext_path"],
        "inbox_files": inbox_files(vault),
        "overview": None,
        "months": [],
        "unmatched": None,
        "manual": [],
        "hints": [],
        "review_left": 0,
        "error": "",
    }

    if not info["encrypted_exists"]:
        return out

    try:
        with secure_store.open_encrypted(vault, save_on_exit=False, warn=warn) as ledger:
            # 渠道记忆要先读出来，才能算准每个文件「是自动认的还是记住的」。
            # inbox_files 在上面的 dict 里已经算过一次（那时还没有账本），
            # 这里拿到记忆后重算一次，让页面显示的和实际导入的一致。
            hints = sourcehint.hint_map(ledger)
            out["inbox_files"] = inbox_files(vault, hints)
            out["hints"] = [
                {"key": k, "source": v, "label": SOURCE_LABELS_ALL.get(v, v)}
                for k, v in sorted(hints.items())
            ]
            txn = ledger.overall()
            row = txn["txn"]
            out["overview"] = {
                "n": row["n"] or 0,
                "first_ts": row["first_ts"] or "",
                "last_ts": row["last_ts"] or "",
                "expense": money(row["expense"]),
                "batches": txn["batches"],
                "suspect": row["suspect"] or 0,
                "confirmed_dup": row["confirmed_dup"] or 0,
            }
            out["months"] = [
                r["m"] for r in ledger.conn.execute(
                    "SELECT DISTINCT substr(ts,1,7) AS m FROM txn ORDER BY m DESC")
            ]
            totals = ledger.unmatched_totals()
            share = (totals["expense"] / totals["expense_all"] * 100) if totals["expense_all"] else 0.0
            out["unmatched"] = {
                "n": totals["n"],
                "merchants": totals["merchants"],
                "expense": money(totals["expense"]),
                "income": money(totals["income"]),
                "expense_all": money(totals["expense_all"]),
                "share": round(share, 1),
            }
            out["manual"] = [
                {"merchant": k, "category": v} for k, v in sorted(manual_map(ledger).items())
            ]
            out["review_left"] = ledger.conn.execute(
                "SELECT COUNT(*) AS n FROM txn WHERE review LIKE 'suspect%'"
            ).fetchone()["n"]
    except Exception as exc:
        # 解不开密文（换了 Windows 账户 / 重装系统）是必须说清楚的事，
        # 不能只在日志里冒一行，界面上要有红条。
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def report_data(vault: str, month: str) -> dict:
    with secure_store.open_encrypted(vault, save_on_exit=False, warn=lambda s: None) as ledger:
        summary = ledger.month_summary(month)
    expense, income, neutral = [], 0.0, 0.0
    for row in summary["rows"]:
        total = float(row["total"] or 0)
        if row["direction"] == "expense":
            expense.append([row["category"], money(total), row["n"]])
        elif row["direction"] == "income":
            income += total
        else:
            neutral += total
    expense.sort(key=lambda item: float(item[1].replace(",", "")), reverse=True)
    return {
        "month": month,
        "expense": expense,
        "expense_total": money(sum(float(i[1].replace(",", "")) for i in expense)),
        "expense_n": sum(i[2] for i in expense),
        "income": money(income),
        "neutral": money(neutral),
    }


def detail_data(vault: str, month: str = "") -> dict:
    """交易明细（网页「账单明细」页的数据源）。

    为什么要它
    ----------
    报表页 (`report_data`) 只给「分类 → 金额 → 笔数」的**汇总**。
    汇总数字对不上时，唯一能查的办法是看**具体是哪几笔** ——
    而以前整个网页没有这个能力，只能开命令行。

    刻意不做「注册登录」
    --------------------
    用户提过要不要加账号系统。**单人本地软件加登录没有意义**：
    登录进去看到的还是他一个人的数据，而账本密文是 DPAPI 绑 Windows 账号的
    （换个 Windows 账号根本解不开，见 `secure_store`）。
    真正需要权限的是「多个人共用一台机器」，那场景本地账本不适用。
    如果以后真要做 Web 版多用户，那是另一个量级的改动，得先想清楚
    「密钥存在哪、谁能解」——不是加个登录框就行的。

    汇总口径与 `report_data` 完全一致（都排除 `confirmed_dup`），
    否则点开看到的笔数会和上面的汇总对不上，那比看不到更让人困惑。
    """
    with secure_store.open_encrypted(vault, save_on_exit=False,
                                     warn=lambda s: None) as ledger:
        if month:
            months = [month]
        else:
            # 不给月份时列出**全部有数据的月份**，让页面能切换
            rows = ledger.conn.execute(
                "SELECT DISTINCT substr(ts,1,7) AS m FROM txn "
                "WHERE review != 'confirmed_dup' AND ts != '' "
                "ORDER BY m DESC").fetchall()
            months = [r["m"] for r in rows]

        if not months:
            return {"month": "", "months": [], "by_category": {},
                    "truncated": False, "n": 0, "expense_total": "0.00"}

        cur = month or months[0]
        detail = ledger.month_detail(cur)
        items = [t for group in detail["by_category"].values() for t in group]
        expense_total = sum(float(t["amount"]) for t in items
                            if t["direction"] == "expense")

    return {
        "month": cur,
        "months": months,
        "by_category": detail["by_category"],
        "truncated": detail["truncated"],
        "n": len(items),
        "expense_total": f"{expense_total:.2f}",
    }


def review_data(vault: str) -> dict:
    """待确认的疑似重复，整理成界面需要的两条并排结构。"""
    with secure_store.open_encrypted(vault, save_on_exit=False, warn=lambda s: None) as ledger:
        return {"items": review_ui.collect(ledger)}


def classify_data(vault: str, month: str = "", mask: bool = False) -> dict:
    """分类与状态判定的诊断数据（对应 CLI 的 `classify`）。

    为什么要有这个页面：判定叠了四层（人工 / 平台分类 / 规则 / 原值）
    加一个状态判定之后，光看报表无法回答「这一笔为什么是那个分类」。
    这个接口把每一层的实际命中情况摊开，并把「还没被映射 / 认不出来」
    的值单列出来 —— 那是迭代映射表和状态词表的唯一入口。

    只读，不改任何数据。
    """
    from . import platformmap
    from .model import STATUS_KILLS_EXPENSE, status_verdict

    with secure_store.open_encrypted(vault, save_on_exit=False,
                                     warn=lambda s: None) as ledger:
        scope = " WHERE substr(ts,1,7) = ?" if month else ""
        params = [month] if month else []
        joiner = " AND " if scope else " WHERE "

        plat_rows = ledger.conn.execute(
            "SELECT source, platform_category, direction, COUNT(*) AS n,"
            " SUM(CAST(amount AS REAL)) AS total FROM txn" + scope
            + joiner + "platform_category != ''"
            " GROUP BY source, platform_category, direction"
            " ORDER BY total DESC", params).fetchall()

        total_n = ledger.conn.execute(
            "SELECT COUNT(*) AS n FROM txn" + scope, params).fetchone()["n"]

        by_status: dict = {}
        for r in ledger.conn.execute(
                "SELECT status, direction, COUNT(*) AS n,"
                " SUM(CAST(amount AS REAL)) AS total FROM txn" + scope
                + joiner + "status != '' GROUP BY status, direction", params):
            verdict = status_verdict(r["status"] or "")
            slot = by_status.setdefault(r["status"], {
                "status": r["status"], "verdict": verdict,
                "expense": [0, 0.0], "income": [0, 0.0], "neutral": [0, 0.0],
            })
            slot[r["direction"]] = [r["n"], float(r["total"] or 0)]

    return {
        "month": month,
        "mask": mask,
        "total": total_n,
        "platform": [
            {
                "source": SOURCE_LABELS_ALL.get(r["source"], r["source"]),
                "value": mask_merchant(r["platform_category"]) if mask
                         else r["platform_category"],
                "hit": platformmap.platform_category(r["source"],
                                                    r["platform_category"]) or "",
                "n": r["n"],
                "total": money(r["total"]),
            }
            for r in plat_rows
        ],
        "status": [
            {
                "status": mask_merchant(s["status"]) if mask else s["status"],
                "verdict": s["verdict"],
                "killed": s["verdict"] in STATUS_KILLS_EXPENSE,
                "expense_n": s["expense"][0], "expense": money(s["expense"][1]),
                "income_n": s["income"][0], "income": money(s["income"][1]),
                "neutral_n": s["neutral"][0], "neutral": money(s["neutral"][1]),
            }
            for s in sorted(by_status.values(),
                            key=lambda x: -(x["expense"][1] + x["income"][1]
                                            + x["neutral"][1]))
        ],
    }


def unmatched_data(vault: str, month: str = "", mask: bool = False) -> dict:
    with secure_store.open_encrypted(vault, save_on_exit=False, warn=lambda s: None) as ledger:
        totals = ledger.unmatched_totals(month or None)
        rows = ledger.unmatched_merchants(month or None, limit=300)
    share = (totals["expense"] / totals["expense_all"] * 100) if totals["expense_all"] else 0.0
    return {
        "month": month,
        "mask": mask,
        "totals": {
            "n": totals["n"],
            "merchants": totals["merchants"],
            "expense": money(totals["expense"]),
            "income": money(totals["income"]),
            "expense_all": money(totals["expense_all"]),
            "share": round(share, 1),
        },
        "rows": [
            {
                "merchant": mask_merchant(r["merchant"]) if mask else (r["merchant"] or "（空）"),
                "n": r["n"],
                "expense": money(r["expense"]),
                "income": money(r["income"]),
                "last_ts": r["last_ts"] or "",
            }
            for r in rows
        ],
    }


SOURCES_TEXT = """支付宝
  手机：支付宝 → 我的 → 账单 → 右上角「…」→ 开具交易流水证明 → 用于个人对账
  下载得到的是「带密码的 zip」，密码是申请时你自己设的。

微信
  手机：微信 → 我 → 服务 → 钱包 → 账单 → 右上角「常见问题」→ 下载账单
        → 用于个人对账 → 选时间范围 → 填邮箱
  账单会以「带密码的 zip」发到邮箱，密码在微信里单独给你。

银行卡
  优先用银行 App 的「导出账单到邮箱」功能，导出的加密 PDF 丢进投递目录即可。
  没有导出功能时才用截图，截图要求：一屏一张图、别滚动截长图、
  每笔至少能看到时间 / 对方 / 金额 / 收支方向。"""

USAGE_TEXT = """怎么用（只有三步）

1. 把导出的账单文件丢进「投递目录」（上面那个路径，点按钮直接打开）
2. 回到这个页面，进「导入」页，需要密码的文件会显示一个密码框，填上
3. 点「开始处理」，等日志跑完

渠道认错了怎么办
  导入页每个文件都有一行「渠道」下拉框，默认是自动猜的。
  猜错了就自己选，选完点「把手动选过的都记住」——
  记的是**形状**（比如「装着 PDF 的压缩包」）而不是文件名，
  所以下个月银行寄来的文件哪怕名字换了，也照样认得，不用再管。
  认不出渠道的行会标成浅红色，那就是唯一需要你动手的地方。
  已记住的每一条都能在「历史修正」页看到并撤销。

之后：
  * 「疑似重复」页会告诉你有没有需要人工判定的笔（点两个大按钮）
  * 「未分类」页列出规则认不出来的商户，选个分类点一下就归位了
  * 「报表」页看每个月钱花在哪

安全边界
  * 只监听本机 127.0.0.1，局域网访问不到
  * 页面不引用任何外部资源，全程不联网
  * 密码只在本机内存里用一次，不保存、不写日志、任务结束即清空
  * 账本全程在内存里解密，退出时加密落盘，明文不碰硬盘
  * 日志只活在这个服务进程的内存里，关掉窗口就没了"""


# ==========================================================================
# 页面
#
# 刻意不用 str.format：JS 和 CSS 里全是花括号，format 会把它们当占位符。
# 只用 str.replace 替换两个标记。
# ==========================================================================

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MyLedger 控制台</title>
<style>
  :root {
    --bg:#f7f7f5; --card:#fff; --line:#e3e2dd; --text:#1f1f1d; --muted:#6b6a66;
    --accent:#b3261e; --accent2:#146c43; --blue:#1a5fb4; --hl:#fff3cd; --warnbg:#fdf0ee;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font-family:system-ui,"Microsoft YaHei",sans-serif; font-size:14px; }
  header { background:var(--card); border-bottom:1px solid var(--line);
           padding:14px 24px; display:flex; justify-content:space-between;
           align-items:center; gap:16px; flex-wrap:wrap; }
  h1 { font-size:16px; font-weight:600; margin:0; }
  h1 small { font-weight:400; color:var(--muted); margin-left:10px; font-size:12px; }
  .meta { color:var(--muted); font-size:12px; text-align:right; line-height:1.7; }
  nav { background:var(--card); border-bottom:1px solid var(--line);
        padding:0 24px; display:flex; gap:2px; overflow-x:auto; }
  nav button { border:0; background:none; padding:12px 16px; cursor:pointer;
               font-size:14px; color:var(--muted); border-bottom:2px solid transparent;
               white-space:nowrap; font-family:inherit; }
  nav button:hover { color:var(--text); }
  nav button.on { color:var(--text); border-bottom-color:var(--accent); font-weight:500; }
  nav .badge { display:inline-block; background:var(--accent); color:#fff;
               border-radius:9px; padding:0 6px; font-size:11px; margin-left:6px; }
  main { max-width:1080px; margin:0 auto; padding:22px 24px 140px; }
  .tab { display:none; } .tab.on { display:block; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:18px 20px; margin-bottom:16px; }
  .card h2 { font-size:15px; margin:0 0 12px; font-weight:600; }
  .card h2 span { font-weight:400; color:var(--muted); font-size:12px; margin-left:8px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; }
  .kpi { background:var(--bg); border:1px solid var(--line); border-radius:10px; padding:14px; }
  .kpi b { display:block; font-size:22px; font-weight:600; letter-spacing:-0.4px; }
  .kpi i { font-style:normal; color:var(--muted); font-size:12px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:left; padding:7px 8px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:500; font-size:12px; }
  td.num { text-align:right; font-variant-numeric:tabular-nums; }
  /* 认不出渠道的行标红：这是唯一需要用户动手处理的情况，
     藏在正常行里等人去找等于没有提示。 */
  tr.row-bad td { background:#fdf1f1; }
  tr.row-bad .shape { color:var(--accent); }
  .shape { font-size:11px; margin-top:2px; }
  td select { padding:6px 9px; font-size:13px; min-width:150px; }
  tr.sum td { font-weight:600; border-top:2px solid var(--line); }
  .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  button.act { border:0; border-radius:9px; padding:10px 18px; font-size:14px; cursor:pointer;
               background:var(--blue); color:#fff; font-family:inherit; font-weight:500; }
  button.act:hover { filter:brightness(1.08); }
  button.act[disabled] { opacity:.45; cursor:not-allowed; }
  button.act.ghost { background:var(--bg); color:var(--text); border:1px solid var(--line); }
  button.act.danger { background:var(--accent); }
  button.act.good { background:var(--accent2); }
  button.act.sm { padding:6px 12px; font-size:12px; }
  input,select { border:1px solid var(--line); border-radius:8px; padding:9px 11px;
                 font-size:14px; font-family:inherit; background:#fff; color:var(--text); }
  input:focus,select:focus { outline:2px solid #cfe0f3; border-color:var(--blue); }
  label.chk { display:inline-flex; align-items:center; gap:6px; color:var(--muted); cursor:pointer; }
  .muted { color:var(--muted); }
  .note { background:#eef4fb; border:1px solid #cfe0f3; border-radius:10px;
          padding:11px 14px; font-size:13px; margin-bottom:12px; }
  .warn { background:var(--warnbg); border:1px solid #f2c9c2; border-radius:10px;
          padding:11px 14px; font-size:13px; margin-bottom:12px; }
  code { background:var(--bg); border:1px solid var(--line); border-radius:5px;
         padding:1px 5px; font-size:12px; }
  /* 明细页 */
  .dcat { display:flex; align-items:center; gap:9px; cursor:pointer;
          padding:4px 0 10px; user-select:none; }
  .dcat:hover b { color:var(--blue); }
  .caret { color:var(--muted); width:12px; font-size:11px; }
  .dcat .muted { margin-left:auto; font-size:12px; }
  .more { margin-top:10px; }
  td.bad { color:#b3402f; }
  td.good { color:#1c7a4a; }
  pre.log { background:#1f1f1d; color:#e8e8e4; border-radius:10px; padding:14px;
            font-size:12px; line-height:1.55; max-height:320px; overflow:auto;
            white-space:pre-wrap; word-break:break-all; margin:0; }
  #drawer { position:fixed; left:0; right:0; bottom:0; background:var(--card);
            border-top:1px solid var(--line); padding:12px 24px 16px; display:none;
            box-shadow:0 -6px 24px rgba(0,0,0,.07); }
  #drawer.on { display:block; }
  #drawer .row { justify-content:space-between; margin-bottom:8px; }
  .pair { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  @media (max-width:760px){ .pair { grid-template-columns:1fr; } }
  .txn { background:var(--bg); border:1px solid var(--line); border-radius:12px; padding:16px; }
  .txn .amount { font-size:26px; font-weight:600; }
  .txn dl { margin:12px 0 0; display:grid; grid-template-columns:60px 1fr; gap:6px 10px;
            font-size:13px; }
  .txn dt { color:var(--muted); } .txn dd { margin:0; word-break:break-all; }
  .same { background:var(--hl); border-bottom:2px solid #e0b400; border-radius:3px; padding:0 3px; }
  .actions2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-top:18px; }
  button.big { border:0; border-radius:12px; padding:20px; font-size:17px; font-weight:600;
               color:#fff; cursor:pointer; font-family:inherit; text-align:center; line-height:1.5; }
  button.big small { display:block; font-size:12px; font-weight:400; opacity:.85; margin-top:5px; }
  button.big.dup { background:var(--accent); }
  button.big.ok { background:var(--accent2); }
  .kbd { display:inline-block; border:1px solid rgba(255,255,255,.55); border-radius:4px;
         padding:0 5px; font-size:11px; margin-right:6px; }
  footer { text-align:center; color:var(--muted); font-size:12px; padding:22px 12px 8px; }
</style>
</head>
<body>
<header>
  <h1>MyLedger 控制台<small>本地 · 加密 · 不联网</small></h1>
  <div class="meta" id="meta">正在加载…</div>
</header>
<nav id="nav">
  <button data-tab="home" class="on">概览</button>
  <button data-tab="import">导入<span class="badge" id="b-inbox" style="display:none">0</span></button>
  <button data-tab="review">疑似重复<span class="badge" id="b-review" style="display:none">0</span></button>
  <button data-tab="report">报表</button>
  <button data-tab="detail">明细</button>
  <button data-tab="unmatched">未分类</button>
  <button data-tab="classify">分类诊断</button>
  <button data-tab="fix">历史修正</button>
  <button data-tab="help">帮助</button>
</nav>
<main>
  <div class="tab on" id="tab-home"><div class="card"><div class="muted">正在加载…</div></div></div>
  <div class="tab" id="tab-import"></div>
  <div class="tab" id="tab-review"></div>
  <div class="tab" id="tab-report"></div>
  <div class="tab" id="tab-detail"></div>
  <div class="tab" id="tab-unmatched"></div>
  <div class="tab" id="tab-classify"></div>
  <div class="tab" id="tab-fix"></div>
  <div class="tab" id="tab-help"></div>
</main>
<footer>账本全程内存解密、退出加密落盘 · 页面自包含、不引任何外部资源 · 关掉这个窗口服务仍在，重开浏览器即可</footer>

<div id="drawer">
  <div class="row">
    <div><b id="job-title">处理中…</b> <span class="muted" id="job-sub"></span></div>
    <div class="row">
      <button class="act ghost sm" id="btn-copy">复制日志</button>
      <button class="act ghost sm" id="btn-hide">收起</button>
    </div>
  </div>
  <pre class="log" id="log"></pre>
</div>

<script>
const TOKEN = "__TOKEN__";
const VAULT = __VAULT_JSON__;
const SOURCES_TEXT = __SOURCES_JSON__;
const USAGE_TEXT = __USAGE_JSON__;

let STATE = null, REVIEW = [], reviewIdx = 0, busy = false, curMonth = "", maskOn = false;

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
}
async function api(path, body) {
  const opt = { headers: { "X-MyLedger-Token": TOKEN, "Cache-Control": "no-cache" } };
  if (body !== undefined) {
    opt.method = "POST";
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  const txt = await r.text();
  if (!r.ok) throw new Error(txt || ("HTTP " + r.status));
  return JSON.parse(txt || "{}");
}
function show(tab) {
  document.querySelectorAll("#nav button").forEach(b =>
    b.classList.toggle("on", b.dataset.tab === tab));
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("on", t.id === "tab-" + tab));
  if (tab === "import") renderImport();
  if (tab === "review") renderReview();
  if (tab === "report") renderReport();
  if (tab === "detail") renderDetail();
  if (tab === "unmatched") renderUnmatched();
  if (tab === "classify") renderClassify();
  if (tab === "fix") renderFix();
}
document.getElementById("nav").addEventListener("click", e => {
  const b = e.target.closest("button[data-tab]");
  if (b) show(b.dataset.tab);
});

/* ---------------- 日志抽屉 ---------------- */
function logOpen(title, sub) {
  document.getElementById("drawer").classList.add("on");
  document.getElementById("job-title").textContent = title;
  document.getElementById("job-sub").textContent = sub || "";
  document.getElementById("log").textContent = "";
}
function logPush(line) {
  const el = document.getElementById("log");
  el.textContent += line + "\n";
  el.scrollTop = el.scrollHeight;
}
document.getElementById("btn-hide").onclick = () =>
  document.getElementById("drawer").classList.remove("on");
document.getElementById("btn-copy").onclick = async () => {
  try { await navigator.clipboard.writeText(document.getElementById("log").textContent); }
  catch (e) { /* 剪贴板不可用就算了，日志本来就能手动选中 */ }
};

async function runJob(action, params, title) {
  if (busy) { alert("有任务正在跑，等它结束。"); return null; }
  busy = true;
  logOpen(title, "任务进行中…");
  try {
    const { job } = await api("/api/run", { action: action, params: params || {} });
    let seen = 0;
    for (;;) {
      await new Promise(r => setTimeout(r, 400));
      const j = await api("/api/job?id=" + encodeURIComponent(job));
      (j.lines || []).slice(seen).forEach(logPush);
      seen = (j.lines || []).length;
      document.getElementById("job-sub").textContent =
        j.status === "running" ? "任务进行中…" : "已结束";
      if (j.status !== "running") {
        if (j.error) logPush("\n！出错了：" + j.error);
        await refreshState();
        if (j.result && j.result.passthrough) show("import");
        return j;
      }
    }
  } catch (e) {
    logPush("！启动失败：" + e.message);
    return null;
  } finally { busy = false; }
}

/* ---------------- 概览 ---------------- */
function renderHome() {
  const el = document.getElementById("tab-home");
  if (!STATE) { el.innerHTML = "<div class='card'><div class='muted'>正在加载…</div></div>"; return; }
  let h = "";
  if (STATE.error)
    h += "<div class='warn'><b>账本打不开</b><br>" + esc(STATE.error) +
         "<br><span class='muted'>密钥绑定当前 Windows 账户。换机器或重装系统后会这样；" +
         "数据没丢，vault\\raw\\ 里的原始账单还在，重新导入一次即可。</span></div>";

  if (!STATE.encrypted_exists) {
    h += "<div class='note'><b>还没有账本。</b>先到「导入」页把账单文件放进投递目录再处理，" +
         "账本会在第一次导入时自动创建。</div>";
  } else {
    const o = STATE.overview || {}, u = STATE.unmatched || {};
    h += "<div class='card'><h2>账本概览<span>" + esc(STATE.encrypted_path) + "</span></h2><div class='grid'>" +
      kpi(o.n + " 笔", "交易总笔数") +
      kpi(o.batches, "导入批次") +
      kpi(o.expense + " 元", "支出总额") +
      kpi(u.n + " 笔", "未分类（占支出 " + u.share + "%）") +
      kpi(o.suspect, "待确认重复") +
      kpi(o.confirmed_dup, "已确认重复（已剔除）") +
      "</div><p class='muted' style='margin:12px 0 0'>时间范围：" +
      esc(o.first_ts || "-") + " ~ " + esc(o.last_ts || "-") + "</p></div>";
  }

  const needsPwd = (STATE.inbox_files || []).filter(f => f.needs_password).length;
  if ((STATE.inbox_files || []).length) {
    h += "<div class='card'><h2>投递目录里还有 " + STATE.inbox_files.length +
         " 个文件没处理</h2><div class='row'><button class='act' onclick=\"show('import')\">去导入</button></div>" +
         (needsPwd ? "<p class='muted' style='margin:12px 0 0'>其中 " + needsPwd +
           " 个是加密文件，需要你填一次密码。</p>" : "") + "</div>";
  }
  h += "<div class='card'><h2>存储与运行环境</h2>" +
       "<div class='grid'>" +
       kpi(esc(STATE.power), "电源") +
       kpi(STATE.encrypted_size_kb + " KB", "加密账本大小") +
       kpi(esc(STATE.now), "当前时间") +
       "</div><div class='row' style='margin-top:12px'>" +
       "<button class='act ghost' onclick='refreshState()'>刷新</button>" +
       "</div>" +
       (STATE.plaintext_exists
         ? "<div class='warn' style='margin-top:12px'><b>发现明文库：</b>" +
           esc(STATE.plaintext_path) + "<br>留着等于账本没加密，请手动删掉它。</div>" : "") +
       "</div>";
  el.innerHTML = h;
}
function kpi(v, label) { return "<div class='kpi'><b>" + v + "</b><i>" + label + "</i></div>"; }

/* ---------------- 导入 ---------------- */
function renderImport() {
  const el = document.getElementById("tab-import");
  if (!STATE) return;
  const files = STATE.inbox_files || [];
  let h = "<div class='note'><b>步骤：</b>把账单文件放进投递目录 → 需要密码的填上 → 点「开始处理」。" +
          "<br>处理成功的文件会自动移到 <code>_done</code> 子目录，失败的原样留在原地。</div>";

  h += "<div class='card'><h2>投递目录</h2><div class='row'>" +
       "<code style='flex:1;overflow:auto'>" + esc(STATE.inbox) + "</code>" +
       "<button class='act ghost sm' onclick='openFolder()'>打开文件夹</button>" +
       "<button class='act ghost sm' onclick='copyText(\"" + esc(STATE.inbox).replace(/\\/g, "\\\\") + "\")'>复制路径</button>" +
       "</div><p class='muted' style='margin:10px 0 0'>" +
       "这个目录在仓库之外、账本旁边，不要放进任何云盘同步目录。</p></div>";

  h += "<div class='card'><h2>待处理文件<span>";
  h += files.length ? files.length + " 个" : "目前是空的";
  h += "</span></h2>";
  if (!files.length) {
    h += "<p class='muted' style='margin:0'>把支付宝 / 微信导出的 zip，或银行的加密 PDF 丢进上面的目录，" +
         "再点「刷新」。</p>";
  } else {
    h += "<table><thead><tr><th>文件</th><th>大小</th><th>渠道</th><th>密码</th></tr></thead><tbody>";
    files.forEach((f, i) => {
      const unidentified = f.source === "unknown";
      h += "<tr" + (unidentified ? " class='row-bad'" : "") + ">" +
           "<td>" + esc(f.name) +
           "<div class='muted shape'>形状 " + esc(f.shape) +
           (f.remembered ? " ·已记住渠道" : "") + "</div></td>" +
           "<td class='num'>" + f.size_kb + " KB</td>" +
           "<td>" + sourceSelect(f) + "</td><td>" +
           (f.needs_password
             ? "<input type='password' data-pwd='" + esc(f.source) + "' placeholder='需要密码（不保存）' style='width:190px'>"
             : "<span class='muted'>不需要</span>") +
           "</td></tr>";
    });
    h += "</tbody></table>" +
         "<p class='muted' style='margin:12px 0 0'>" +
         "「自动」= 按文件名和内容猜。认错了就手动选一个；" +
         "选完点「把手动选过的都记住」—— 记的是<b>形状</b>而不是文件名，" +
         "所以下个月银行寄来的文件哪怕名字换了也照样认得。" +
         "</p>" +
         "<div class='row' style='margin-top:10px'>" +
         "<button class='act ghost sm' onclick='rememberAll()'>把手动选过的都记住</button>" +
         "</div>";
  }
  h += "<div class='row' style='margin-top:14px'>" +
       "<button class='act ghost sm' onclick='refreshState()'>刷新列表</button>" +
       "<label class='chk'><input type='checkbox' id='opt-dry'> 只试算，不写库</label>" +
       "<label class='chk'><input type='checkbox' id='opt-keep'> 处理完不移动源文件</label>" +
       "<label class='chk'><input type='checkbox' id='opt-ai'> 用本地 AI 补分类（需插电 + Ollama）</label>" +
       "</div>" +
       "<div class='row' style='margin-top:12px'>" +
       "<button class='act' id='btn-monthly'>开始处理</button>" +
       "<span class='muted'>密码只在本机内存里用一次，不保存、不写日志。</span></div></div>";
  el.innerHTML = h;

  document.getElementById("btn-monthly").onclick = async () => {
    const passwords = {};
    el.querySelectorAll("input[data-pwd]").forEach(inp => {
      if (inp.value) passwords[inp.dataset.pwd] = inp.value;
    });
    const sources = collectSources();
    const files = STATE.inbox_files || [];
    // 密码按「渠道」归拢，所以手动改了渠道的话要按新渠道去找密码，
    // 否则用户明明填了密码，却因为渠道变了而没被送进去。
    const missing = files.filter(f => f.needs_password && !passwords[sources[f.path] || f.source]);
    if (missing.length) {
      const ok = confirm("有 " + missing.length + " 个加密文件没填密码，它们会被跳过。继续吗？");
      if (!ok) return;
    }
    const j = await runJob("monthly", {
      ai: document.getElementById("opt-ai").checked,
      keep: document.getElementById("opt-keep").checked,
      dry_run: document.getElementById("opt-dry").checked,
      passwords: passwords,
      sources: sources
    }, "月度处理");
    el.querySelectorAll("input[data-pwd]").forEach(inp => inp.value = "");
    if (j && j.status === "done" && (j.result || {}).exit === 2) show("import");
  };
}

/* 渠道下拉框。默认选中自动识别出来的那个；
   认不出来时停在「自动（识别不出）」，逼用户自己选一个。 */
function sourceSelect(f) {
  const cur = f.source === "unknown" ? "" : f.source;
  let h = "<select data-src='" + esc(f.path) + "'>" +
          "<option value=''" + (cur ? "" : " selected") + ">自动（识别不出，请选）</option>";
  (STATE.source_choices || []).forEach(c => {
    h += "<option value='" + esc(c.value) + "'" + (cur === c.value ? " selected" : "") + ">" +
         esc(c.label) + "</option>";
  });
  return h + "</select>";
}

/* 只收集「用户手动改过」的文件。选「自动」的不算 ——
   把自动的结果也记一遍会让记忆表变成启发式的影子，
   启发式升级后那些旧记录反而会挡住新逻辑。 */
function collectSources() {
  const out = {};
  document.querySelectorAll("select[data-src]").forEach(s => {
    if (s.value) out[s.dataset.src] = s.value;
  });
  return out;
}

async function rememberAll() {
  const sources = collectSources();
  const keys = Object.keys(sources);
  if (!keys.length) { alert("还没有手动选过任何渠道。"); return; }
  for (const p of keys) {
    const f = (STATE.inbox_files || []).find(x => x.path === p);
    try {
      await api("/api/setsrc", { key: f ? f.shape : "", source: sources[p] });
    } catch (e) {
      alert("记住 " + (f ? f.name : p) + " 失败：" + e.message);
      return;
    }
  }
  await refreshState();
  alert("已记住 " + keys.length + " 个形状。以后同类文件自动按你选的渠道解析。");
}

/* ---------------- 疑似重复 ---------------- */
function renderReview() {
  const el = document.getElementById("tab-review");
  if (reviewIdx >= REVIEW.length) {
    el.innerHTML = "<div class='card'><h2>没有待确认的疑似重复</h2>" +
      "<p class='muted'>这个页面只在导入后出现记录时才有活干。如果没有，说明本次导入很干净。</p>" +
      "<button class='act ghost' onclick='loadReview()'>重新检查</button></div>";
    return;
  }
  const it = REVIEW[reviewIdx], o = it.other;
  const same = k => o && String(it.self[k]) === String(o[k]);
  const card = (t, other) => "<div class='txn'><div class='muted'>" + esc(t.source_label) +
    "</div><div class='amount'>" + esc(t.amount) + " 元</div><dl>" +
    fld("时间", t.ts, other && String(t.ts) === String(other.ts)) +
    fld("对方", t.counterparty, other && String(t.counterparty) === String(other.counterparty)) +
    fld("商品", t.item, other && String(t.item) === String(other.item)) +
    fld("方式", t.method, other && String(t.method) === String(other.method)) +
    fld("单号", t.txn_id || "（无）", other && String(t.txn_id) === String(other.txn_id)) +
    "</dl></div>";
  function fld(l, v, hl) {
    return "<dt>" + esc(l) + "</dt><dd class='" + (hl ? "same" : "") + "'>" +
           (esc(v) || "—") + "</dd>";
  }
  el.innerHTML = "<div class='card'><h2>疑似重复确认<span>第 " + (reviewIdx + 1) + " / " +
    REVIEW.length + " 笔 · 按 1 = 同一笔，按 2 = 两笔独立</span></h2>" +
    "<div class='note'><b>为什么怀疑重复：</b>" + esc(it.reason) + "</div>" +
    "<div class='pair'>" + card(it.self, o) +
    (o ? card(o, it.self) : "<div class='txn'><div class='muted'>未找到对应记录</div></div>") +
    "</div><div class='actions2'>" +
    "<button class='big dup' onclick='decide(\"dup\")'><span class='kbd'>1</span>是同一笔" +
    "<small>从统计里剔除这一笔，记录仍然保留</small></button>" +
    "<button class='big ok' onclick='decide(\"ok\")'><span class='kbd'>2</span>是两笔独立交易" +
    "<small>保留两笔，以后不再提示</small></button></div></div>";
}
async function decide(d) {
  if (busy || reviewIdx >= REVIEW.length) return;
  busy = true;
  const it = REVIEW[reviewIdx];
  try {
    await api("/api/run", { action: "resolve", params: { id: it.id, decision: d } });
    await new Promise(r => setTimeout(r, 200));
    await refreshState();
  } catch (e) {
    alert("写入失败：" + e.message);
  } finally {
    busy = false;
    reviewIdx++;
    renderReview();
  }
}
async function loadReview() {
  try {
    const d = await api("/api/review");
    REVIEW = d.items || [];
    reviewIdx = 0;
    renderReview();
  } catch (e) {
    document.getElementById("tab-review").innerHTML =
      "<div class='card'><div class='warn'>" + esc(e.message) + "</div></div>";
  }
}
document.addEventListener("keydown", e => {
  const on = document.getElementById("tab-review").classList.contains("on");
  if (!on) return;
  if (e.key === "1" || e.key === "ArrowLeft") decide("dup");
  if (e.key === "2" || e.key === "ArrowRight") decide("ok");
});

/* ---------------- 报表 ---------------- */
function renderReport() {
  const el = document.getElementById("tab-report");
  if (!STATE) return;
  if (!(STATE.months || []).length) {
    el.innerHTML = "<div class='card'><h2>还没有数据</h2><p class='muted'>先到「导入」页处理账单。</p></div>";
    return;
  }
  if (!curMonth || STATE.months.indexOf(curMonth) < 0) curMonth = STATE.months[0];
  el.innerHTML = "<div class='card'><h2>选择月份</h2><div class='row'>" +
    "<select id='month-sel'>" + STATE.months.map(m =>
      "<option value='" + m + "'" + (m === curMonth ? " selected" : "") + ">" + m + "</option>").join("") +
    "</select><button class='act' onclick='loadReport()'>查看</button></div></div>" +
    "<div id='report-body'><div class='card'><div class='muted'>正在加载…</div></div></div>";
  document.getElementById("month-sel").onchange = e => { curMonth = e.target.value; loadReport(); };
  loadReport();
}
async function loadReport() {
  const box = document.getElementById("report-body");
  if (!box) return;
  try {
    const d = await api("/api/report?month=" + encodeURIComponent(curMonth));
    let h = "<div class='card'><h2>" + esc(d.month) + " 支出构成<span>已剔除确认重复</span></h2>";
    if (!d.expense.length) h += "<p class='muted'>本月没有支出。</p>";
    else {
      h += "<table><thead><tr><th>分类</th><th class='num'>金额(元)</th><th class='num'>笔数</th></tr></thead><tbody>";
      d.expense.forEach(r => {
        h += "<tr><td>" + esc(r[0]) + "</td><td class='num'>" + esc(r[1]) +
             "</td><td class='num'>" + r[2] + "</td></tr>";
      });
      h += "<tr class='sum'><td>合计</td><td class='num'>" + esc(d.expense_total) +
           "</td><td class='num'>" + d.expense_n + "</td></tr></tbody></table>";
    }
    h += "<div class='grid' style='margin-top:14px'>" +
         kpi(esc(d.income) + " 元", "收入合计") +
         kpi(esc(d.neutral) + " 元", "充值/提现/还款等（不计消费）") + "</div></div>";
    box.innerHTML = h;
  } catch (e) {
    box.innerHTML = "<div class='card'><div class='warn'>" + esc(e.message) + "</div></div>";
  }
}

/* ---------------- 明细（点开看具体是哪几笔） ---------------- */
let DETAIL = null;
let detailOpen = {};          // 分类 -> true/false，记住哪些展开了
let detailFilter = "";        // 搜索词
let detailDir = "";           // 方向筛选

/* 方向的显示名与配色。三个方向的名字要跟「报表」页一致，
   否则用户会以为是两套口径。 */
const DIR_LABEL = {
  expense: "支出", income: "收入", neutral: "中性",
};
function dirClass(d) {
  return d === "expense" ? "bad" : (d === "income" ? "good" : "muted");
}
function shortTime(ts) {
  /* 库里存的是 "2026-09-30 18:56:58"，明细里只关心「哪天几点」，
     完整秒数留着占地方。 */
  return (ts || "").slice(5, 16);
}
function renderDetail() {
  const el = document.getElementById("tab-detail");
  if (el.dataset.ready !== "1") {
    el.innerHTML =
      "<div class='note'>报表只给「分类 → 金额 → 笔数」的汇总，" +
      "数字对不上时得看具体是哪几笔 —— 这里就是那份明细。" +
      "按分类点开，可搜商户/摘要。<br>" +
      "已确认的重复<b>不在这里出现</b>（口径与报表页一致）。</div>" +
      "<div id='detail-body'><div class='card'><div class='muted'>正在加载…</div></div></div>";
    el.dataset.ready = "1";
  }
  loadDetail();
}
async function loadDetail() {
  const box = document.getElementById("detail-body");
  if (!box) return;
  try {
    const m = curMonth ? ("?month=" + encodeURIComponent(curMonth)) : "";
    const d = await api("/api/detail" + m);
    DETAIL = d;
    let h = "<div class='card'>";
    /* 月份切换。用库里真实有的月份，不用「当前月」——
       导入历史账单时当前月可能是空的。 */
    if ((d.months || []).length > 1) {
      h += "<div class='row'><span class='muted'>月份</span><select id='d-month'>";
      d.months.forEach(mm => {
        h += "<option value='" + esc(mm) + "'" + (mm === d.month ? " selected" : "") +
             ">" + esc(mm) + "</option>";
      });
      h += "</select></div>";
    }
    h += "<div class='row'><input id='d-q' placeholder='搜商户 / 摘要 / 备注' value='" +
         esc(detailFilter) + "'><select id='d-dir'>" +
         "<option value=''>全部方向</option>" +
         ["expense", "income", "neutral"].map(k =>
           "<option value='" + k + "'" + (detailDir === k ? " selected" : "") +
           ">" + DIR_LABEL[k] + "</option>").join("") +
         "</select>" +
         "<button class='act' onclick='detailGo()'>筛选</button>" +
         "<button class='act' onclick='detailClear()'>清空</button></div>";
    h += "<div class='grid' style='margin:12px 0'>" +
         kpi(d.n + " 笔", "本月交易总数") +
         kpi(esc(d.expense_total) + " 元", "其中支出") +
         kpi(String(Object.keys(d.by_category || {}).length), "涉及分类数") +
         "</div>";
    if (d.truncated) {
      h += "<div class='warn'>本月超过 1000 笔，只显示最近 1000 笔。</div>";
    }
    box.innerHTML = h + "</div>" + detailList();
    const ms = document.getElementById("d-month");
    if (ms) ms.onchange = e => { curMonth = e.target.value; detailOpen = {}; loadDetail(); };
  } catch (e) {
    box.innerHTML = "<div class='card'><div class='warn'>" + esc(e.message) + "</div></div>";
  }
}
function detailGo() {
  const q = document.getElementById("d-q");
  const dd = document.getElementById("d-dir");
  detailFilter = q ? q.value.trim() : "";
  detailDir = dd ? dd.value : "";
  loadDetail();
}
function detailClear() {
  detailFilter = ""; detailDir = ""; detailOpen = {};
  loadDetail();
}
/* 把明细按分类列成可展开的表。 */
function detailList() {
  if (!DETAIL) return "";
  const q = detailFilter.toLowerCase();
  const groups = Object.entries(DETAIL.by_category || {});
  if (!groups.length) {
    return "<div class='card'><p class='muted'>这个月还没有交易。</p></div>";
  }
  /* 分类按该分类的支出总额降序 —— 与报表页口径一致，
     免得同一份数据在两个页面里顺序不同。 */
  const sorted = groups.map(([cat, items]) => {
    const exp = items.filter(t => t.direction === "expense")
                     .reduce((a, t) => a + Number(t.amount), 0);
    return [cat, items, exp];
  }).sort((a, b) => b[2] - a[2]);

  let h = "";
  sorted.forEach(([cat, items, exp]) => {
    let list = items;
    if (detailDir) list = list.filter(t => t.direction === detailDir);
    if (q) {
      list = list.filter(t =>
        ((t.counterparty || "") + " " + (t.item || "")).toLowerCase().includes(q));
    }
    /* 筛选后该分类一笔都不剩就别显示了 —— 显示一个空分类只会让人以为筛选出错 */
    if (!list.length) return;
    const open = !!detailOpen[cat];
    const shown = open ? list : list.slice(0, 1);
    h += "<div class='card'>" +
         "<div class='dcat' onclick='detailToggle(\"" + esc(cat).replace(/"/g, "&quot;") + "\')'>" +
           "<span class='caret'>" + (open ? "▾" : "▸") + "</span>" +
           "<b>" + esc(cat) + "</b>" +
           "<span class='muted'>" + list.length + " 笔" +
             (exp > 0 ? " · 支出 " + exp.toFixed(2) + " 元" : "") + "</span>" +
         "</div>";
    h += "<table><thead><tr><th>时间</th><th>方向</th><th class='num'>金额(元)</th>" +
         "<th>商户 / 对方</th><th>摘要</th><th>来源</th></tr></thead><tbody>";
    shown.forEach(t => {
      /* 中性交易默认折叠时只给一行 —— 它们大多是转存/提现，
         逐条列出来会把真正想看的支出淹没。 */
      h += "<tr><td class='muted'>" + esc(shortTime(t.ts)) + "</td>" +
           "<td class='" + dirClass(t.direction) + "'>" + (DIR_LABEL[t.direction] || "") + "</td>" +
           "<td class='num'>" + esc(t.amount) + "</td>" +
           "<td>" + esc(t.counterparty || "—") + "</td>" +
           "<td class='muted'>" + esc(t.item || "") + "</td>" +
           "<td class='muted'>" + esc(t.source) + "</td></tr>";
    });
    h += "</tbody></table>";
    if (list.length > shown.length) {
      h += "<div class='more'><button class='act' onclick='detailToggle(\"" +
           esc(cat).replace(/"/g, "&quot;") + "\')'>展开其余 " +
           (list.length - shown.length) + " 笔</button></div>";
    }
    h += "</div>";
  });
  if (!h) {
    h = "<div class='card'><p class='muted'>没有符合筛选条件的交易。</p></div>";
  }
  return h;
}
function detailToggle(cat) {
  detailOpen[cat] = !detailOpen[cat];
  const box = document.getElementById("detail-body");
  if (box && DETAIL) {
    /* 只重画列表部分，保留上面的月份/搜索框，
       否则用户输了一半的搜索词会被清掉。 */
    const pos = box.innerHTML.indexOf("</div><div class='card'");
    if (pos > 0) {
      const keep = box.innerHTML.slice(0, pos + 6);
      box.innerHTML = keep + detailList();
    } else {
      box.innerHTML = detailList();
    }
  }
}

/* ---------------- 未分类 ---------------- */
function renderUnmatched() {
  const el = document.getElementById("tab-unmatched");
  if (!STATE) return;
  el.innerHTML =
    "<div class='note'>规则靠关键词，总有认不出来的商户。这里列出它们，" +
    "你选个分类点一下就行 —— 不写代码、不改规则表，按商户精确生效，" +
    "不会影响别的交易。</div>" +
    "<div class='card'><h2>未分类汇总</h2><div id='um-totals' class='grid'></div>" +
    "<div class='row' style='margin-top:12px'>" +
    "<label class='chk'><input type='checkbox' id='um-mask'" + (maskOn ? " checked" : "") +
    "> 遮蔽商户名（这份输出可以安全发给别人看）</label>" +
    "<button class='act ghost sm' onclick='loadUnmatched()'>刷新</button></div></div>" +
    "<div class='card'><h2>归位</h2><div class='row'>" +
    "<input id='um-merchant' list='um-list' placeholder='商户名（可从下表复制）' style='flex:1;min-width:240px'>" +
    "<datalist id='um-list'></datalist>" +
    "<select id='um-cat'>" + (STATE.categories || []).map(c =>
      "<option value='" + esc(c) + "'>" + esc(c) + "</option>").join("") + "</select>" +
    "<button class='act ghost' onclick='doSetcat(false)'>试算</button>" +
    "<button class='act good' onclick='doSetcat(true)'>执行</button></div>" +
    "<p class='muted' style='margin:10px 0 0'>「试算」只写缓存（以后导入自动生效），" +
    "不动已入库的历史交易；「执行」才会一并回填历史，写库前自动备份密文。</p></div>" +
    "<div class='card'><h2>已人工指定的商户<span id='um-manual-n'></span></h2>" +
    "<div id='um-manual'></div></div>" +
    "<div class='card'><h2>未分类明细<span id='um-scope'></span></h2>" +
    "<div id='um-body'><div class='muted'>正在加载…</div></div></div>";
  document.getElementById("um-mask").onchange = e => { maskOn = e.target.checked; loadUnmatched(); };
  const s = document.getElementById("um-cat");
  if (s && s.options.length) s.value = "餐饮";
  loadUnmatched();
  renderManual();
}
async function loadUnmatched() {
  const body = document.getElementById("um-body");
  try {
    const d = await api("/api/unmatched?mask=" + (maskOn ? "1" : "0"));
    const t = d.totals;
    document.getElementById("um-totals").innerHTML =
      kpi(t.n + " 笔", "未分类交易") + kpi(t.merchants, "涉及商户") +
      kpi(t.expense + " 元", "其中支出") + kpi(t.share + "%", "占同期支出") +
      kpi(t.income + " 元", "其中收入");
    document.getElementById("um-scope").textContent = maskOn ? "（商户名已遮蔽）" : "";
    const dl = document.getElementById("um-list");
    if (dl) dl.innerHTML = d.rows.map(r =>
      "<option value='" + esc(r.merchant) + "'></option>").join("");
    if (!d.rows.length) {
      body.innerHTML = "<p class='muted'>没有未分类的交易，规则覆盖得不错。</p>";
      return;
    }
    let h = "<table><thead><tr><th>商户</th><th class='num'>笔数</th>" +
            "<th class='num'>支出(元)</th><th class='num'>收入(元)</th><th>最近一笔</th></tr></thead><tbody>";
    d.rows.forEach(r => {
      h += "<tr><td>" + esc(r.merchant) + "</td><td class='num'>" + r.n +
           "</td><td class='num'>" + esc(r.expense) + "</td><td class='num'>" + esc(r.income) +
           "</td><td class='muted'>" + esc(r.last_ts) + "</td></tr>";
    });
    body.innerHTML = h + "</tbody></table>";
  } catch (e) {
    body.innerHTML = "<div class='warn'>" + esc(e.message) + "</div>";
  }
}
function renderManual() {
  const box = document.getElementById("um-manual");
  if (!box || !STATE) return;
  const list = STATE.manual || [];
  document.getElementById("um-manual-n").textContent = list.length ? "共 " + list.length + " 个" : "";
  if (!list.length) { box.innerHTML = "<p class='muted'>还没有人工指定。规则和 AI 之外，你说了算。</p>"; return; }
  let h = "<table><thead><tr><th>分类</th><th>商户</th><th></th></tr></thead><tbody>";
  list.forEach(m => {
    h += "<tr><td>" + esc(m.category) + "</td><td>" + esc(m.merchant) +
         "</td><td style='text-align:right'><button class='act ghost sm' " +
         "onclick='doClear(\"" + esc(m.merchant).replace(/"/g, "&quot;") + "\")'>撤销</button></td></tr>";
  });
  box.innerHTML = h + "</tbody></table>";
}
async function doSetcat(apply) {
  const merchant = document.getElementById("um-merchant").value.trim();
  if (!merchant) { alert("先填商户名。"); return; }
  const category = document.getElementById("um-cat").value;
  await runJob("setcat", { merchant: merchant, category: category, apply: !!apply },
    "手工指定分类：" + merchant);
  renderUnmatched();
}
async function doClear(merchant) {
  if (!confirm("撤销「" + merchant + "」的人工指定，并回填历史？\n（写库前会自动备份密文）")) return;
  await runJob("setcat", { merchant: merchant, clear: true, apply: true }, "撤销人工指定：" + merchant);
  renderUnmatched();
}

/* ---------------- 历史修正 ---------------- */
function renderFix() {
  document.getElementById("tab-fix").innerHTML =
    "<div class='card'><h2>按当前规则重算历史数据</h2>" +
    "<div class='note'>改了分类规则之后，旧数据不会自动跟着变。这个操作把全库按" +
    "<b>当前规则</b>重算一遍分类和收支方向。<br>" +
    "优先级：<b>人工指定 &gt; 规则 &gt; 原值</b>（你手工指定的永远不会被改回去）。<br>" +
    "操作幂等，重复跑结果一样。</div>" +
    "<div class='row'>" +
    "<button class='act ghost' onclick='doRecompute(false)'>先试算（不改任何数据）</button>" +
    "<button class='act danger' onclick='doRecompute(true)'>执行（写库前自动备份）</button>" +
    "</div></div>" +
    "<div class='card'><h2>说明</h2><p class='muted' style='margin:0'>" +
    "「试算」会把每一笔的改动列在下面的日志里，看完确认没问题再点「执行」。<br>" +
    "因为试算不动数据，所以试算和执行的输出应该完全一致；不一致说明两次之间账本被别处改过。</p></div>" +
    renderHints();
}
/* ---------------- 分类诊断 ----------------
   判定叠了四层分类 + 一个状态判定之后，光看报表答不出
   「这一笔为什么是那个分类」。这个页面把每层的实际命中摊开，
   并把「还没被映射 / 还认不出来」的值单列出来 —— 那是迭代
   映射表和状态词表的唯一入口。 */
function renderClassify() {
  const el = document.getElementById("tab-classify");
  if (!STATE) return;
  el.innerHTML =
    "<div class='note'>分类优先级：<b>人工指定 &gt; 平台分类 &gt; 关键词规则 &gt; 原值</b>。" +
    "「平台分类」是支付宝账单里的「交易分类」列、微信的「交易类型」列 —— " +
    "平台已经判好的结果，比靠商户名猜更准，所以它排在规则前面，但压不过你的人工指定。" +
    "<br><br>方向判定：先看「收/支」列，再看<b>交易状态</b>。" +
    "状态显示这笔没真花出去（交易关闭 / 已退款 / 已过期）的，方向压成不计收支 —— " +
    "记录留着能对账，但不进总支出。状态认不出来时一律按原样保留，宁可放过也不误伤。</div>" +
    "<div class='card'><h2>平台分类</h2>" +
    "<div class='row'><label class='chk'><input type='checkbox' id='cf-mask'" +
    (maskOn ? " checked" : "") + "> 遮蔽原文（这份输出可以安全发给别人看）</label>" +
    "<button class='act ghost sm' onclick='loadClassify()'>刷新</button></div>" +
    "<div id='cf-plat'><div class='muted'>正在加载…</div></div></div>" +
    "<div class='card'><h2>交易状态</h2><div id='cf-stat'>" +
    "<div class='muted'>正在加载…</div></div></div>";
  document.getElementById("cf-mask").onchange = e => { maskOn = e.target.checked; loadClassify(); };
  loadClassify();
}
async function loadClassify() {
  const pb = document.getElementById("cf-plat");
  const sb = document.getElementById("cf-stat");
  if (!pb || !sb) return;
  try {
    const d = await api("/api/classify?mask=" + (maskOn ? "1" : "0"));
    if (!d.platform.length) {
      pb.innerHTML = "<p class='muted' style='margin:10px 0 0'>库里还没有平台分类数据。" +
        "支付宝账单看「交易分类」列、微信看「交易类型」列 —— 这两列只有<b>新版导出</b>里有，" +
        "老账单可能没有，属正常。这种情况下分类全部靠关键词规则和你的手工指定。</p>";
    } else {
      let h = "<table><thead><tr><th>来源</th><th>平台分类</th><th>判定为</th>" +
              "<th class='num'>笔数</th><th class='num'>金额(元)</th></tr></thead><tbody>";
      d.platform.forEach(r => {
        const miss = !r.hit;
        h += "<tr" + (miss ? " style='color:#b06'" : "") + "><td>" + esc(r.source) +
             "</td><td>" + esc(r.value) + "</td><td>" +
             (miss ? "<span class='muted'>（未映射，靠规则兜底）</span>" : esc(r.hit)) +
             "</td><td class='num'>" + r.n + "</td><td class='num'>" +
             esc(r.total) + "</td></tr>";
      });
      const miss = d.platform.filter(r => !r.hit);
      if (miss.length) {
        h += "</tbody></table><p class='muted' style='margin:10px 0 0'>" +
             "标色的几行是平台分类<b>没能直接认出来</b>的，靠关键词规则兜底。" +
             "微信「商户消费」属于这一类（它占九成流水，信息量为零，属正常）。" +
             "如果看到明显该认却没认的值，往 <code>importer/platformmap.py</code> 里加一行，</p>";
      } else {
        h += "</tbody></table>";
      }
      pb.innerHTML = h;
    }

    if (!d.status.length) {
      sb.innerHTML = "<p class='muted' style='margin:10px 0 0'>库里没有交易状态数据。" +
        "支付宝的「交易状态」列、微信的「当前状态」列，老版本导出可能没有。</p>";
    } else {
      const names = {ok: "已确认完成", refunded: "已退款", failed: "未成立",
                     pending: "未完成", unknown: "认不出（按原样保留）"};
      let h = "<table><thead><tr><th>状态原文</th><th>判定</th><th>当前分布</th></tr></thead><tbody>";
      let killed = 0, killedTotal = 0, unknown = [];
      d.status.forEach(r => {
        const parts = [];
        if (r.expense_n) parts.push("支出 " + r.expense_n + " 笔 / " + r.expense + " 元");
        if (r.income_n) parts.push("收入 " + r.income_n + " 笔 / " + r.income + " 元");
        if (r.neutral_n) parts.push("中性 " + r.neutral_n + " 笔 / " + r.neutral + " 元");
        if (r.killed && r.neutral_n) {
          killed += r.neutral_n;
          killedTotal += parseFloat(String(r.neutral).replace(/,/g, "")) || 0;
        }
        if (r.verdict === "unknown") unknown.push(r.status);
        h += "<tr><td>" + esc(r.status) + "</td><td>" +
             esc(names[r.verdict] || r.verdict) + "</td><td class='muted'>" +
             esc(parts.join("；") || "（无）") + "</td></tr>";
      });
      h += "</tbody></table>";
      if (killed) {
        h += "<p class='muted' style='margin:10px 0 0'>落在「未成立 / 已退款 / 未完成」" +
             "这些状态里的共 " + killed + " 笔、" +
             killedTotal.toLocaleString("en-US", {minimumFractionDigits: 2,
               maximumFractionDigits: 2}) +
             " 元，全部不在支出里 —— 总支出因此少了这一块。</p>";
      }
      h += "<p class='muted' style='margin:6px 0 0'>提示：标「中性」的那些里，" +
           "有一部分在状态判定之前就已经是不计收支了（余额宝转入、信用卡还款），" +
           "它们不是被状态剔掉的。</p>";
      if (unknown.length) {
        h += "<p class='muted' style='margin:10px 0 0'>这些状态值本工具<b>还认不出来</b>，" +
             "一律按原样保留（宁可放过也不误伤）：<code>" +
             unknown.map(esc).join("</code>、<code>") +
             "</code>。确认语义后加进 <code>importer/model.py</code> 的 <code>_STATUS_*</code> 词表。</p>";
      }
      sb.innerHTML = h;
    }
  } catch (e) {
    pb.innerHTML = "<div class='warn'>" + esc(e.message) + "</div>";
  }
}
/* 已记住的渠道。必须能在这里看到并撤销 ——
   一条记错的记忆会一直生效，用户却完全不知道它存在，
   那比「没有记忆功能」更坏。 */
function renderHints() {
  const hints = (STATE && STATE.hints) || [];
  if (!hints.length) {
    return "<div class='card'><h2>记住的渠道</h2><p class='muted' style='margin:0'>" +
           "还没有记住任何渠道。导入页把渠道选错时，点「把手动选过的都记住」就会出现在这里。" +
           "</p></div>";
  }
  let h = "<div class='card'><h2>记住的渠道<span>" + hints.length + " 条</span></h2>" +
          "<div class='note'>键是<b>形状</b>（文件后缀 / 压缩包里装的东西），不是文件名。" +
          "所以银行每月寄来的流水号文件名不一样也不影响。" +
          "命中记忆的渠道会<b>优先于</b>自动识别。</div>" +
          "<table><thead><tr><th>形状</th><th>渠道</th><th></th></tr></thead><tbody>";
  hints.forEach(x => {
    h += "<tr><td><code>" + esc(x.key) + "</code></td><td>" + esc(x.label) + "</td>" +
         "<td class='num'><button class='act ghost sm' data-forget='" + esc(x.key) +
         "'>撤销</button></td></tr>";
  });
  return h + "</tbody></table></div>";
}
/* 事件委托：记忆条目是动态渲染的，逐个绑 onclick 会漏。
   用 data 属性 + 一个监听器，重新渲染后照样生效。 */
document.addEventListener("click", e => {
  const btn = e.target.closest("[data-forget]");
  if (btn) forgetHint(btn.dataset.forget);
});
async function forgetHint(key) {
  if (!confirm("撤销「" + key + "」这条记忆？\n撤销后这类文件会回到自动识别。")) return;
  try {
    await api("/api/setsrc", { key: key, clear: true });
  } catch (e) {
    alert("撤销失败：" + e.message);
    return;
  }
  await refreshState();
  renderFix();
}
async function doRecompute(apply) {
  if (apply && !confirm("确认按当前规则重写历史数据？\n（会先自动备份加密账本，可回滚）")) return;
  await runJob("recompute", { apply: !!apply }, apply ? "历史修正（执行）" : "历史修正（试算）");
  renderUnmatched();
}

/* ---------------- 帮助 ---------------- */
function renderHelp() {
  document.getElementById("tab-help").innerHTML =
    "<div class='card'><h2>怎么用</h2><pre class='log' style='background:var(--bg);color:var(--text);" +
    "border:1px solid var(--line)'>" + esc(USAGE_TEXT) + "</pre></div>" +
    "<div class='card'><h2>各渠道怎么导出账单</h2><pre class='log' style='background:var(--bg);" +
    "color:var(--text);border:1px solid var(--line)'>" + esc(SOURCES_TEXT) + "</pre></div>" +
    "<div class='card'><h2>还想用命令行？</h2><p class='muted' style='margin:0'>" +
    "网页上做的一切都能在命令行做到，命令清单见仓库里的 <code>docs/HANDOVER.md</code>。</p></div>";
}

async function openFolder() {
  try { await api("/api/open_inbox", {}); }
  catch (e) { alert("打不开文件夹：" + e.message + "\n路径见上方，可以复制后自己打开。"); }
}
async function copyText(t) {
  try { await navigator.clipboard.writeText(t); } catch (e) { alert(t); }
}

async function refreshState() {
  try {
    STATE = await api("/api/state");
  } catch (e) {
    STATE = STATE || {};
    STATE.error = e.message;
  }
  const o = STATE.overview;
  document.getElementById("meta").innerHTML =
    "保管库 " + esc(VAULT) + "<br>" + esc(STATE.power || "") + " ｜ " + esc(STATE.now || "");
  const bInbox = document.getElementById("b-inbox");
  const n = (STATE.inbox_files || []).length;
  bInbox.style.display = n ? "inline-block" : "none";
  bInbox.textContent = n;
  const bRev = document.getElementById("b-review");
  const r = STATE.review_left || 0;
  bRev.style.display = r ? "inline-block" : "none";
  bRev.textContent = r;
  renderHome();
  const on = document.querySelector("#nav button.on");
  const tab = on ? on.dataset.tab : "home";
  if (tab === "import") renderImport();
  if (tab === "unmatched") renderUnmatched();
  if (tab === "classify") renderClassify();
  if (tab === "report") renderReport();
}

(async function boot() {
  renderHelp();
  // 顺序有讲究：renderFix 里要画「记住的渠道」，那数据来自 STATE。
  // 先 refreshState 再 renderFix，否则第一次打开是空的，
  // 得手动点一次刷新才出现 —— 看起来像功能坏了。
  await refreshState();
  renderFix();
  await loadReview();
  if (STATE && STATE.review_left) show("review");
})();

/* 让 URL 能直接打开某个页签：`/?t=令牌#detail`
   用途有两个：
     1. 可以把「明细」「未分类」这类常看的页面**收藏成书签**
     2. 排查问题时能把某个页面的确切状态发给别人看
   认不出的页签一律回「概览」，不能白屏。 */
(function () {
  const want = (location.hash || "").replace(/^#/, "");
  const known = ["home", "import", "review", "report", "detail",
                 "unmatched", "classify", "fix", "help"];
  if (want && known.indexOf(want) >= 0 && !(STATE && STATE.review_left)) {
    show(want);
  }
})();
</script>
</body>
</html>
"""


def render_page(token: str, vault: str) -> str:
    def js(value: str) -> str:
        # 直接塞进 JS 字面量：json.dumps 负责转义，再把 < 换成 \\u003c，
        # 免得路径里出现 </script> 这种能提前闭合脚本块的东西。
        return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")

    return (PAGE
            .replace("__TOKEN__", token)
            .replace("__VAULT_JSON__", js(vault))
            .replace("__SOURCES_JSON__", js(SOURCES_TEXT))
            .replace("__USAGE_JSON__", js(USAGE_TEXT)))


# ==========================================================================
# 服务
# ==========================================================================

class Console:
    """网页控制台的后端。

    只有一个工作线程，所有动作排同一个队列串行跑；
    只读查询和动作共用一把锁（读会短暂等待，超时就返回上一次的快照），
    这样既不会并发动账本，也不会因为一个长任务把界面卡死。
    """

    def __init__(self, vault: str, token: str):
        self.vault = vault
        self.token = token
        self.lock = threading.Lock()
        self.jobs: dict = {}
        self.queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self.jobs_lock = threading.Lock()
        self.counter = 0
        self.cached = None
        self.worker = threading.Thread(target=self._loop, daemon=True)
        self.worker.start()

    # ---------------- 任务 ---------------- #
    def submit(self, action: str, params: dict) -> dict:
        fn = ACTIONS.get(action)
        if fn is None:
            raise ValueError(f"不支持的操作：{action}")
        with self.jobs_lock:
            running = [j for j in self.jobs.values() if j["status"] == "running"]
            if running:
                raise RuntimeError("已经有任务在跑，等它结束再试。")
            self.counter += 1
            job_id = f"j{self.counter}"
            job = {
                "id": job_id, "action": action, "params": params,
                "status": "running", "lines": [], "result": None,
                "error": "", "lock": threading.Lock(),
            }
            self.jobs[job_id] = job
            self.queue.put(job_id)
        return job

    def get_job(self, job_id: str) -> Optional[dict]:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        with job["lock"]:
            lines = list(job["lines"])
        return {"id": job["id"], "status": job["status"], "lines": lines,
                "result": job["result"], "error": job["error"]}

    def _loop(self) -> None:
        while True:
            job_id = self.queue.get()
            if job_id is None:
                return
            job = self.jobs.get(job_id)
            if job is None:
                continue
            fn = ACTIONS[job["action"]]

            def log(line, _job=job):
                with _job["lock"]:
                    _job["lines"].append(str(line))

            params = job["params"]

            def guarded():
                log("=" * 60)
                log(f"{job['action']}  开始 {datetime.now():%H:%M:%S}")
                log("=" * 60)
                try:
                    result = fn(self.vault, params, log)
                    job["result"] = result
                    job["status"] = "done"
                    log(f"—— 结束（{datetime.now():%H:%M:%S}）")
                except Exception as exc:
                    import traceback
                    log(f"！{type(exc).__name__}: {exc}")
                    for extra in traceback.format_exc().strip().splitlines()[-4:]:
                        log("  " + extra)
                    job["error"] = f"{type(exc).__name__}: {exc}"
                    job["status"] = "error"

            with self.lock:
                guarded()
            # 完成后立即丢掉参数：密码一类的临时内容不该在内存里多活一秒
            job["params"] = {}

    # ---------------- 只读 ---------------- #
    def quick(self, fn, fallback=None, timeout: float = 2.0):
        """拿锁做一次只读查询。拿不到锁（有任务在跑）就返回兜底值。"""
        if not self.lock.acquire(timeout=timeout):
            return {"stale": True, **(fallback or {})}
        try:
            return fn()
        finally:
            self.lock.release()

    def state(self) -> dict:
        def go():
            self.cached = snapshot(self.vault, warn=lambda s: None)
            return self.cached
        return self.quick(go, fallback=(self.cached or {}), timeout=2.0)

    def stop(self) -> None:
        self.queue.put(None)


def build_server(vault: str, *, port: int = DEFAULT_PORT, host: str = "127.0.0.1"):
    """建好服务但不启动。返回 (httpd, console, token)。

    单独拆出来是为了能测：run() 会开浏览器并阻塞在 serve_forever 上，
    自动化测试里没法用。
    """
    if host not in LOCAL_HOSTS:
        raise ValueError(f"拒绝绑定 {host}：这个控制台只允许本机访问。")

    token = secrets.token_urlsafe(24)
    console = Console(vault, token)
    page = render_page(token, vault)

    class Handler(BaseHTTPRequestHandler):
        server_version = "MyLedgerConsole/1.0"
        protocol_version = "HTTP/1.1"

        # ---------- 基础设施 ---------- #
        def _authorised(self) -> bool:
            if self.headers.get("X-MyLedger-Token") == token:
                return True
            from urllib.parse import parse_qs, urlparse
            return parse_qs(urlparse(self.path).query).get("t", [""])[0] == token

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

        def _json(self, payload, code: int = 200) -> None:
            self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _plain(self, text: str, code: int) -> None:
            self._send(code, text.encode("utf-8"), "text/plain; charset=utf-8")

        def log_message(self, *args) -> None:
            pass    # 默认日志会把含令牌的 URL 刷进控制台

        # ---------- GET ---------- #
        def do_GET(self) -> None:      # noqa: N802
            from urllib.parse import parse_qs, urlparse
            parsed = urlparse(self.path)
            path, query = parsed.path, parse_qs(parsed.query)

            if path == "/":
                # 页面本身要能在浏览器里直接打开，所以用令牌放行后才发；
                # 没令牌的访问一律 403，避免本机其它进程乱打。
                if query.get("t", [""])[0] != token:
                    self._plain("forbidden", 403)
                    return
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                return
            if not self._authorised():
                self._plain("forbidden", 403)
                return

            try:
                if path == "/api/state":
                    self._json(console.state())
                    return
                if path == "/api/review":
                    self._json(console.quick(lambda: review_data(vault),
                                             fallback={"items": []}))
                    return
                if path == "/api/report":
                    month = query.get("month", [""])[0]
                    self._json(console.quick(lambda: report_data(vault, month),
                                             fallback={"month": month, "expense": [],
                                                       "expense_total": "0.00", "expense_n": 0,
                                                       "income": "0.00", "neutral": "0.00"}))
                    return
                if path == "/api/detail":
                    # 报表「点开看具体是哪几笔」用。汇总数字对不上时，
                    # 唯一能查的办法就是看明细 —— 以前这个页面做不到。
                    month = query.get("month", [""])[0]
                    self._json(console.quick(
                        lambda: detail_data(vault, month),
                        fallback={"month": month, "by_category": {},
                                  "months": [], "truncated": False,
                                  "n": 0, "expense_total": "0.00"}))
                    return
                if path == "/api/unmatched":
                    mask = query.get("mask", ["0"])[0] == "1"
                    month = query.get("month", [""])[0]
                    self._json(console.quick(
                        lambda: unmatched_data(vault, month, mask),
                        fallback={"month": month, "mask": mask,
                                  "totals": {"n": 0, "merchants": 0, "expense": "0.00",
                                             "income": "0.00", "expense_all": "0.00", "share": 0.0},
                                  "rows": []}))
                    return
                if path == "/api/classify":
                    mask = query.get("mask", ["0"])[0] == "1"
                    month = query.get("month", [""])[0]
                    self._json(console.quick(
                        lambda: classify_data(vault, month, mask),
                        fallback={"month": month, "mask": mask, "total": 0,
                                  "platform": [], "status": []}))
                    return
                if path == "/api/job":
                    job = console.get_job(query.get("id", [""])[0])
                    if job is None:
                        self._plain("no such job", 404)
                        return
                    self._json(job)
                    return
            except Exception as exc:
                self._plain(f"{type(exc).__name__}: {exc}", 500)
                return
            self._plain("not found", 404)

        # ---------- POST ---------- #
        def do_POST(self) -> None:      # noqa: N802
            from urllib.parse import urlparse
            if not self._authorised():
                self._plain("forbidden", 403)
                return
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._plain("bad json", 400)
                return

            if path == "/api/run":
                try:
                    job = console.submit(str(payload.get("action") or ""),
                                         payload.get("params") or {})
                except (ValueError, RuntimeError) as exc:
                    self._plain(str(exc), 409 if isinstance(exc, RuntimeError) else 400)
                    return
                self._json({"job": job["id"]})
                return

            if path == "/api/open_inbox":
                # 只允许打开投递目录，不接受任意路径 ——
                # 否则这个接口就成了「让本机服务启动任意东西」的口子。
                try:
                    folder = monthly.ensure_inbox(vault)
                    if hasattr(os, "startfile"):
                        os.startfile(folder)          # noqa: S606 - Windows 专用
                    self._json({"ok": True, "path": folder})
                except Exception as exc:
                    self._plain(f"{type(exc).__name__}: {exc}", 500)
                return

            self._plain("not found", 404)

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd, console, token


def preflight() -> list:
    """启动前自检。返回中文警告列表，由调用方打印。

    为什么放在 Python 里而不是启动器脚本里：
      * 这里有完整的中文能力（Python 在 Windows 上用宽字符 API 写控制台，
        不受代码页影响），而 .cmd 里写中文会踩 cmd.exe 的 UTF-8 指针漂移坑
        （见 MyLedger.cmd 顶部注释）。
      * 只检查「缺了会让某些功能直接不可用」的东西，缺什么直说缺什么。
    """
    warns = []
    if sys.version_info < (3, 11) or not hasattr(sqlite3.Connection, "serialize"):
        warns.append(
            "Python 版本过低：加密账本依赖 sqlite3 的 serialize()/deserialize()，需要 3.11+。"
            f"当前是 {sys.version.split()[0]}。"
        )
    try:
        from .bank_pdf import _require_pymupdf
        _require_pymupdf()
    except Exception:
        warns.append(
            "这个解释器里没有 PDF 解析库（PyMuPDF）：带密码的银行 PDF 账单会导不进来，"
            "其它功能不受影响。"
        )
    if not is_on_ac():
        warns.append(
            "当前没插电，电源状态：" + describe() + "。"
            "这里的操作都不吃 CPU，照常用；只有「用本地 AI 补分类」会跳过。"
        )
    return warns


def run(vault: str, *, port: int = DEFAULT_PORT, open_browser: bool = True,
        host: str = "127.0.0.1") -> int:
    """起网页控制台，阻塞到 Ctrl+C / 关掉 cmd 窗口。"""
    try:
        httpd, console, token = build_server(vault, port=port, host=host)
    except OSError as exc:
        print(f"端口 {port} 起不来（{exc}）。换一个：python -m importer web --port 8899")
        return 2

    actual = httpd.server_address[1]
    url = f"http://{host}:{actual}/?t={token}"
    lines = [
        "=" * 72,
        "MyLedger 网页控制台",
        f"  地址   :   {url}",
        f"  保管库 :   {vault}",
        f"  解释器 :   {sys.executable}",
        "  只监听本机 127.0.0.1，不联网，页面不引任何外部资源。",
        "  浏览器会自动打开；这个窗口不要关，关掉就等于停止服务。",
    ]
    for warn in preflight():
        lines.append(f"  ！{warn}")
    lines.append("=" * 72)
    # 一次打印并强制 flush。
    # 不加 flush 的话，输出被重定向到文件时是块缓冲，这段开机信息会一直卡在
    # 缓冲区里 —— 直到进程退出才出现，等于没有。
    print("\n".join(lines), flush=True)

    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        console.stop()
        httpd.server_close()
    return 0
