"""月度自动处理：扫描投递目录 → 逐个导入 → 出月报。

设计要点：

1. **幂等。** 重复跑不会重复记账——L1 去重按「同源 + 同交易单号」跳过。
   所以定时任务可以每天跑，不用怕。你甚至可以把同一个文件放两次。

2. **zip 密码用完即忘。** 加密的账单 zip 只在交互式运行时提示输入一次，
   用完立刻从内存丢弃，不写任何文件、不留任何缓存。
   代价是：**后台自动运行无法处理加密 zip**（没人给它输密码），
   后台跑只处理你已经解压好的文件。这是「不记密码」的必然结果。

3. **处理完就挪走。** 成功的文件移动到 `inbox/_done/`（是移动，不是删除），
   inbox 里剩下的东西一眼就能看出「还有没有没处理的」。
   失败的文件留在原地，绝不静默丢弃。

4. **不猜数据源。** 识别不出来的文件留在原地并报告。

5. **默认不开 AI。** 解析和去重都是轻量操作，不需要模型。绝大多数月份
   规则 + 缓存就能覆盖全部流水，一次模型调用都不用。

6. **导出那一步自动化不了。** 支付宝/微信/银行都没有 API，
   必须你在手机上手动申请、下载、截图。这条流水线负责的是「文件到手之后」。

7. **有疑似重复就自动把确认界面拉起来。** 交互式运行时（双击 monthly.cmd），
   处理完发现有疑似重复，直接打开网页版确认界面，不用你另敲一条命令。
   后台运行不会弹——没人在场，弹窗口只会干扰。
"""

from __future__ import annotations

import getpass
import os
import shutil
import sys
import zipfile
from datetime import datetime
from typing import Optional

from . import review_ui, secure_store
from .pipeline import import_file
from .power import describe
from .render import print_month, print_overview
from .secure_store import open_encrypted
from .sources import PARSERS, detect_source

INBOX_DIRNAME = "inbox"
DONE_DIRNAME = "_done"

# 只认这些后缀。刻意不含 .txt：
#   投递目录里会放一个「说明.txt」当使用指引，.txt 认进来会让每次运行
#   都多报一条失败，把真正的失败淹掉。银行 OCR 产出的是 .rows.tsv，
#   支付宝/微信的内层文件是 csv/xlsx，都不需要 .txt。
SUFFIXES = (".zip", ".csv", ".tsv", ".xlsx", ".xlsm", ".pdf")

IGNORED_NAMES = ("说明.txt", "README.txt", "README.md", "readme.md")

SOURCE_LABELS_EXTRA = {"bank_pdf": "银行卡PDF"}

INBOX_README = """把待处理的账单文件丢进这个目录，然后双击 scripts\\monthly.cmd

支持的放法：
    * 支付宝导出的 zip          → 运行时提示输密码，用完即忘
    * 微信导出的 zip            → 同上
    * 已解压的 .csv / .xlsx     → 直接解析，不需要密码
    * bank-ocr 产出的 *.rows.tsv → 银行截图路线

处理成功的文件会被移动到 _done\\ 子目录，这里剩下的就是还没处理的。
失败的文件会留在原地并给出原因，不会静默丢弃。

关于密码：本程序不保存你的 zip 解压密码。加密 zip 只能在交互式运行时处理；
登录后自动跑的后台任务只能处理已解压的文件。

这个目录在仓库之外。不要把它放进任何云盘同步目录。
"""


def inbox_dir(vault: str) -> str:
    return os.path.join(vault, INBOX_DIRNAME)


def done_dir(vault: str) -> str:
    return os.path.join(vault, INBOX_DIRNAME, DONE_DIRNAME)


