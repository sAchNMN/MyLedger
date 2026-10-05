"""命令行入口。

    python -m importer web                     本地网页控制台（推荐日常使用）
    python -m importer monthly                 月度批处理（命令行版）
    python -m importer review --ui             疑似重复确认（两个大按钮）
    python -m importer import <文件...>         单文件导入
    python -m importer report [--month 2026-09] 消费汇总
    python -m importer unmatched               盘点未分类交易（按商户聚合）
    python -m importer classify                诊断分类与状态判定（平台分类/状态认出了什么）
    python -m importer setcat <商户> <分类>     手工指定分类（优先级最高）
    python -m importer setsrc <形状> <渠道>      记住「某形状的文件是哪个渠道」
    python -m importer recompute               按当前规则修正历史数据（默认只试算）
    python -m importer store                   查看账本存储状态
    python -m importer sources                 各渠道导出方法
    python -m importer ocr <图片...>           银行截图 → TSV（本地 OCR）

账本默认落在仓库之外的 vault 里，并且是**加密存储**（见 secure_store.py）：
运行时整个库在内存里，退出时序列化 + DPAPI 加密落盘，明文一秒都不碰硬盘。

zip 解压密码不保存：导入时提示输入一次，用完即丢。
"""

from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys

from . import (
    __version__,
    fixups,
    monthly,
    platformmap,
    review_ui,
    secure_store,
    sourcehint,
    suggest,
    webui,
)
from .categorize import (
    CATEGORIES,
    DEFAULT_AI_MODEL,
    DEFAULT_AI_URL,
    clear_manual,
    manual_map,
    set_manual,
)
from .model import SOURCE_LABELS, STATUS_KILLS_EXPENSE, status_verdict
from .pipeline import import_file
from .power import require_ac
from .render import mask_merchant, money, print_month, print_overview, table

DEFAULT_VAULT = os.path.join(os.path.expanduser("~"), "MyLedgerVault")

# cmd_classify 要按 source 显示中文名，而 SOURCE_LABELS 只覆盖了 txn 里的
# 三个来源（缺 bank_pdf 的别名差异）。这里用 webui 导出的全量表。
SOURCE_LABELS_ALL = {**SOURCE_LABELS, "bank_pdf": "银行卡PDF"}


# --------------------------------------------------------------------------
# 公共：打开加密账本
# --------------------------------------------------------------------------

def _vault(args) -> str:
    return getattr(args, "vault", None) or DEFAULT_VAULT


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------

def cmd_import(args) -> int:
    vault = _vault(args)
    password = args.password
    # 哪些渠道要问密码：凡是账单文件可能带密码的都问。
    # 之前只问 alipay/wechat，漏了 bank_pdf —— 而银行的加密 PDF 才是
    # 最常见的那一种，漏掉它等于「命令行没法导银行账单」。
    if password is None and args.source in ("alipay", "wechat", "bank_pdf") and sys.stdin.isatty():
        # 只提示一次，用完即丢；不传 --password 且是交互式时不硬要，
        # 因为文件可能已经解压过了。
        try:
            label = SOURCE_LABELS.get(args.source, args.source)
            kind = "PDF" if args.source == "bank_pdf" else "压缩包"
            password = getpass.getpass(
                f"{label}账单的{kind}密码（不回显，用完即忘，不保存；直接回车跳过）："
            ) or None
        except (KeyboardInterrupt, EOFError):
            print()

    outcomes = []
    with secure_store.open_encrypted(vault, save_on_exit=not args.dry_run) as ledger:
        for path in args.files:
            print("=" * 72)
            print(f"源文件：{path}")
            outcomes.append(import_file(
                path, ledger,
                source=args.source, password=password,
                use_ai=args.ai, ignore_power=args.ignore_power,
                archive=args.archive, dry_run=args.dry_run,
            ))

    # 三种结局分开对待（ML-006）。
    #
    # 「部分失败」不等于「没导入成功」：库里已经有几笔了。说成没导入，
    # 人会以为白跑一趟，然后重新导一遍、或者干脆把源文件删了 —— 而那份文件
    # 正是失败那几笔唯一的重试依据。两种情况的退出码都是非零（上游要报警），
    # 但文案必须让人一眼分出「什么都没进去」和「进去了一半」。
    partial = [o for o in outcomes if o.status == "partial_failed"]
    bad = [o for o in outcomes if not o.ok and o.status != "partial_failed"]
    suspects = sum(o.suspect for o in outcomes if o.ok)

    print("=" * 72)
    if partial:
        print(f"有 {len(partial)} 个文件只导入了一部分（源文件仍在原处）：")
        for o in partial:
            print(f"  {o.path}")
            print(f"    {o.error}")
            print(f"    已入库 {o.inserted + o.identical} 笔，失败 {len(o.failed_rows)} 笔。")
            print("    补完问题后重跑同一个文件即可：已入库的会按单号/指纹跳过。")
    if bad:
        print(f"有 {len(bad)} 个文件没导入成功：")
        for o in bad:
            print(f"  {o.path}")
            print(f"    {o.status_label}：{o.error}")
    if suspects:
        print(f"有 {suspects} 笔疑似重复，用大按钮界面确认最快：")
        print("  python -m importer review --ui")
    print(f"本次共写入 {sum(o.inserted for o in outcomes)} 笔。")
    if partial:
        print(f"！这 {len(partial)} 个文件没有全部导入成功，不要把源文件删掉或挪走 ——")
        print("  它们是失败那几笔唯一的重试依据。")
    return 2 if (bad or partial) else 0


