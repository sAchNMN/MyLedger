"""命令行入口。

    python -m importer monthly                 月度批处理（推荐用法）
    python -m importer review --ui             疑似重复确认（两个大按钮）
    python -m importer import <文件...>         单文件导入
    python -m importer report [--month 2026-09] 消费汇总
    python -m importer unmatched               盘点未分类交易（按商户聚合）
    python -m importer setcat <商户> <分类>     手工指定分类（优先级最高）
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

from . import __version__, fixups, monthly, review_ui, secure_store
from .categorize import CATEGORIES, clear_manual, manual_map, set_manual
from .model import SOURCE_LABELS
from .pipeline import import_file
from .render import mask_merchant, money, print_month, print_overview, table

DEFAULT_VAULT = os.path.join(os.path.expanduser("~"), "MyLedgerVault")


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
    if password is None and args.source in ("alipay", "wechat") and sys.stdin.isatty():
        # 只提示一次，用完即丢；不传 --password 且是交互式时不硬要，
        # 因为文件可能已经解压过了。
        try:
            label = SOURCE_LABELS.get(args.source, args.source)
            password = getpass.getpass(
                f"{label}账单的解压密码（不回显，用完即忘，不保存；直接回车跳过）："
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

    ok = [o for o in outcomes if o.ok]
    bad = [o for o in outcomes if not o.ok]

    print("=" * 72)
    if bad:
        print(f"有 {len(bad)} 个文件没导入成功：")
        for o in bad:
            print(f"  {o.path}")
            print(f"    原因：{o.error}")
    suspects = sum(o.suspect for o in ok)
    if suspects:
        print(f"有 {suspects} 笔疑似重复，用大按钮界面确认最快：")
        print("  python -m importer review --ui")
    print(f"本次共写入 {sum(o.inserted for o in ok)} 笔。")
    return 2 if bad else 0


# --------------------------------------------------------------------------
# monthly
# --------------------------------------------------------------------------

def cmd_monthly(args) -> int:
    return monthly.run(
        _vault(args),
        use_ai=args.ai, ignore_power=args.ignore_power,
        keep=args.keep, dry_run=args.dry_run,
        auto_review=not args.no_review,
    )


# --------------------------------------------------------------------------
# recompute —— 按当前规则修正历史数据（默认只试算）
# --------------------------------------------------------------------------

def cmd_recompute(args) -> int:
    vault = _vault(args)
    enc = secure_store.encrypted_path(vault)

    if args.apply:
        if os.path.exists(enc):
            fixups.backup(enc)
        with secure_store.open_encrypted(vault, save_on_exit=True) as ledger:
            fixups.recompute(ledger, apply=True)
    else:
        # 试算：只读内存，退出时不写回
        with secure_store.open_encrypted(vault, save_on_exit=False) as ledger:
            fixups.recompute(ledger, apply=False)
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

    if info["plaintext_exists"]:
        print()
        print(f"！发现明文库：{info['plaintext_path']}")
        for path in info["wal_files"]:
            print(f"！   附属文件：{path}")
        print("  下次导入时会自动迁移到加密存储，但**明文文件不会被自动删除**。")
        print("  迁移完成后请手动删掉它们——留着等于账本没加密。")

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
    return subprocess.call(cmd)


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
    p.set_defaults(func=cmd_recompute)

    # ---- unmatched ----
    p = _add_vault(sub.add_parser("unmatched", help="盘点未分类交易（按商户聚合）"))
    p.add_argument("--month", help="只统计某个月，形如 2026-09")
    p.add_argument("--limit", type=int, default=200, help="最多列出多少个商户")
    p.add_argument("--mask", action="store_true",
                   help="商户名遮蔽后输出，便于把结果安全外发")
    p.set_defaults(func=cmd_unmatched)

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

    # ---- sources ----
    sub.add_parser("sources", help="各渠道怎么导出账单").set_defaults(func=cmd_sources)

    # ---- ocr ----
    p = sub.add_parser("ocr", help="银行截图 → TSV（调本地 Windows OCR）")
    p.add_argument("images", nargs="+")
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_ocr)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
