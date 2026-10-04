"""命令行入口。

用法：
    python -m importer import <文件...>         导入账单（自动识别数据源）
    python -m importer review                   列出疑似重复，人工确认
    python -m importer report [--month 2026-09] 消费汇总
    python -m importer sources                  各渠道导出方法
    python -m importer ocr <图片...>            银行截图 → TSV（调用本地 OCR）

数据库默认落在仓库之外的 vault 里，跟加密容器在同一个安全区。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import unicodedata
from decimal import Decimal

from . import __version__
from .categorize import DEFAULT_CATEGORY, categorize
from .model import DIRECTION_EXPENSE, DIRECTION_INCOME, DIRECTION_NEUTRAL, SOURCE_LABELS
from .sources import PARSERS, detect_source
from .store import Ledger

DEFAULT_VAULT = os.path.join(os.path.expanduser("~"), "MyLedgerVault")
DEFAULT_DB = os.path.join(DEFAULT_VAULT, "ledger.db")


# --------------------------------------------------------------------------
# 输出辅助（中英文混排对齐）
# --------------------------------------------------------------------------

def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(text))


def _pad(text: str, width: int, align: str = "left") -> str:
    text = str(text)
    space = max(0, width - _width(text))
    if align == "right":
        return " " * space + text
    return text + " " * space


def table(headers: list[str], rows: list[list], aligns: list[str] | None = None) -> str:
    aligns = aligns or ["left"] * len(headers)
    widths = [_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _width(cell))
    out = ["  ".join(_pad(h, widths[i]) for i, h in enumerate(headers))]
    out.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        out.append("  ".join(_pad(c, widths[i], aligns[i]) for i, c in enumerate(row)))
    return "\n".join(out)


def money(value) -> str:
    return f"{Decimal(str(value)):,.2f}"


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------

def cmd_import(args) -> int:
    ledger = Ledger(args.db)
    exit_code = 0
    total_inserted = 0
    total_suspect = 0
    failed_files: list[str] = []

    for path in args.files:
        print("=" * 72)
        source = args.source if args.source != "auto" else detect_source(path)
        if source not in PARSERS:
            print(f"无法识别数据源：{path}")
            print("  用 --source alipay|wechat|bank 手动指定。")
            failed_files.append(path)
            exit_code = 2
            continue

        try:
            result = PARSERS[source](path, args.password)
        except Exception as exc:
            print(f"读取失败：{path}")
            print(f"  {type(exc).__name__}: {exc}")
            failed_files.append(path)
            exit_code = 2
            continue

        print(result.summary())

        # ------------------------------------------------------------------
        # 失败要响亮。
        #
        # 这条校验来自一次真实事故：一份账单导出文件只有说明和表头、
        # 没有任何流水，但文件开头明明白白写着「共 87 笔记录」。
        # 如果不校验，导入器会安安静静地写进一个空批次，
        # 你会在月底看到「本月零支出」——这种错比报错危险得多。
        # ------------------------------------------------------------------
        if not result.txns:
            print()
            print("导入中止：这份文件里一笔流水都没解析出来。")
            if result.declared_count:
                print(f"  但文件自称有 {result.declared_count} 笔记录。")
                print("  常见原因：导出文件不完整（只下到说明和表头）、"
                      "或下载的压缩包没解开。")
            for item in result.skipped[:5]:
                print(f"  跳过原因（第 {item.get('row')} 行）：{item.get('reason')}")
            print("  没有任何数据被写入数据库。")
            failed_files.append(path)
            exit_code = 2
            continue

        if result.declared_count is not None and len(result.txns) != result.declared_count:
            print(f"！注意：解析到 {len(result.txns)} 笔，文件自称 {result.declared_count} 笔，"
                  f"差 {result.declared_count - len(result.txns)} 笔。"
                  f"可能文件不完整，或有用不到的格式。")

        if result.skipped:
            print(f"\n跳过 {len(result.skipped)} 行：")
            for item in result.skipped[:10]:
                print(f"  第 {item.get('row')} 行：{item.get('reason')}")
            if len(result.skipped) > 10:
                print(f"  …另外 {len(result.skipped) - 10} 行，详见 --verbose")

        # 分类
        cat_stats = categorize(
            result.txns, ledger, use_ai=args.ai,
            ignore_power=args.ignore_power,
        )

        if args.dry_run:
            print("\n[dry-run] 不写库。分类结果预览：")
            for txn in result.txns[:15]:
                print(f"  {txn.ts:%m-%d %H:%M}  {money(txn.amount):>10}  "
                      f"{txn.category:<6}  {txn.counterparty[:24]}")
            if len(result.txns) > 15:
                print(f"  …共 {len(result.txns)} 笔")
            continue

        ledger.start_batch(source, path, origin="",
                           parsed=len(result.txns), declared=result.declared_count)

        inserted = identical = suspect = 0
        for txn in result.txns:
            verdict = ledger.add(txn)
            if verdict.is_duplicate_skip:
                identical += 1
            elif verdict.needs_review:
                suspect += 1
                inserted += 1
            else:
                inserted += 1

        ledger.finish_batch(len(result.txns), result.declared_count,
                            inserted, identical, suspect, len(result.skipped))
        total_inserted += inserted
        total_suspect += suspect

        print()
        print(f"写入 {inserted} 笔  重复跳过 {identical} 笔  疑似重复待确认 {suspect} 笔")
        print(f"分类：规则命中 {cat_stats['rules']}，缓存命中 {cat_stats['cache']}，"
              f"本地 AI {cat_stats['ai']}，仍未分类 {cat_stats['unmatched']}")
        print(f"批次号 {ledger.batch_id}")

        if args.archive:
            archived = archive_source(path, ledger, source)
            if archived:
                print(f"原始文件已归档到 {archived}（仓库外）")

    print("=" * 72)
    if failed_files:
        print(f"有 {len(failed_files)} 个文件没导入成功：")
        for path in failed_files:
            print(f"  {path}")
    if total_suspect:
        print(f"有 {total_suspect} 笔疑似重复，跑 `python -m importer review` 逐条确认。")
    print(f"本次共写入 {total_inserted} 笔。数据库：{args.db}")
    return exit_code


def archive_source(path: str, ledger: Ledger, source: str) -> str:
    """把原始账单文件复制进 vault 归档，便于日后重新解析。"""
    vault_raw = os.path.join(os.path.dirname(os.path.abspath(ledger.db_path)), "raw")
    os.makedirs(vault_raw, exist_ok=True)
    try:
        sha = ledger.conn.execute(
            "SELECT file_sha1 FROM batch WHERE batch_id = ?", (ledger.batch_id,)
        ).fetchone()["file_sha1"]
    except Exception:
        sha = ""
    base = os.path.basename(path)
    target = os.path.join(vault_raw, f"{source}_{(sha or 'x')[:12]}_{base}")
    if os.path.abspath(target) == os.path.abspath(path):
        return ""
    if not os.path.exists(target):
        shutil.copy2(path, target)
    return target


# --------------------------------------------------------------------------
# review
# --------------------------------------------------------------------------

def cmd_review(args) -> int:
    ledger = Ledger(args.db)
    rows = ledger.list_review(limit=args.limit)
    if not rows:
        print("没有待确认的疑似重复。")
        return 0

    print(f"共 {len(rows)} 笔疑似重复。逐条看，然后决定保留还是标记为重复。\n")
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
            print(f"  ── 与之相似的另一笔 ──")
            print(f"  #{other['id']}  {SOURCE_LABELS.get(other['source'], other['source'])}  "
                  f"{other['ts']}  {money(other['amount'])} 元")
            print(f"  交易对方 : {other['counterparty']}")
            print(f"  单号     : {other['txn_id'] or '（无）'}")
        print(f"  处置     : python -m importer review --resolve {row['id']} --as dup   # 确是同一笔")
        print(f"             python -m importer review --resolve {row['id']} --as ok    # 是两笔独立交易")

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
        print(f"\n处置失败。")
        return 2
    return 0


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def cmd_report(args) -> int:
    ledger = Ledger(args.db)
    overall = ledger.overall()
    txn = overall["txn"]

    print("=" * 72)
    print(f"账本概览    数据库 {args.db}")
    print(f"  交易总笔数 : {txn['n'] or 0}")
    print(f"  时间范围   : {txn['first_ts'] or '-'} ~ {txn['last_ts'] or '-'}")
    print(f"  导入批次   : {overall['batches']} 次")
    print(f"  支出总额   : {money(txn['expense'] or 0)} 元")
    if txn["suspect"]:
        print(f"  待确认重复 : {txn['suspect']} 笔（用 review 处理）")
    if txn["confirmed_dup"]:
        print(f"  已确认重复 : {txn['confirmed_dup']} 笔（已从统计中剔除）")

    month = args.month
    if month:
        print()
        summary = ledger.month_summary(month)
        if not summary["rows"]:
            print(f"{month} 没有数据。")
            return 0

        expense = [
            (row["category"], Decimal(str(row["total"] or 0)), row["n"])
            for row in summary["rows"] if row["direction"] == DIRECTION_EXPENSE
        ]
        income = [
            Decimal(str(row["total"] or 0))
            for row in summary["rows"] if row["direction"] == DIRECTION_INCOME
        ]
        neutral = [
            Decimal(str(row["total"] or 0))
            for row in summary["rows"] if row["direction"] == DIRECTION_NEUTRAL
        ]

        print(f"{month} 支出构成（已剔除确认重复）")
        if expense:
            expense.sort(key=lambda item: item[1], reverse=True)
            total = sum(item[1] for item in expense)
            rows = [[cat, money(amt), n] for cat, amt, n in expense]
            rows.append(["合计", money(total), sum(n for _, _, n in expense)])
            print(table(["分类", "金额(元)", "笔数"], rows, ["left", "right", "right"]))
            print(f"\n  支出合计 {money(total)} 元")
        else:
            print("  （本月没有支出）")

        if income:
            print(f"  收入合计 {money(sum(income))} 元")
        if neutral:
            print(f"  中性交易（充值/提现/还款等）合计 {money(sum(neutral))} 元，不计入消费")
    return 0


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

def cmd_sources(args) -> int:
    print(f"""MyLedger 支持的账单来源（全部本地解析，不联网）