# --------------------------------------------------------------------------
# monthly
# --------------------------------------------------------------------------

def cmd_monthly(args) -> int:
    return monthly.run(
        _vault(args),
        use_ai=args.ai, ignore_power=args.ignore_power,
        keep=args.keep, dry_run=args.dry_run,
        auto_review=not args.no_review,
        log_path=args.log_file,
    )


# --------------------------------------------------------------------------
# recompute —— 按当前规则修正历史数据（默认只试算）
# --------------------------------------------------------------------------

def cmd_batches(args) -> int:
    """列出导入批次。查「哪一批是坏的」的入口。"""
    vault = _vault(args)
    with secure_store.open_encrypted(vault, save_on_exit=False) as ledger:
        rows = ledger.conn.execute(
            "SELECT b.batch_id, b.source, b.imported_at, b.parsed, b.inserted, "
            "       b.skipped, b.status, b.path, "
            "       (SELECT COUNT(*) FROM txn t WHERE t.batch_id = b.batch_id) AS live "
            "FROM batch b ORDER BY b.imported_at").fetchall()
        if not rows:
            print("还没有任何导入批次。")
            return 0
        print(f"共 {len(rows)} 个导入批次：")
        print()
        body = []
        for r in rows:
            body.append([
                r["batch_id"],
                r["imported_at"],
                r["source"],
                f"{r['inserted']}/{r['parsed']}",
                r["live"],
                os.path.basename(r["path"] or ""),
            ])
        print(table(
            ["批次", "导入时间", "来源", "写入/解析", "库中", "文件"],
            body,
            ["left", "left", "left", "right", "right", "left"],
        ))
        print()
        print("「写入/解析」两列不等 = 那一批有行没写进去（部分失败）。")
        print("「库中」少于「写入」= 有交易被后来删过或标成重复。")
        print("删掉一整批：python -m importer drop-batch <批次号> --apply")
    return 0


def cmd_drop_batch(args) -> int:
    """删掉一个导入批次的全部交易（解析器有 bug 时的补救路）。"""
    vault = _vault(args)
    enc = secure_store.encrypted_path(vault)

    if args.apply:
        if os.path.exists(enc):
            fixups.backup(enc)
        with secure_store.open_encrypted(vault, save_on_exit=True) as ledger:
            fixups.drop_batch(ledger, args.batch_id, apply=True)
    else:
        with secure_store.open_encrypted(vault, save_on_exit=False) as ledger:
            fixups.drop_batch(ledger, args.batch_id, apply=False)
    return 0


def cmd_fix_source(args) -> int:
    """把 txn.source 修正成「以 batch.source 为准」（历史数据修正）。"""
    vault = _vault(args)
    enc = secure_store.encrypted_path(vault)

    if args.apply:
        if os.path.exists(enc):
            fixups.backup(enc)
        with secure_store.open_encrypted(vault, save_on_exit=True) as ledger:
            fixups.fix_source_labels(ledger, apply=True)
    else:
        with secure_store.open_encrypted(vault, save_on_exit=False) as ledger:
            fixups.fix_source_labels(ledger, apply=False)
    return 0


# --------------------------------------------------------------------------

def cmd_recompute(args) -> int:
    vault = _vault(args)
    enc = secure_store.encrypted_path(vault)

    if args.apply:
        if os.path.exists(enc):
            fixups.backup(enc)
        with secure_store.open_encrypted(vault, save_on_exit=True) as ledger:
            fixups.recompute(ledger, apply=True)
            if args.strip_raw:
                fixups.strip_raw(ledger, apply=True)
    else:
        # 试算：只读内存，退出时不写回
        with secure_store.open_encrypted(vault, save_on_exit=False) as ledger:
            fixups.recompute(ledger, apply=False)
            if args.strip_raw:
                fixups.strip_raw(ledger, apply=False)
    return 0


# --------------------------------------------------------------------------
# unmatched —— 未分类盘点
# --------------------------------------------------------------------------

def cmd_unmatched(args) -> int:
    """列出没被规则认出来的商户，按涉及笔数排序。

    存在的理由：规则靠关键词，总会漏。要补分类就得先看清漏的是哪些商户、
    各占多少钱 —— 盲加关键词只会把「话费充值」这类真实消费吸到错误的桶里。
    """
    vault = _vault(args)
    with secure_store.open_encrypted(vault, save_on_exit=False) as ledger:
        totals = ledger.unmatched_totals(args.month)
        rows = ledger.unmatched_merchants(args.month, limit=args.limit)

        scope = args.month or "全部时间"
        print("=" * 72)
        print(f"未分类盘点 · {scope}")
        print(f"  未分类交易 : {totals['n']} 笔，涉及 {totals['merchants']} 个商户")
        print(f"  其中支出   : {money(totals['expense'])} 元")
        if totals["income"]:
            print(f"  其中收入   : {money(totals['income'])} 元")
        if totals["expense_all"]:
            share = totals["expense"] / totals["expense_all"] * 100
            print(f"  占同期支出 : {share:.1f}%"
                  f"（同期支出合计 {money(totals['expense_all'])} 元）")

        if not rows:
            print("\n没有未分类的交易 —— 规则覆盖得不错。")
            return 0

        print()
        body = []
        for r in rows:
            name = mask_merchant(r["merchant"]) if args.mask else (r["merchant"] or "（空）")
            body.append([name, r["n"], money(r["expense"]), money(r["income"])])
        print(table(["商户", "笔数", "支出(元)", "收入(元)"], body,
                    ["left", "right", "right", "right"]))

        print()
        print("归位方式（不写代码、不改规则表，按商户精确生效）：")
        print('  python -m importer setcat "商户名" 餐饮 --apply')
        print("可用分类：" + "、".join(CATEGORIES))
        if args.mask:
            print("（--mask：商户名已遮蔽，这份输出可以安全外发）")
        return 0


