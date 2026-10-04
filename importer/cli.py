"""命令行入口。

    python -m importer monthly                 月度批处理（推荐用法）
    python -m importer review --ui             疑似重复确认（两个大按钮）
    python -m importer import <文件...>         单文件导入
    python -m importer report [--month 2026-09] 消费汇总
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
from .model import SOURCE_LABELS
from .pipeline import import_file
from .render import money, print_month, print_overview

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