支付宝
  手机：支付宝 → 我的 → 账单 → 右上角「…」→ 开具交易流水证明 → 用于个人对账
  下载得到的是【带密码的 zip】，密码是你在申请时设置的。
  导入：python -m importer import "路径\\支付宝交易明细(...).zip" --password 你的密码

微信
  手机：微信 → 我 → 服务 → 钱包 → 账单 → 右上角「常见问题」→ 下载账单
        → 用于个人对账 → 选时间范围 → 填邮箱
  账单会以【带密码的 zip】发到你的邮箱，密码在微信里单独给你。
  导入：python -m importer import "路径\\微信支付账单(...).zip" --password 收到的密码

银行卡
  银行 APP 没有导出功能，只能截图。截图要求：
    - 一屏一张图，包含：交易时间 / 交易对方 / 金额 / 收支方向
    - 尽量在同一张图里截完整的列表，别把一笔交易截断在两屏
    - 用银行 APP 的「交易明细」或「收支明细」页，不要用首页缩略列表
  处理：python -m importer ocr 截图1.png 截图2.png
        再导入生成的 TSV：python -m importer import out\\bank_ocr.tsv --source bank

验证过的格式（本机实测）
  支付宝 CSV : UTF-8 BOM，24 行说明后是表头
               交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,
               收/付款方式,交易状态,交易订单号,商家订单号,备注
  微信 XLSX  : 14 行说明后是表头，第 15 行是
               交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,
               当前状态,交易单号,商户单号,备注

  两者的说明行数会随版本变化，所以导入器不写死行号，而是按关键词找表头行。