# --------------------------------------------------------------------------
# classify —— 分类判定诊断
# --------------------------------------------------------------------------

def cmd_classify(args) -> int:
    """看清「每笔交易的分类和方向到底是怎么定下来的」。

    存在的理由：这套判定有四层（人工指定 / 平台分类 / 关键词规则 / 原值）
    加上一个状态判定，叠起来之后光看报表是看不出某一笔为什么那样的。
    这个命令把那层叠在一起的**实际效果**摊开：

      · 平台分类认出了多少笔、分别是哪些平台类目
      · 哪些平台类目**还没被映射**（映射表该补了 —— 这是迭代映射表的唯一入口）
      · 状态压掉了多少笔「没真花出去」的支出
      · 状态值里有哪些是本工具还认不出来的（认不出 = 判定不了 = 需要补）

    只读，不改任何数据。
    """
    vault = _vault(args)
    with secure_store.open_encrypted(vault, save_on_exit=False) as ledger:
        # WHERE 子句分开攒，最后统一拼。
        # 直接在字符串里插 WHERE 的话，无 --month 时会留下一个孤零零的
        # 「 AND platform_category != ''」—— SQLite 报的是
        # 「near AND: syntax error」，跟真正的原因隔着一整句，很难查。
        scope_sql = " WHERE substr(ts,1,7) = ?" if args.month else ""
        params = [args.month] if args.month else []

        total = ledger.conn.execute(
            f"SELECT COUNT(*) AS n FROM txn{scope_sql}", params).fetchone()["n"]

        rows = ledger.conn.execute(
            "SELECT source, platform_category, direction, COUNT(*) AS n,"
            " SUM(CAST(amount AS REAL)) AS total FROM txn"
            + scope_sql
            + (" AND " if scope_sql else " WHERE ")
            + "platform_category != ''"
            " GROUP BY source, platform_category, direction"
            " ORDER BY total DESC",
            params).fetchall()

        st_rows = ledger.conn.execute(
            "SELECT status, direction, COUNT(*) AS n,"
            " SUM(CAST(amount AS REAL)) AS total FROM txn"
            + scope_sql
            + (" AND " if scope_sql else " WHERE ")
            + "status != '' GROUP BY status, direction ORDER BY total DESC",
            params).fetchall()

        # 状态分组按方向拆开统计。
        #
        # 为什么必须拆：direction='neutral' 的记录有两种来源 ——
        #   a) 原本就是中性（余额宝转入、信用卡还款），压根没被状态碰过
        #   b) 原本是支出，被状态压成中性（交易关闭的订单、已退款的消费）
        # 只看状态维度的合计会把这两种混成一句话，读的人无从分辨。
        #
        # 有个信息在库里确实**已经丢了**：「一笔被压中性的记录原本是支出」
        # 这件事，方向被压过之后就再也看不出来（raw 里只有「收/支」列的值，
        # 能重推，但那是 recompute 的活，不该在只读诊断里现算）。
        # 所以这里只如实呈现分组，不去猜哪一笔属于 a、哪一笔属于 b。
        by_status: dict[tuple, dict] = {}
        for r in ledger.conn.execute(
                "SELECT status, direction, COUNT(*) AS n,"
                " SUM(CAST(amount AS REAL)) AS total FROM txn"
                + scope_sql
                + (" AND " if scope_sql else " WHERE ") + "status != ''"
                + " GROUP BY status, direction", params):
            verdict = status_verdict(r["status"] or "")
            key = (r["status"], verdict)
            slot = by_status.setdefault(
                key, {"expense": [0, 0.0], "income": [0, 0.0],
                      "neutral": [0, 0.0]})
            slot[r["direction"]] = [r["n"], float(r["total"] or 0)]

    scope = args.month or "全部时间"
    print("=" * 72)
    print(f"分类判定诊断 · {scope} · 共 {total} 笔")
    print()
    print("分类优先级：人工指定 > 平台分类 > 关键词规则 > 原值")
    print("方向判定：「收/支」列 → 交易状态（未成立/已退款的一律压成不计收支）")

    if not rows:
        print()
        print("库里还没有平台分类数据。")
        print("支付宝账单看「交易分类」列、微信看「交易类型」列 ——")
        print("这两个列只在**新版导出**里有，老账单可能没有，属正常。")
    else:
        print()
        print("=" * 72)
        print("平台分类")
        print("=" * 72)
        body = []
        unknown_total = 0
        for r in rows:
            value = r["platform_category"]
            hit = platformmap.platform_category(r["source"], value)
            body.append([
                SOURCE_LABELS_ALL.get(r["source"], r["source"]),
                value if not args.mask else mask_merchant(value),
                hit or "（未映射）",
                r["n"],
                money(r["total"]),
            ])
            if not hit and r["direction"] == "expense":
                unknown_total += r["total"]
        print(table(["来源", "平台分类", "判定为", "笔数", "金额(元)"], body,
                    ["left", "left", "left", "right", "right"]))
        if unknown_total:
            print()
            print(f"其中 {money(unknown_total)} 元的支出，平台分类没能直接认出来，")
            print("是靠关键词规则兜底的（微信「商户消费」属于这一类，属正常）。")
            print("如果看到明显该认却没认的值，往 importer/platformmap.py 里加一行即可，")
            print("改完跑 recompute 试算看效果。")

    if by_status:
        print()
        print("=" * 72)
        print("交易状态（决定这笔钱到底算不算花出去）")
        print("=" * 72)
        body = []
        unrecognised = []
        killed_expense_n = 0
        killed_expense_total = 0.0
        for (value, verdict), slot in sorted(
                by_status.items(), key=lambda kv: -sum(
                    s[1] for s in kv[1].values())):
            label = {
                "ok": "已确认完成",
                "refunded": "已退款",
                "failed": "未成立",
                "pending": "未完成",
                "unknown": "认不出（按原样保留）",
            }.get(verdict, verdict)
            exp_n, exp_total = slot["expense"]
            inc_n, inc_total = slot["income"]
            neu_n, neu_total = slot["neutral"]
            if verdict in STATUS_KILLS_EXPENSE and neu_n:
                # 落在压支出的状态里、且现在不在支出方向 = 一定没被算进支出。
                # 但「它原本是不是支出」在库里已经**查不回来了**（方向压过之后
                # 这个事实就消失了）。所以这里只给一个不误导的合计，
                # 不去区分「被状态剔掉」和「本来就是中性」——
                # 那两种的分法在 raw 里有线索，但那是 recompute 的活。
                killed_expense_n += neu_n
                killed_expense_total += neu_total
            parts = []
            if exp_n:
                parts.append(f"支出 {exp_n} 笔 / {money(exp_total)}")
            if inc_n:
                parts.append(f"收入 {inc_n} 笔 / {money(inc_total)}")
            if neu_n:
                parts.append(f"中性 {neu_n} 笔 / {money(neu_total)}")
            body.append([
                value if not args.mask else mask_merchant(value),
                label, "；".join(parts) or "（无）",
            ])
            if verdict == "unknown":
                unrecognised.append(value)
        print(table(["状态原文", "判定", "当前分布"], body))

        if killed_expense_n:
            print()
            print(f"落在「未成立 / 已退款 / 未完成」这些状态里的共 "
                  f"{killed_expense_n} 笔、{money(killed_expense_total)} 元，")
            print("它们全部不在支出里 —— 报表的总支出因此少了这一块。")
        print("（提示：表里标「中性」的那些，其中一部分在状态判定之前就已经是")
        print("  不计收支了，比如余额宝转入、信用卡还款 —— 它们不是被状态剔掉的。）")
        if unrecognised:
            print()
            print("这些状态值本工具还认不出来，一律按原样保留（宁可放过也不误伤）：")
            for value in unrecognised:
                print(f"  {value if not args.mask else mask_merchant(value)}")
            print("确认语义后加进 importer/model.py 的 _STATUS_* 词表。")

    print()
    print("想重新按当前规则算一遍（默认只试算，加 --apply 才写库）：")
    print("  python -m importer recompute")
    if args.mask:
        print("（--mask：商户名与状态原文已遮蔽，这份输出可以安全外发）")
    return 0


