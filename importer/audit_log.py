"""结构化审计日志：后台自动运行时唯一允许落盘的东西（ML-004）。

为什么要有这个模块
------------------
后台执行器（scripts\\monthly-run.ps1）以前是这么记日志的：

    $output = & $python @pyArgs 2>&1
    [System.IO.File]::AppendAllLines($LogFile, $output)

子进程的**全部**标准输出和错误输出都被追加进 `vault\\logs\\monthly-YYYYMM.log`。
解析失败的行、dry-run 的预览、异常堆栈、异常消息 —— 有什么抄什么。
而这份日志的处境是：长期保留、可能被云盘同步、可能被备份、
半年后还可能被人翻出来看。

「日志」在隐私账上是最危险的一种文件：它不像账本那样有加密、有明确归属，
却往往是整个 vault 里唯一一份人人可读的明文副本。

所以这里换个方向：不再「过滤输出」，而是**从零构造白名单记录**。

    * 只有 ALLOWED_FIELDS 里的字段能进日志
    * 白名单外的字段被丢掉，**只留字段名不留值**（rejected() 可以查）
    * 没有自由文本字段 —— 想说清一件事就得先来加一个字段
    * 文件名走 mask_name()，把长数字串（账号、订单号）打掉

格式是 JSON Lines：一行一条事件。机器可读、能 grep、追加写不破坏旧内容。

刻意不做的事：
    * 不写完整路径、不写命令行、不写异常堆栈
    * 不写原文件名（可能含姓名/账号）
    * 不写交易字段（商户、金额、单号、备注）
    真要看这些，跑一次交互式的 monthly.cmd，报告在屏幕上，不落盘。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Iterable

# 允许落盘的字段。想加字段就来改这张表 —— 改的时候自然会想一下
# 「这个值是不是来自原始账单」。
ALLOWED_FIELDS = frozenset({
    # 事件本身
    "ts", "event",
    # 运行环境（不含路径、不含命令行）
    "interactive", "dry_run", "ai", "files",
    # 单个文件的结果（按序号标识，不写文件名原文）
    "index", "source", "status", "file_masked",
    # 计数
    "parsed", "declared", "inserted", "identical", "suspect",
    "skipped_rows", "failed_rows",
    # 错误只用码和类型名，不用自由文本
    "issue_codes", "write_error_types",
    # 收尾
    "ok", "partial_failed", "failed", "exit_code", "suspect_total", "months",
})

# 事件名也定义成常量，避免各处手写字符串写歪了
EVENT_RUN_START = "run_start"
EVENT_FILE = "file_done"
EVENT_RUN_END = "run_end"

_DIGITS = re.compile(r"\d{4,}")


def mask_name(name: str) -> str:
    """把文件名里的长数字串打掉。

    为什么要脱敏：账单文件名经常自带账号、卡号、订单号 ——
    「招商银行_张三_622202123456_202609.pdf」这种。原样写进日志，
    等于绕了一圈又把 L3 级别的数据（账号片段）留下来了。

    只打掉 4 位以上的连续数字：账号、订单号、年月日都在这个范围里，
    而「09月」这种短数字保留下来，日志还能看。
    """
    return _DIGITS.sub("####", name or "")


class AuditLog:
    """一次运行的结构化日志。追加写，不覆盖。

    record() 永远不会因为字段不合法而抛异常：日志写不下去不该把月度处理带崩，
    但被丢掉的字段名会记在 rejected() 里，调用方可以据此提醒「有人往日志里塞了
    不该塞的东西」—— 静默丢弃和一个报错都没有，是两种不同的坏。
    """

    def __init__(self, path: str):
        self.path = path
        self._rejected: set[str] = set()

    def rejected(self) -> list[str]:
        """被白名单丢掉的字段名（只有名字，没有值）。"""
        return sorted(self._rejected)

    def record(self, event: str, **fields) -> dict:
        """写一条事件，返回真正落盘的那条记录。"""
        payload: dict = {
            "ts": datetime.now().isoformat(sep=" ", timespec="seconds"),
            "event": event,
        }
        for key, value in fields.items():
            if key in ALLOWED_FIELDS:
                payload[key] = value
            else:
                self._rejected.add(key)

        line = json.dumps(payload, ensure_ascii=False)
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
        return payload


def summarise(outcomes: Iterable, months: Iterable[str]) -> dict:
    """把一轮运行的结果压成几个计数（只输出数字，不含任何交易内容）。"""
    ok = partial = failed = 0
    inserted = suspect = 0
    for outcome in outcomes:
        if outcome.status == "partial_failed":
            partial += 1
        elif outcome.ok:
            ok += 1
        else:
            failed += 1
        inserted += outcome.inserted
        suspect += outcome.suspect
    return {
        "ok": ok,
        "partial_failed": partial,
        "failed": failed,
        "inserted": inserted,
        "suspect_total": suspect,
        "months": sorted(months),
    }