""")
    return 0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="importer",
        description="MyLedger 本地账单导入器（全程离线）",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="导入账单文件")
    p_import.add_argument("files", nargs="+", help="账单文件路径（支持 csv/xlsx/带密码的 zip）")
    p_import.add_argument("--source", default="auto",
                          choices=["auto", "alipay", "wechat", "bank"],
                          help="数据源，默认按文件名和表头自动识别")
    p_import.add_argument("--password", default=None, help="压缩包密码")
    p_import.add_argument("--db", default=DEFAULT_DB, help=f"数据库路径（默认 {DEFAULT_DB}）")
    p_import.add_argument("--ai", action="store_true", help="用本地模型补分类（需 Ollama）")
    p_import.add_argument("--ignore-power", action="store_true", help="未插电也强制跑 AI")
    p_import.add_argument("--dry-run", action="store_true", help="只解析不写库")
    p_import.add_argument("--no-archive", dest="archive", action="store_false",
                          help="不把原始文件复制进 vault 归档")
    p_import.set_defaults(archive=True, func=cmd_import)

    p_review = sub.add_parser("review", help="逐条确认疑似重复")
    p_review.add_argument("--db", default=DEFAULT_DB)
    p_review.add_argument("--limit", type=int, default=200)
    p_review.add_argument("--resolve", type=int, help="要处置的交易 id")
    p_review.add_argument("--as", dest="as_", choices=["dup", "ok"], help="dup=确认重复, ok=独立交易")
    p_review.set_defaults(func=cmd_review)

    p_report = sub.add_parser("report", help="消费汇总")
    p_report.add_argument("--db", default=DEFAULT_DB)
    p_report.add_argument("--month", help="月份，形如 2026-09；不填只看总览")
    p_report.set_defaults(func=cmd_report)

    p_sources = sub.add_parser("sources", help="各渠道怎么导出账单")
    p_sources.set_defaults(func=cmd_sources)

    p_ocr = sub.add_parser("ocr", help="银行截图 → TSV（调本地 Windows OCR）")
    p_ocr.add_argument("images", nargs="+")
    p_ocr.add_argument("--out", default=os.path.join(DEFAULT_VAULT, "ocr"))
    p_ocr.set_defaults(func=cmd_ocr)

    return parser


def cmd_ocr(args) -> int:
    """调用 scripts/bank-ocr.ps1 做本地 OCR。

    OCR 用 Windows 自带的 OCR 引擎（Windows.Media.Ocr），
    中文识别开箱可用，不装任何东西、不上传任何图片。
    """
    import subprocess

    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "scripts", "bank-ocr.ps1")
    if not os.path.isfile(script):
        print(f"找不到 OCR 脚本：{script}")
        return 2
    os.makedirs(args.out, exist_ok=True)
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", script, "-OutDir", args.out] + args.images
    print("调用本地 OCR（Windows 自带引擎，不联网）……")
    return subprocess.call(cmd)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