# --------------------------------------------------------------------------
# setcat —— 手工指定分类
# --------------------------------------------------------------------------

def cmd_setcat(args) -> int:
    """人工指定商户分类。优先级高于规则与本地 AI。

    两段效果，分开说清楚：
      1. 写「商户 → 分类」缓存表 —— 总是写、总是持久化。新导入的交易下次自动生效。
         这一步只动一行记录，撤销成本几乎为零（--clear）。
      2. 改已经入库的历史交易 —— **只有加 --apply 才会做**，而且做之前先备份密文。
         因为这一步是批量改写，是全流程里少数几个真会动数据的地方。
    """
    vault = _vault(args)

    if args.list:
        with secure_store.open_encrypted(vault, save_on_exit=False) as ledger:
            mapping = manual_map(ledger)
        if not mapping:
            print("还没有人工指定的分类。")
            return 0
        print(f"人工指定的商户共 {len(mapping)} 个（优先级高于规则）：")
        for merchant in sorted(mapping):
            print(f"  {mapping[merchant]:<8} {merchant}")
        return 0

    merchant = args.merchant
    if not merchant:
        print("缺少商户名。用法：setcat <商户名> <分类>，或 setcat --list。")
        return 2

    if args.clear and args.category:
        print("--clear 表示撤销人工指定，不能同时给分类。")
        return 2
    if not args.clear:
        if not args.category:
            print("缺少分类名。可用分类：" + "、".join(CATEGORIES))
            return 2
        if args.category not in CATEGORIES:
            print(f"不认识的分类「{args.category}」。可用分类：" + "、".join(CATEGORIES))
            return 2

    # 改历史交易前先备份密文。备份失败就不动手。
    if args.apply:
        enc = secure_store.encrypted_path(vault)
        if os.path.exists(enc):
            fixups.backup(enc)

    with secure_store.open_encrypted(vault, save_on_exit=True) as ledger:
        if args.clear:
            removed = clear_manual(ledger, merchant)
            print(f"已撤销「{merchant}」的人工指定，退回规则判定。"
                  if removed else f"「{merchant}」本来就没有人工指定。")
            category = None
        else:
            key = set_manual(ledger, merchant, args.category)
            print(f"已记下缓存：{key} → {args.category}（下次导入这个商户自动用它）")
            category = args.category

        fixups.apply_manual(ledger, merchant, category, apply=args.apply)

    if not args.apply:
        print("已入库的历史交易**未改动**。确认上面的试算结果后，加 --apply 执行。")
    return 0


# --------------------------------------------------------------------------
# web —— 本地网页控制台
# --------------------------------------------------------------------------

def cmd_web(args) -> int:
    """打开本地网页控制台。日常使用推荐这个，不用记命令。"""
    return webui.run(_vault(args), port=args.port, open_browser=not args.no_browser)