def ensure_inbox(vault: str) -> str:
    box = inbox_dir(vault)
    os.makedirs(box, exist_ok=True)
    readme = os.path.join(box, "说明.txt")
    if not os.path.exists(readme):
        with open(readme, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(INBOX_README)
    return box


def scan_inbox(vault: str) -> list:
    """列出待处理的账单文件。说明文件、隐藏文件和 _done 子目录都不算。"""
    box = inbox_dir(vault)
    if not os.path.isdir(box):
        return []
    found = []
    for name in sorted(os.listdir(box)):
        if name in IGNORED_NAMES or name.startswith("."):
            continue
        full = os.path.join(box, name)
        if not os.path.isfile(full):
            continue
        if name.lower().endswith(SUFFIXES):
            found.append(full)
    return found


def _needs_password(path: str) -> bool:
    """这个文件需不需要密码。

    zip：读中央目录里的加密标志，最快最准。
    PDF：问 PyMuPDF 的 needs_pass。
    都不能靠「试着打开失败」判断——那要先把文件读一遍，慢且不必要。
    """
    try:
        if path.lower().endswith(".pdf"):
            from .bank_pdf import _require_pymupdf
            pymupdf = _require_pymupdf()
            doc = pymupdf.open(path)
            need = bool(getattr(doc, "needs_pass", False))
            doc.close()
            return need
        if not zipfile.is_zipfile(path):
            return False
        with zipfile.ZipFile(path) as zf:
            return any(info.flag_bits & 0x1 for info in zf.infolist())
    except Exception:
        return False


def file_needs_password(path: str) -> bool:
    """对外名字：这个文件要不要密码。

    网页控制台靠它决定「哪些文件得让你先填密码」。
    正文里一律走这个函数，不要再从外面直接摸 _needs_password。
    """
    return _needs_password(path)


def _prompt_password(label: str, kind: str = "压缩包") -> Optional[str]:
    """交互式索取密码。拿不到就返回 None，绝不猜、绝不回退到空密码。"""
    if not sys.stdin.isatty():
        return None
    try:
        return getpass.getpass(
            f"  {label}账单的{kind}密码（不回显，用完即忘，不保存；直接回车跳过）："
        )
    except (KeyboardInterrupt, EOFError):
        print()
        return None


def _move_to_done(path: str, vault: str) -> str:
    """挪到 _done/。同名时加时间戳，绝不覆盖——覆盖等于丢数据。"""
    target_dir = done_dir(vault)
    os.makedirs(target_dir, exist_ok=True)
    base = os.path.basename(path)
    target = os.path.join(target_dir, base)
    if os.path.exists(target):
        stem, ext = os.path.splitext(base)
        target = os.path.join(target_dir, f"{stem}.{datetime.now():%Y%m%d-%H%M%S}{ext}")
    shutil.move(path, target)
    return target


def run(vault: str, *, use_ai: bool = False, ignore_power: bool = False,
        keep: bool = False, dry_run: bool = False, auto_review: bool = True,
        passwords: Optional[dict] = None,
        interactive: Optional[bool] = None) -> int:
    """跑一次月度批处理。

    三个参数是给「网页控制台」这类非终端调用方用的（cli 不传，行为不变）：

    passwords   {数据源: 密码}。网页控制台把用户在页面上填的密码从这里注入，
                而不是让 getpass 去读一个不存在的控制台。
    interactive None = 按 sys.stdin.isatty() 判断。
                网页控制台必须显式传 False —— 从 .cmd 双击启动时
                stdin **是**终端，不传的话会误判成交互式，进而在 cmd 窗口里
                弹密码提示、并自动拉起另一个确认界面（服务套服务）。
    auto_review 同上，网页控制台自己有确认页面，传 False。
    """
    box = ensure_inbox(vault)
    files = scan_inbox(vault)
    if interactive is None:
        interactive = sys.stdin.isatty()
    # 只有在「有人在场 + 会真写库」时才自动弹确认界面。
    # 后台定时任务里弹浏览器是灾难：没人在场、窗口自动弹出、点都没人点。
    will_auto_review = auto_review and interactive and not dry_run

    print("=" * 72)
    print(f"MyLedger 月度处理   {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"投递目录 : {box}")
    print(f"数据库   : {os.path.join(vault, 'ledger.db.enc')}（DPAPI 加密）")
    print(f"电源     : {describe()}")
    print(f"运行方式 : {'交互式（可输入 zip 密码）' if interactive else '后台（无法输入 zip 密码）'}")
    if use_ai:
        print("本地 AI  : 已开启（仅用于商户分类兜底）")
    else:
        print("本地 AI  : 关闭（解析和去重不需要模型）")
    if will_auto_review:
        print("疑似重复 : 处理完自动打开确认界面（加 --no-review 可关掉）")
    print("=" * 72)

    if not files:
        print()
        print("投递目录里没有待处理的文件。")
        print("把导出的账单文件放进去，再跑一次即可。")
        print("（导出方法：python -m importer sources）")
        return 0

    print(f"发现 {len(files)} 个待处理文件")
    for path in files:
        print(f"  {os.path.basename(path)}")

    months: set = set()
    ok_count = 0
    fail: list = []

    # 一次运行内的密码只存在内存里：同一个渠道的多个 zip 不必重复输入，
    # 但运行一结束就随进程一起消失，不落任何盘。
    # 网页控制台传进来的密码同样只活在这次调用里，用完即忘。
    session_passwords: dict = dict(passwords or {})

    with open_encrypted(vault, save_on_exit=not dry_run) as ledger:
        for path in files:
            print()
            print("-" * 72)
            print(f"处理：{os.path.basename(path)}")

            source = detect_source(path)
            if source not in PARSERS:
                print("  识别不出这是哪个渠道的账单，留在原地等你处理。")
                print("  确认渠道后可手动指定：python -m importer import <文件> --source alipay|wechat|bank")
                fail.append((path, "无法识别数据源"))
                continue

            label = {"alipay": "支付宝", "wechat": "微信",
                     "bank_ocr": "银行截图", "bank_pdf": "银行卡PDF"}.get(source, source)
            print(f"  识别为：{label}")

            password = session_passwords.get(source)
            if _needs_password(path):
                kind = "PDF" if path.lower().endswith(".pdf") else "压缩包"
                if password is None and interactive:
                    password = _prompt_password(label, kind)
                    if password:
                        session_passwords[source] = password
                if not password:
                    print(f"  这是加密 {kind}，当前运行方式拿不到密码，跳过。")
                    print("  处理办法：在能输入密码的地方重跑一次 ——")
                    print("            网页控制台在导入页填密码；命令行交互式运行会提示输入。")
                    print("            或者自己解开后把 csv 丢进投递目录。")
                    fail.append((path, f"加密 {kind} 需要密码"))
                    continue

            try:
                outcome = import_file(
                    path, ledger,
                    source=source, password=password,
                    use_ai=use_ai, ignore_power=ignore_power,
                    archive=True, dry_run=dry_run,
                )
            except Exception as exc:
                # 单个文件崩了不能带崩整批。文件留在原地，原因说清楚。
                import traceback
                print(f"  处理这个文件时崩了：{type(exc).__name__}: {exc}")
                detail = traceback.format_exc().strip().splitlines()
                for line in detail[-6:]:
                    print(f"    {line.strip()}")
                print("  文件留在原地，不会丢。")
                fail.append((path, f"{type(exc).__name__}: {exc}"))
                continue

            if not outcome.ok:
                print(f"  未导入：{outcome.error}")
                print("  文件留在原地，不会丢。")
                fail.append((path, outcome.error))
                continue

            ok_count += 1
            months |= outcome.months

            if dry_run:
                print("  [dry-run] 文件未移动。")
            elif keep:
                print("  --keep：文件保留在投递目录。")
            else:
                moved = _move_to_done(path, vault)
                print(f"  已移入 {os.path.relpath(moved, box)}")

        # --------------------------------------------------------------
        # 报表
        # --------------------------------------------------------------
        print()
        print_overview(ledger)
        for month in sorted(months):
            print_month(ledger, month)
        suspects = ledger.conn.execute(
            "SELECT COUNT(*) AS n FROM txn WHERE review LIKE 'suspect%'"
        ).fetchone()["n"]

        # --------------------------------------------------------------
        # 有疑似重复就直接把确认界面拉起来。
        #
        # 放在 with 块里面，是因为裁定要写回同一个内存库：
        # 出了这个块账本就加密落盘了，之后再改就是另一个连接，白改。
        # --------------------------------------------------------------
        if suspects and will_auto_review:
            print()
            print("=" * 72)
            print(f"发现 {suspects} 笔疑似重复，直接打开确认界面。")

            # 先把导入结果落盘，再开界面。
            #
            # open_encrypted 只在 with 退出时保存，而确认界面会一直阻塞到审完为止。
            # 不先存一次的话，这段时间里账本文件还是旧内容 —— 关掉窗口、
            # 或者中途崩了，刚导进来的几十笔就白导了（用户的真实账单这么丢过一次）。
            # 裁定结果会在 with 退出时再存一次，两次写入互不影响。
            try:
                size = secure_store.save(ledger)
                print(f"（已在打开界面前落盘，{size / 1024:.1f} KB —— 即使中途关掉窗口，")
                print("  本次导入的数据也不会丢）")
            except Exception as exc:
                print(f"！提前落盘失败：{type(exc).__name__}: {exc}")
                print("  导入数据目前只在内存里，请务必点完界面再关窗口。")

            resolved = review_ui.run(ledger)
            print(f"确认了 {resolved} 笔。")

    print()
    print("=" * 72)
    print(f"本次处理：成功 {ok_count} 个，失败 {len(fail)} 个")
    if fail:
        print("失败清单（文件都还在原地）：")
        for path, reason in fail:
            print(f"  {os.path.basename(path)} —— {reason}")
    if suspects:
        print()
        if will_auto_review:
            print(f"还有 {suspects} 笔疑似重复没处理完。重新打开界面：")
        else:
            print(f"有 {suspects} 笔疑似重复等你确认。用大按钮界面最快：")
        print("  python -m importer review --ui")

    return 0 if not fail else 2
