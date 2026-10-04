"""单文件导入流水线。

cli 的 `import` 和 monthly 的批处理都走这一份逻辑。
共用是关键：如果两条路径各写一遍，迟早会出现「手动导没问题、自动跑漏数」
这种最难查的偏差。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .categorize import categorize
from .render import money
from .sources import PARSERS, detect_source
from .store import Ledger


@dataclass
class ImportOutcome:
    path: str
    source: str = ""
    ok: bool = False
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

    @property
    def total(self) -> int:
        return self.inserted + self.identical


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
    """把一份账单文件导入账本。任何一步失败都返回 ok=False，不写库。"""
    outcome = ImportOutcome(path=path, dry_run=dry_run)

    outcome.source = source if source != "auto" else detect_source(path)
    if outcome.source not in PARSERS:
        outcome.error = "无法识别数据源（用 --source alipay|wechat|bank 手动指定）"
        emit(f"无法识别数据源：{path}")
        return outcome

    try:
        result = PARSERS[outcome.source](path, password)
    except Exception as exc:
        outcome.error = f"{type(exc).__name__}: {exc}"
        emit(f"读取失败：{path}")
        emit(f"  {outcome.error}")
        return outcome

    outcome.parsed = len(result.txns)
    outcome.declared = result.declared_count
    outcome.skipped = len(result.skipped)
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
            emit(f"  跳过原因（第 {item.get('row')} 行）：{item.get('reason')}")
        outcome.error = msg
        return outcome

    if result.declared_count is not None and len(result.txns) != result.declared_count:
        emit(f"！注意：解析到 {len(result.txns)} 笔，文件自称 {result.declared_count} 笔，"
             f"差 {result.declared_count - len(result.txns)} 笔。可能文件不完整。")

    if result.skipped:
        emit(f"跳过 {len(result.skipped)} 行：")
        for item in result.skipped[:10]:
            emit(f"  第 {item.get('row')} 行：{item.get('reason')}")
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
        outcome.ok = True
        return outcome

    ledger.start_batch(outcome.source, path, origin="",
                       parsed=len(result.txns), declared=result.declared_count)

    # ------------------------------------------------------------------
    # 逐笔写入，单行失败不拖垮整份账单。
    #
    # 这条来自真实事故：某份 PDF 解析出 31 笔，然后在写第一笔时抛异常，
    # 整个导入中断 —— 批次记录建了但一笔没写，文件也没移走，
    # 表面症状只有一句「解析 31 笔，写入 0 笔」，看不出到底哪里出问题。
    # 现在把每笔的异常单独接住、记下来继续跑，最后连同失败原因一起报告。
    # finish_batch 放进 finally，保证批次状态一定是完整的。
    # ------------------------------------------------------------------
    try:
        for txn in result.txns:
            try:
                verdict = ledger.add(txn)
            except Exception as exc:
                outcome.failed_rows.append({
                    "ts": txn.ts.isoformat(sep=" "),
                    "reason": f"{type(exc).__name__}: {exc}",
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
    finally:
        ledger.finish_batch(len(result.txns), result.declared_count,
                            outcome.inserted, outcome.identical,
                            outcome.suspect, len(result.skipped) + len(outcome.failed_rows))

    outcome.batch_id = ledger.batch_id
    outcome.ok = True

    emit(f"写入 {outcome.inserted} 笔  重复跳过 {outcome.identical} 笔  "
         f"疑似重复待确认 {outcome.suspect} 笔")
    if outcome.failed_rows:
        emit(f"！有 {len(outcome.failed_rows)} 笔写入失败（其余已入库）：")
        for item in outcome.failed_rows[:5]:
            emit(f"    {item['ts']}  {item['reason']}")
    emit(f"分类：规则命中 {cat_stats['rules']}，缓存命中 {cat_stats['cache']}，"
         f"本地 AI {cat_stats['ai']}，仍未分类 {cat_stats['unmatched']}")

    if archive:
        archived = archive_source(path, ledger, outcome.source)
        if archived:
            outcome.archived = archived
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