# --------------------------------------------------------------------------
# store —— 存储状态
# --------------------------------------------------------------------------

def cmd_store(args) -> int:
    vault = _vault(args)
    info = secure_store.status(vault)

    print("=" * 72)
    print(f"vault 目录 : {info['vault']}")
    if info["encrypted_exists"]:
        print(f"加密账本   : 存在，{info['encrypted_size'] / 1024:.1f} KB")
        print(f"             {info['encrypted_path']}")
    else:
        print("加密账本   : 还没创建（第一次导入时会自动创建）")
    print("加密方式   : Windows DPAPI（CryptProtectData），绑定当前 Windows 用户")
    print("             运行时整个库在内存，退出时加密落盘，明文不碰硬盘")

    residue = secure_store.find_residue(vault)
    if residue:
        print()
        print("！还有明文副本，账本等于没加密：")
        for path in residue:
            print(f"    {path}")
        if info["plaintext_exists"]:
            print("  下次打开账本时会自动迁移并校验，"
                  "然后把明文移入 _plaintext_quarantine（移动，不删除）。")
            print("  迁移失败时明文会原样保留，不会被挪走。")
        print("  先确认加密账本能正常打开，再删掉上面这些路径：")
        print("    Remove-Item -LiteralPath '<上面列的路径>' -Recurse -Force")

    if info["encrypted_exists"]:
        print()
        print("提示：这个库的密钥绑定你的 Windows 用户账户。重装系统或换账户后解不开。")
        print("      数据不会丢——vault\\raw\\ 里的原始账单还在，重新导一遍即可。")
    return 0


# --------------------------------------------------------------------------
# review
# --------------------------------------------------------------------------

def cmd_review(args) -> int:
    vault = _vault(args)
    with secure_store.open_encrypted(vault, save_on_exit=True) as ledger:
        if args.ui:
            return review_ui.run(ledger, port=args.port)
        return _review_terminal(ledger, args)


def _review_terminal(ledger, args) -> int:
    rows = ledger.list_review(limit=args.limit)
    if not rows:
        print("没有待确认的疑似重复。")
        return 0

    print(f"共 {len(rows)} 笔疑似重复。")
    print("提示：加 --ui 换成大按钮界面，比在这里读数快得多。\n")
    for row in rows:
        other = ledger.review_counterpart(row)
        print("=" * 72)
        print(f"#{row['id']}  {SOURCE_LABELS.get(row['source'], row['source'])}  "
              f"{row['ts']}  {money(row['amount'])} 元")
        print(f"  交易对方 : {row['counterparty']}")
        print(f"  商品     : {row['item']}")
        print(f"  单号     : {row['txn_id'] or '（无）'}")
        print(f"  判定依据 : {row['review_note']}")
        if other:
            print("  ── 与之相似的另一笔 ──")
            print(f"  #{other['id']}  {SOURCE_LABELS.get(other['source'], other['source'])}  "
                  f"{other['ts']}  {money(other['amount'])} 元")
            print(f"  交易对方 : {other['counterparty']}")
            print(f"  单号     : {other['txn_id'] or '（无）'}")
        print(f"  处置     : review --resolve {row['id']} --as dup   # 确是同一笔")
        print(f"             review --resolve {row['id']} --as ok    # 是两笔独立交易")

    if args.resolve:
        target = ledger.conn.execute(
            "SELECT * FROM txn WHERE id = ?", (args.resolve,)
        ).fetchone()
        if not target:
            print(f"\n找不到 #{args.resolve}，没有任何改动。")
            return 2

        # 改动前先把对象打出来。裁定是人工决定，但至少要让人看清自己改的是哪一条——
        # 打错一个 id 就把一笔真实消费从报表里抹掉，这种错不留痕迹最难查。
        print()
        print(f"将要处置 #{target['id']}：{target['ts']} {money(target['amount'])} 元 "
              f"{target['counterparty']}（当前状态：{target['review'] or '正常'}）")
        if not target["review"].startswith("suspect"):
            print("  注意：这条不是「疑似重复」，把它标记为重复会直接从统计里剔除。")

        decision = "confirmed_dup" if args.as_ == "dup" else "confirmed_ok"
        if ledger.resolve(args.resolve, decision):
            print(f"#{args.resolve} 已标记为 {decision}。")
            return 0
        print("\n处置失败。")
        return 2
    return 0


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def cmd_report(args) -> int:
    vault = _vault(args)
    with secure_store.open_encrypted(vault, save_on_exit=False) as ledger:
        print_overview(ledger, ledger.db_path)
        if args.month:
            print_month(ledger, args.month)
        else:
            for row in ledger.conn.execute(
                    "SELECT DISTINCT substr(ts,1,7) AS m FROM txn ORDER BY m"):
                print_month(ledger, row["m"])
    return 0


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

