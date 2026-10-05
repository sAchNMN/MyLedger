"""单文件导入流水线。

cli 的 `import` 和 monthly 的批处理都走这一份逻辑。
共用是关键：如果两条路径各写一遍，迟早会出现「手动导没问题、自动跑漏数」
这种最难查的偏差。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .categorize import categorize
from .model import describe_issue
from .render import money
from .sources import PARSERS, detect_source
from .store import Ledger


# --------------------------------------------------------------------------
# 导入结果状态（ML-006）
#
# 为什么不是一个 ok 布尔：
#     导入是逐笔写入的，「循环跑完」和「全部写入成功」是两件事。
#     一个 bit 表达不了「3 笔里挂了 1 笔」，调用方就只能靠猜 ——
#     猜错的代价是源文件被归档、失败的流水永远不再重试，而全程没有任何报错。
#
#     success        全部写成功（重复跳过不算失败）   → 允许归档
#     partial_failed 有行失败，也有行写进去了        → 源文件留在原地等重试
#     write_failed   一笔都没写成功（含写入中途中断） → 源文件留在原地
#     parse_failed   解析阶段就失败                  → 连批次行都不建
#     dry_run        只试算                          → 不写库、不动文件
#     not_started    还没走到任何判定                → ok=False
#
#     ok 由 status 派生，不再是一个独立字段：两个来源迟早会不一致，
#     而不一致的方向如果恰好是「status 说失败、ok 说成功」，就又漏记了。
#     默认值取 not_started 而不是 success，失败必须往「不成功」的方向倒。
# --------------------------------------------------------------------------

OUTCOME_STATUSES = (
    "not_started",
    "success",
    "partial_failed",
    "write_failed",
    "parse_failed",
    "dry_run",
)

STATUS_LABELS = {
    "not_started": "尚未开始",
    "success": "全部写入成功",
    "partial_failed": "部分交易写入失败",
    "write_failed": "一笔都没有写入成功",
    "parse_failed": "解析阶段失败",
    "dry_run": "试算，未写库",
}


@dataclass
class ImportOutcome:
    path: str
    source: str = ""
    status: str = "not_started"
    parsed: int = 0
    declared: Optional[int] = None
    inserted: int = 0
    identical: int = 0
    suspect: int = 0
    skipped: int = 0
    batch_id: str = ""
    archived: str = ""
    months: set = field(default_factory=set)
    failed_rows: list = field(default_factory=list)   # 单行写入失败，不影响其余
    error: str = ""
    dry_run: bool = False
    # 下面两个是给后台日志用的：只有错误码和异常类型名，不含任何取值。
    # 日志要能回答「出了哪类问题」，但不能回答「谁在哪花了多少」。
    issue_codes: list = field(default_factory=list)
    write_error_types: list = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.inserted + self.identical

    @property
    def ok(self) -> bool:
        """这批算不算「处理完了」。只有 success 和 dry_run 为真。

        其余一律为假 —— 包括 not_started。宁可让人多看一眼，
        也不能让一个没走到判定的批次被当成成功归档。
        """
        return self.status in ("success", "dry_run")

    @property
    def archivable(self) -> bool:
        """能不能把源文件收进归档。只有全部写入成功才可以。"""
        return self.status == "success"

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)


def import_file(
    path: str,
    ledger: Ledger,
    *,
    source: str = "auto",
    password: Optional[str] = None,
    use_ai: bool = False,
    ignore_power: bool = False,
    archive: bool = True,
    dry_run: bool = False,
    emit: Callable[[str], None] = print,
) -> ImportOutcome:
    """把一份账单文件导入账本。

    任何一步失败都返回一个 status 不是 success 的 outcome。
    注意：部分失败**会**留下已写入的那些记录（这是刻意的，见下面的注释），
    所以调用方不能靠「没抛异常」判断成功，要看 outcome.archivable。
    """
    outcome = ImportOutcome(path=path, dry_run=dry_run)

    if source != "auto":
        outcome.source = source
    else:
        # 账本开着就能读「渠道记忆」，所以自动识别会认用户指定过的形状。
        from . import sourcehint
        outcome.source = detect_source(path, sourcehint.hint_map(ledger))
    if outcome.source not in PARSERS:
        outcome.status = "parse_failed"
        outcome.error = (
            "无法识别数据源。"
            "命令行用 --source alipay|wechat|bank_pdf 指定；"
            "网页控制台在导入页选「渠道」下拉框，"
            "或用 setsrc 记一次，以后同类文件自动认。"
        )
        emit(f"无法识别数据源：{path}")
        return outcome

    try:
        result = PARSERS[outcome.source](path, password)
    except Exception as exc:
        outcome.status = "parse_failed"
        outcome.error = f"{type(exc).__name__}: {exc}"
        emit(f"读取失败：{path}")
        emit(f"  {outcome.error}")
        return outcome

    outcome.parsed = len(result.txns)
    outcome.declared = result.declared_count
    outcome.skipped = len(result.skipped)
    outcome.issue_codes = sorted({item.get("reason_code") or "unknown"
                                  for item in result.skipped})
    emit(result.summary())

    # ------------------------------------------------------------------
    # 失败要响亮。
    #
    # 这条校验来自一次真实事故：一份账单导出文件只有说明和表头、
    # 没有任何流水，但文件开头明明白白写着「共 87 笔记录」。
    # 如果不校验，导入器会安安静静地写进一个空批次，
    # 你会在月底看到「本月零支出」——这种错比报错危险得多。
    # 自动化场景下这个校验更重要：没人在旁边看输出。
    # ------------------------------------------------------------------
    if not result.txns:
        msg = "这份文件里一笔流水都没解析出来"
        if result.declared_count:
            msg += f"，但文件自称有 {result.declared_count} 笔记录"
        emit(f"导入中止：{msg}。没有任何数据被写入。")
        for item in result.skipped[:5]:
            emit(f"  {describe_issue(item)}")
        outcome.status = "parse_failed"
        outcome.error = msg
        return outcome

    if result.declared_count is not None and len(result.txns) != result.declared_count:
        emit(f"！注意：解析到 {len(result.txns)} 笔，文件自称 {result.declared_count} 笔，"
             f"差 {result.declared_count - len(result.txns)} 笔。可能文件不完整。")

    # 这里刻意只打印 describe_issue() 的结果：行号 + 字段 + 错误码。
    # 这段文字会一路流进后台日志，所以绝不能带上那一行的原文（ML-007）。
    if result.skipped:
        emit(f"跳过 {len(result.skipped)} 行：")
        for item in result.skipped[:10]:
            emit(f"  {describe_issue(item)}")
        if len(result.skipped) > 10:
            emit(f"  …另外 {len(result.skipped) - 10} 行")

    cat_stats = categorize(result.txns, ledger, use_ai=use_ai, ignore_power=ignore_power)

    if dry_run:
        emit("[dry-run] 不写库。分类结果预览：")
        for txn in result.txns[:15]:
            emit(f"  {txn.ts:%m-%d %H:%M}  {money(txn.amount):>10}  "
                 f"{txn.category:<6}  {txn.counterparty[:24]}")
        if len(result.txns) > 15:
            emit(f"  …共 {len(result.txns)} 笔")
        outcome.status = "dry_run"
        return outcome

    ledger.start_batch(outcome.source, path, origin="",
                       parsed=len(result.txns), declared=result.declared_count)
    ledger.set_batch_status("writing")

    # ------------------------------------------------------------------
    # 逐笔写入，单行失败不拖垮整份账单。
    #
    # 这条来自真实事故：某份 PDF 解析出 31 笔，然后在写第一笔时抛异常，
    # 整个导入中断 —— 批次记录建了但一笔没写，文件也没移走，
    # 表面症状只有一句「解析 31 笔，写入 0 笔」，看不出到底哪里出问题。
    # 现在把每笔的异常单独接住、记下来继续跑，最后连同失败原因一起报告。
    # finish_batch 放进 finally，保证批次状态一定是完整的。
    #
    # 终态判定也放在 finally 里（ML-006）：只要有行失败，批次就绝不能是
    # committed —— 否则 monthly 会据此把源文件移进 _done/，
    # 失败的那几笔从此不会再被重试，报表上永远少这几笔。
    # 已写入的记录**不**回滚：留着才有得核对，重试时 L1/L2 去重会跳过它们。
    # ------------------------------------------------------------------
    completed = False
    try:
        for index, txn in enumerate(result.txns, start=1):
            try:
                verdict = ledger.add(txn)
            except Exception as exc:
                # 只记「第几笔 + 异常类型」，不记时间和异常消息。
                # 异常消息里可能带着行内容（而且时间本身就是消费记录的一部分），
                # 而这些文字会进日志 —— 和解析失败同一条规矩（ML-007）。
                outcome.failed_rows.append({
                    "index": index,
                    "error_type": type(exc).__name__,
                })
                continue

            outcome.months.add(txn.ts.strftime("%Y-%m"))
            if verdict.is_duplicate_skip:
                outcome.identical += 1
            elif verdict.needs_review:
                outcome.suspect += 1
                outcome.inserted += 1
            else:
                outcome.inserted += 1
        completed = True
    finally:
        if not completed:
            # 循环中途抛出去了。异常继续往上抛（调用方要看到它），
            # 但批次状态必须落成「不完整」，绝不能停在 writing 上。
            outcome.status = "write_failed"
            outcome.error = "写入过程中断，批次不完整，源文件必须留在原地"
            final_status = "failed"
        elif outcome.failed_rows and not (outcome.inserted or outcome.identical):
            outcome.status = "write_failed"
            outcome.error = (f"全部 {len(outcome.failed_rows)} 笔交易都写入失败，"
                             f"一笔都没入库")
            final_status = "failed"
        elif outcome.failed_rows:
            outcome.status = "partial_failed"
            outcome.error = (f"有 {len(outcome.failed_rows)} 笔交易写入失败"
                             f"（其余 {outcome.inserted + outcome.identical} 笔已入库）")
            final_status = "partial_failed"
        else:
            outcome.status = "success"
            final_status = "committed"

        ledger.finish_batch(len(result.txns), result.declared_count,
                            outcome.inserted, outcome.identical,
                            outcome.suspect, len(result.skipped) + len(outcome.failed_rows),
                            status=final_status)
        outcome.write_error_types = sorted({row["error_type"] for row in outcome.failed_rows})

    outcome.batch_id = ledger.batch_id

    emit(f"写入 {outcome.inserted} 笔  重复跳过 {outcome.identical} 笔  "
         f"疑似重复待确认 {outcome.suspect} 笔")
    if outcome.failed_rows:
        emit(f"！有 {len(outcome.failed_rows)} 笔写入失败（其余已入库）：")
        for item in outcome.failed_rows[:5]:
            emit(f"    第 {item['index']} 笔写入失败（{item['error_type']}）")
        emit(f"！本批状态：{outcome.status_label} —— 源文件不会被归档，补完后再跑一次即可。")
    emit(f"分类：人工指定 {cat_stats['manual']}，平台分类 {cat_stats['platform']}，"
         f"规则命中 {cat_stats['rules']}，缓存命中 {cat_stats['cache']}，"
         f"本地 AI {cat_stats['ai']}，仍未分类 {cat_stats['unmatched']}")
    if cat_stats["status_neutralised"]:
        emit(f"！有 {cat_stats['status_neutralised']} 笔的状态显示这笔没真花出去"
             f"（交易关闭 / 已退款 / 已过期等），已压成不计收支，不计入支出。")

    if archive and outcome.archivable:
        archived = archive_source(path, ledger, outcome.source)
        if archived:
            outcome.archived = archived
            ledger.set_batch_status("archived")
            emit(f"原始文件已归档到 {archived}")

    return outcome


def archive_source(path: str, ledger: Ledger, source: str) -> str:
    """把原始账单复制进 vault 归档，便于日后重新解析。

    是复制不是移动：源文件留在原地，你随时能自己核对。
    """
    import os
    import shutil

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