def cmd_sources(args) -> int:
    print("""MyLedger 支持的账单来源（全部本地解析，不联网）

支付宝
  手机：支付宝 → 我的 → 账单 → 右上角「…」→ 开具交易流水证明 → 用于个人对账
  下载得到的是【带密码的 zip】，密码是你在申请时设置的。

微信
  手机：微信 → 我 → 服务 → 钱包 → 账单 → 右上角「常见问题」→ 下载账单
        → 用于个人对账 → 选时间范围 → 填邮箱
  账单会以【带密码的 zip】发到你的邮箱，密码在微信里单独给你。

银行卡
  银行 APP 没有导出功能，只能截图。截图要求：
    - 用「交易明细」页，不要用首页缩略列表（那条没有时间/对方字段）
    - 一屏一张图，不要把一笔交易截断在两屏之间
    - 每笔至少能看到：时间、交易对方、金额、收支方向
  处理：python -m importer ocr 截图1.png 截图2.png

关于密码
  本程序**不保存**你的 zip 解压密码。
  交互式运行时（双击 monthly.cmd）每个渠道提示输入一次，用完即丢。
  后台自动运行没法输入密码，所以只能处理你已经解压好的 csv/xlsx。

推荐流程
  1. 三个渠道的文件都丢进 %USERPROFILE%\\MyLedgerVault\\inbox\\
  2. 双击 scripts\\monthly.cmd（有加密 zip 的话会提示输密码）
  3. python -m importer review --ui    ← 两个大按钮，快速点完

验证过的格式（本机实测）
  支付宝 CSV : UTF-8 BOM，24 行说明后是表头
               交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,
               收/付款方式,交易状态,交易订单号,商家订单号,备注
  微信 XLSX  : 14 行说明后是表头
               交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,
               当前状态,交易单号,商户单号,备注
  两者的说明行数会随版本变化，所以导入器不写死行号，而是按关键词找表头行。
""")
    return 0


# --------------------------------------------------------------------------
# suggest（建议分类，人来拍板）
# --------------------------------------------------------------------------

def cmd_suggest(args) -> int:
    """给未分类商户一个建议，由你逐条拍板。

    这不是自动分类。建议只是把「库里已经有的信息」搬到你眼前，
    决定权在你手上 —— 少的是打字次数，不是判断次数。
    """
    vault = _vault(args)
    use_ai = bool(args.ai)

    if use_ai and not require_ac("本地 AI 建议分类", override=args.ignore_power):
        return 1

    with secure_store.open_encrypted(vault, save_on_exit=False) as ledger:
        items = suggest.with_ai(ledger, use_ai=use_ai,
                                ai_url=args.ai_url, ai_model=args.ai_model)

    if not items:
        print("没有未分类的交易，规则覆盖得不错。")
        return 0

    total = sum(i.expense + i.income for i in items)
    print("=" * 70)
    print(f"未分类商户 {len(items)} 个，涉及 {money(total)}")
    print("=" * 70)

    if args.list:
        for index, item in enumerate(items, 1):
            tag = f"{item.category}（{item.evidence}）" if item.category else "没有建议"
            print(f"{index:>3}. {item.merchant:<24} {item.count:>3} 笔  {tag}")
            if item.sample_item:
                print(f"      商品：{item.sample_item}")
        print()
        print("逐条拍板：python -m importer suggest")
        return 0

    print()
    print("可用分类（输入编号即可）：")
    for index, name in enumerate(CATEGORIES, 1):
        print(f"  {index:>2}. {name}", end="\n" if index % 6 == 0 else "")
    print()
    print("  回车 = 采纳建议   编号 = 换成这个分类   s = 跳过   q = 退出")
    print()

    accepted: list[tuple[str, str]] = []
    for index, item in enumerate(items, 1):
        amount = money(item.expense or item.income)
        print(f"[{index}/{len(items)}] {item.merchant}  {item.count} 笔  {amount}")
        if item.sample_item:
            print(f"      商品：{item.sample_item}")
        if item.category:
            print(f"      建议：{item.category}  ← {item.evidence}")
        else:
            print("      没有建议（想不出依据就不猜）")
        try:
            answer = input("      你的决定 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n（中断了，已采纳的会写下去）")
            break

        if answer.lower() in ("q", "quit", "exit"):
            break
        if answer.lower() in ("s", "", "skip") and not item.category:
            continue
        if answer == "":
            accepted.append((item.merchant, item.category))     # 采纳建议
            continue
        if answer.lower() in ("s", "skip"):
            continue
        try:
            chosen = CATEGORIES[int(answer) - 1]
        except (ValueError, IndexError):
            print("      看不懂这个输入，跳过。")
            continue
        accepted.append((item.merchant, chosen))

    if not accepted:
        print()
        print("一条都没采纳，账本没动。")
        return 0

    if not args.apply:
        print()
        print(f"试算：将采纳 {len(accepted)} 条（加 --apply 才真的写库）")
        for merchant, category in accepted:
            print(f"  {category:<8} {merchant}")
        return 0

    enc = secure_store.encrypted_path(vault)
    if os.path.exists(enc):
        fixups.backup(enc)
    with secure_store.open_encrypted(vault, save_on_exit=True) as ledger:
        for merchant, category in accepted:
            set_manual(ledger, merchant, category)
            fixups.apply_manual(ledger, merchant, category, apply=True)
    print()
    print(f"已记下 {len(accepted)} 条，并回填到历史交易。")
    print("撤销某一条：python -m importer setcat \"商户名\" --clear --apply")
    return 0


# --------------------------------------------------------------------------
# ocr
# --------------------------------------------------------------------------

def cmd_ocr(args) -> int:
    """调用 scripts/bank-ocr.ps1 做本地 OCR。

    用 Windows 自带的 OCR 引擎（Windows.Media.Ocr），
    中文识别开箱可用，不装任何东西、不上传任何图片。
    """
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "scripts", "bank-ocr.ps1")
    if not os.path.isfile(script):
        print(f"找不到 OCR 脚本：{script}")
        return 2
    out_dir = args.out or os.path.join(DEFAULT_VAULT, "ocr")
    os.makedirs(out_dir, exist_ok=True)
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", script, "-OutDir", out_dir] + args.images
    print("调用本地 OCR（Windows 自带引擎，不联网）……")
    code = subprocess.call(cmd)

    # 顺手给旧产物一个期限（ML-011）。放在 OCR 之后是因为：清理的是**之前**
    # 留下的文件，当次的产物还热着，不会被自己清掉 —— 也不该被清掉，
    # 那正是接下来要导入的东西。
    try:
        fixups.cleanup_ocr_artifacts(out_dir, keep_days=args.cleanup_days)
    except Exception as exc:                 # noqa: BLE001 - 善后不许反过来搞砸主流程
        print(f"  （OCR 产物清理跳过：{type(exc).__name__}）")
    return code


# --------------------------------------------------------------------------
# setsrc（渠道记忆）
# --------------------------------------------------------------------------

def cmd_setsrc(args) -> int:
    """记住「某个形状的文件是哪个渠道」，以后同类文件自动认。

    键是形状而不是文件名：银行账单文件名是每月都变的流水号，
    按文件名记等于每个月都要重指定一次，那就没有「记住」的意义了。
    形状由 sourcehint.shape_key() 算：ext:csv / zip:pdf / name:某个文件
    """
    vault = _vault(args)
    choices = list(sourcehint.KNOWN_SOURCES)

    if args.of:
        path = args.of
        if not os.path.isfile(path):
            print(f"找不到文件：{path}")
            return 2
        key = sourcehint.shape_key(path)
        print(f"{key}")
        print(f"记成某个渠道：python -m importer setsrc {key} <{'|'.join(choices)}>")
        return 0

    if args.list:
        with secure_store.open_encrypted(vault, save_on_exit=False) as ledger:
            hints = sourcehint.hint_map(ledger)
        if not hints:
            print("还没有记住任何渠道。识别不出来时用：")
            print("  python -m importer setsrc <形状> <渠道>")
            return 0
        print("已记住的渠道：")
        for key, source in sorted(hints.items()):
            print(f"  {key:34s} → {SOURCE_LABELS.get(source, source)}")
        return 0

    if not args.key:
        print("用法：")
        print("  python -m importer setsrc --list                        列出已记住的")
        print("  python -m importer setsrc --of <文件路径>                看这个文件的形状键")
        print(f"  python -m importer setsrc <形状> <渠道>              渠道：{'|'.join(choices)}")
        print("  python -m importer setsrc <形状> --clear                撤销")
        return 2

    if args.clear:
        with secure_store.open_encrypted(vault, save_on_exit=True) as ledger:
            removed = sourcehint.clear_hint(ledger, args.key)
        print(f"已撤销：{args.key}" if removed else f"没有这条记忆：{args.key}")
        return 0 if removed else 2

    if not args.source:
        print(f"要记成哪个渠道？{'|'.join(choices)}")
        return 2

    with secure_store.open_encrypted(vault, save_on_exit=True) as ledger:
        key = sourcehint.set_hint(ledger, args.key, args.source)
    print(f"已记住：{key} → {SOURCE_LABELS.get(args.source, args.source)}")
    print("以后形状相同的文件会自动按这个渠道解析。")
    return 0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def _add_vault(parser):
    parser.add_argument("--vault", default=None, help=f"vault 目录（默认 {DEFAULT_VAULT}）")
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="importer",
        description="MyLedger 本地账单导入器（全程离线，账本加密存储）",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- monthly ----
    p = _add_vault(sub.add_parser("monthly", help="月度批处理：扫投递目录 → 导入 → 出月报"))
    p.add_argument("--ai", action="store_true", help="用本地模型补分类（默认关闭）")
    p.add_argument("--ignore-power", action="store_true")
    p.add_argument("--keep", action="store_true", help="处理完不移走源文件")
    p.add_argument("--no-review", action="store_true",
                   help="即使发现疑似重复也不要自动打开确认界面")
    p.add_argument("--dry-run", action="store_true", help="只解析不写库、不移文件")
    p.add_argument("--log-file", default=None,
                   help="把结构化审计日志追加到这里（只有计数和错误码，不含交易内容）")
    p.set_defaults(func=cmd_monthly)

    # ---- import ----
    p = _add_vault(sub.add_parser("import", help="导入单个账单文件"))
    p.add_argument("files", nargs="+", help="账单文件（支持 csv/xlsx/带密码的 zip）")
    p.add_argument("--source", default="auto",
                   choices=["auto", "alipay", "wechat", "bank", "bank_pdf"],
                   help="数据源，默认按文件名和表头自动识别")
    p.add_argument("--password", default=None,
                   help="压缩包密码。不传则在交互式终端里提示输入（用完即忘，不保存）")
    p.add_argument("--ai", action="store_true", help="用本地模型补分类")
    p.add_argument("--ignore-power", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-archive", dest="archive", action="store_false",
                   help="不把原始文件复制进 vault 归档")
    p.set_defaults(archive=True, func=cmd_import)

    # ---- batches ----
    p = _add_vault(sub.add_parser("batches", help="列出导入批次（查哪一批是坏的）"))
    p.set_defaults(func=cmd_batches)

    # ---- drop-batch ----
    p = _add_vault(sub.add_parser(
        "drop-batch",
        help="删掉一个批次的全部交易（解析器出 bug、库里已有错数据时用）"))
    p.add_argument("batch_id", help="批次号，先用 batches 查")
    p.add_argument("--apply", action="store_true",
                   help="真删（会先自动备份密文）。不加则只试算")
    p.set_defaults(func=cmd_drop_batch)

    # ---- fix-source ----
    p = _add_vault(sub.add_parser(
        "fix-source", help="把历史交易的 source 修正成以批次来源为准"))
    p.add_argument("--apply", action="store_true",
                   help="真改（会先自动备份密文）。不加则只试算")
    p.set_defaults(func=cmd_fix_source)

    # ---- review ----
    p = _add_vault(sub.add_parser("review", help="确认疑似重复"))
    p.add_argument("--ui", action="store_true",
                   help="打开本地网页界面，两个大按钮快速裁定（推荐）")
    p.add_argument("--port", type=int, default=0, help="--ui 的端口，0=自动分配")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--resolve", type=int, help="要处置的交易 id（终端模式）")
    p.add_argument("--as", dest="as_", choices=["dup", "ok"],
                   help="dup=确认重复, ok=独立交易")
    p.set_defaults(func=cmd_review)

    # ---- report ----
    p = _add_vault(sub.add_parser("report", help="消费汇总"))
    p.add_argument("--month", help="月份，形如 2026-09；不填则列出全部月份")
    p.set_defaults(func=cmd_report)

    # ---- store ----
    p = _add_vault(sub.add_parser("store", help="查看账本存储状态与加密情况"))
    p.set_defaults(func=cmd_store)

    # ---- recompute ----
    p = _add_vault(sub.add_parser(
        "recompute", help="按当前规则修正历史数据的分类与方向（默认只试算）"))
    p.add_argument("--apply", action="store_true",
                   help="真正写库（会先自动备份密文）。不加则只试算")
    p.add_argument("--strip-raw", action="store_true",
                   help="顺带把历史数据里含整行原文的 raw 压成白名单形态（同样默认只试算）")
    p.set_defaults(func=cmd_recompute)

    # ---- unmatched ----
    p = _add_vault(sub.add_parser("unmatched", help="盘点未分类交易（按商户聚合）"))
    p.add_argument("--month", help="只统计某个月，形如 2026-09")
    p.add_argument("--limit", type=int, default=200, help="最多列出多少个商户")
    p.add_argument("--mask", action="store_true",
                   help="商户名遮蔽后输出，便于把结果安全外发")
    p.set_defaults(func=cmd_unmatched)

    # ---- classify ----
    p = _add_vault(sub.add_parser(
        "classify", help="诊断分类与状态判定：平台分类认出了什么、状态压掉了什么"))
    p.add_argument("--month", help="只统计某个月，形如 2026-09")
    p.add_argument("--mask", action="store_true",
                   help="商户名与平台分类原文遮蔽后输出，便于安全外发")
    p.set_defaults(func=cmd_classify)

    # ---- setcat ----
    p = _add_vault(sub.add_parser(
        "setcat", help="手工指定某个商户的分类（优先级高于规则与本地 AI）"))
    p.add_argument("merchant", nargs="?", help="交易对方（商户名）")
    p.add_argument("category", nargs="?", help="分类名")
    p.add_argument("--clear", action="store_true", help="撤销该商户的人工指定，退回规则")
    p.add_argument("--list", action="store_true", help="列出全部人工指定的商户")
    p.add_argument("--apply", action="store_true",
                   help="同时改已入库的历史交易（会先备份密文）")
    p.set_defaults(func=cmd_setcat)

    # ---- setsrc ----
    p = _add_vault(sub.add_parser(
        "setsrc", help="记住「某形状的文件是哪个渠道」，以后自动认"))
    p.add_argument("key", nargs="?", help="形状键，如 ext:csv / zip:pdf")
    p.add_argument("source", nargs="?", choices=list(sourcehint.KNOWN_SOURCES),
                   help="渠道：alipay / wechat / bank_pdf / bank_ocr")
    p.add_argument("--list", action="store_true", help="列出全部已记住的渠道")
    p.add_argument("--clear", action="store_true", help="撤销一条记忆")
    p.add_argument("--of", default=None,
                   help="不记，只看这个文件的形状键是多少（排查用）")
    p.set_defaults(func=cmd_setsrc)

    # ---- web ----
    p = _add_vault(sub.add_parser(
        "web", help="打开本地网页控制台（不用记命令，日常推荐）"))
    p.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p.set_defaults(func=cmd_web)

    # ---- sources ----
    sub.add_parser("sources", help="各渠道怎么导出账单").set_defaults(func=cmd_sources)

    # ---- suggest ----
    p = _add_vault(sub.add_parser(
        "suggest",
        help="给未分类商户一个建议分类，你逐条拍板（不是自动分类）"))
    p.add_argument("--list", action="store_true", help="只列出建议，不进入逐条确认")
    p.add_argument("--apply", action="store_true", help="真的写库（默认只试算）")
    p.add_argument("--ai", action="store_true",
                   help="本地没找到依据时用本机模型补一个建议（需装 Ollama）")
    p.add_argument("--ai-url", default=DEFAULT_AI_URL, help="本地模型地址")
    p.add_argument("--ai-model", default=DEFAULT_AI_MODEL, help="本地模型名")
    p.add_argument("--ignore-power", action="store_true",
                   help="忽略电源闸门（未插电也跑 AI）")
    p.set_defaults(func=cmd_suggest)

    # ---- ocr ----
    p = sub.add_parser("ocr", help="银行截图 → TSV（调本地 Windows OCR）")
    p.add_argument("images", nargs="+")
    p.add_argument("--out", default=None)
    p.add_argument("--cleanup-days", type=int, default=fixups.OCR_KEEP_DAYS,
                   metavar="N",
                   help=f"跑完顺手把超过 N 天的 OCR 产物移出 vault"
                        f"（默认 {fixups.OCR_KEEP_DAYS} 天；0 表示不清理）")
    p.set_defaults(func=cmd_ocr)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
