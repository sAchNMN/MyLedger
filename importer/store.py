"""SQLite 存储层。

只有一个数据库文件，放在 vault 里（仓库外）。
表结构刻意保留 raw 原始行——账单解析一旦出错，你需要能回答
「这笔 38.5 元到底是从源文件哪一行读出来的」，而不是只能重导一遍。

review 字段记录每笔交易的审核状态：
    ''             正常，无需处理
    suspect_exact  疑似重复（内容指纹完全一致）
    suspect_near   疑似重复（金额+时间+对方相似）
    confirmed_dup  人工确认为重复
    confirmed_ok   人工确认为独立交易（不再提示）
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from .dedup import DedupResult, classify
from .model import SOURCE_LABELS, Txn, normalize_text

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS batch (
    batch_id     TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    path         TEXT NOT NULL,
    origin       TEXT,
    file_sha1    TEXT,
    file_size    INTEGER,
    imported_at  TEXT NOT NULL,
    parsed       INTEGER DEFAULT 0,
    declared     INTEGER,
    inserted     INTEGER DEFAULT 0,
    identical    INTEGER DEFAULT 0,
    suspect      INTEGER DEFAULT 0,
    skipped      INTEGER DEFAULT 0,
    note         TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS txn (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source            TEXT NOT NULL,
    txn_id            TEXT DEFAULT '',
    merchant_order_id TEXT DEFAULT '',
    ts                TEXT NOT NULL,
    amount            TEXT NOT NULL,
    direction         TEXT NOT NULL,
    counterparty      TEXT DEFAULT '',
    item              TEXT DEFAULT '',
    method            TEXT DEFAULT '',
    status            TEXT DEFAULT '',
    note              TEXT DEFAULT '',
    category          TEXT DEFAULT '未分类',
    match_key         TEXT,
    fingerprint       TEXT NOT NULL,
    batch_id          TEXT DEFAULT '',
    review            TEXT DEFAULT '',
    review_note       TEXT DEFAULT '',
    dup_of            INTEGER,
    raw               TEXT DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_txn_match_key
    ON txn(match_key) WHERE match_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_txn_fingerprint ON txn(fingerprint);
CREATE INDEX IF NOT EXISTS ix_txn_ts_amount ON txn(ts, amount);
CREATE INDEX IF NOT EXISTS ix_txn_review ON txn(review);
CREATE INDEX IF NOT EXISTS ix_txn_category ON txn(category);

CREATE TABLE IF NOT EXISTS merchant_category (
    merchant   TEXT PRIMARY KEY,
    category   TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
"""


def file_sha1(path: str) -> str:
    """算文件指纹，用于识别「这个文件我导过了」。"""
    digest = hashlib.sha1()
    size = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest()


class Ledger:
    """账本。所有写操作都经过去重判定。"""

    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._batch_seen_keys: set[str] = set()
        self._batch_seen_fp: dict[str, str] = {}
        self.batch_id = ""

    # ------------------------------------------------------------------
    # 去重查询接口（dedup.classify 依赖这三个）
    # ------------------------------------------------------------------

    def by_match_key(self, key: str) -> Optional[sqlite3.Row]:
        if key in self._batch_seen_keys:
            return self.conn.execute(
                "SELECT * FROM txn WHERE match_key = ? LIMIT 1", (key,)
            ).fetchone()
        return self.conn.execute(
            "SELECT * FROM txn WHERE match_key = ? LIMIT 1", (key,)
        ).fetchone()

    def by_fingerprint(self, fp: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM txn WHERE fingerprint = ? ORDER BY ts", (fp,)
        ).fetchall()

    def near(self, ts: datetime, amount: Decimal, window_seconds: int) -> list[sqlite3.Row]:
        lo = (ts - timedelta(seconds=window_seconds)).isoformat(sep=" ", timespec="seconds")
        hi = (ts + timedelta(seconds=window_seconds)).isoformat(sep=" ", timespec="seconds")
        return self.conn.execute(
            "SELECT * FROM txn WHERE amount = ? AND ts BETWEEN ? AND ? "
            "AND review != 'confirmed_ok' ORDER BY ts",
            (str(amount), lo, hi),
        ).fetchall()

    def describe(self, row) -> str:
        label = SOURCE_LABELS.get(row["source"], row["source"])
        return f"[{label}] {row['ts']} {row['amount']} 元 {row['counterparty']}"

    # ------------------------------------------------------------------
    # 批次与写入
    # ------------------------------------------------------------------

    def start_batch(self, source: str, path: str, origin: str = "",
                    parsed: int = 0, declared: Optional[int] = None) -> str:
        sha = ""
        size = 0
        if os.path.isfile(path):
            sha = file_sha1(path)
            size = os.path.getsize(path)
            # 同一个文件重复导入直接提示，省得白跑一遍
            prev = self.conn.execute(
                "SELECT batch_id, imported_at, inserted FROM batch "
                "WHERE file_sha1 = ? AND source = ? ORDER BY imported_at DESC LIMIT 1",
                (sha, source),
            ).fetchone()
            if prev:
                print(f"提示：这个文件在 {prev['imported_at']} 已经导入过"
                      f"（批次 {prev['batch_id']}，写入 {prev['inserted']} 笔），"
                      f"本次重复的流水会被自动跳过。")

        self.batch_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + (sha[:6] or "nohash")
        self._batch_seen_keys.clear()
        self._batch_seen_fp.clear()
        self.conn.execute(
            "INSERT INTO batch (batch_id, source, path, origin, file_sha1, file_size,"
            " imported_at, parsed, declared) VALUES (?,?,?,?,?,?,?,?,?)",
            (self.batch_id, source, path, origin, sha, size,
             datetime.now().isoformat(sep=" ", timespec="seconds"), parsed, declared),
        )
        self.conn.commit()
        return self.batch_id

    def add(self, txn: Txn) -> DedupResult:
        """判定并写入一笔交易。identical 直接跳过，suspect 写入但标记待确认。"""
        txn.batch_id = self.batch_id
        verdict = classify(txn, self)

        key = txn.match_key()
        fp = txn.fingerprint()

        # 批内去重：同一个文件里出现两次同样的流水
        if verdict.verdict == "new" and key and key in self._batch_seen_keys:
            verdict = DedupResult(verdict="identical", reason=f"本次导入内重复（{key}）")
        if verdict.verdict == "new" and fp in self._batch_seen_fp:
            verdict = DedupResult(
                verdict="suspect_exact",
                reason="与本次导入内的另一笔流水内容完全一致",
            )

        if verdict.is_duplicate_skip:
            return verdict

        review = verdict.verdict if verdict.needs_review else ""
        row = txn.to_row()
        self.conn.execute(
            "INSERT OR IGNORE INTO txn (source, txn_id, merchant_order_id, ts, amount,"
            " direction, counterparty, item, method, status, note, category, match_key,"
            " fingerprint, batch_id, review, review_note, dup_of, raw)"
            " VALUES (:source,:txn_id,:merchant_order_id,:ts,:amount,:direction,"
            " :counterparty,:item,:method,:status,:note,:category,:match_key,"
            " :fingerprint,:batch_id,:review,:review_note,:dup_of,:raw)",
            {
                **row,
                "match_key": key,
                "fingerprint": fp,
                "review": review,
                "review_note": verdict.reason,
                "dup_of": verdict.existing_id,
            },
        )

        if key:
            self._batch_seen_keys.add(key)
        self._batch_seen_fp[fp] = txn.ts.isoformat()
        return verdict

    def finish_batch(self, parsed: int, declared: Optional[int],
                     inserted: int, identical: int, suspect: int,
                     skipped: int, note: str = "") -> None:
        self.conn.execute(
            "UPDATE batch SET parsed=?, declared=?, inserted=?, identical=?,"
            " suspect=?, skipped=?, note=? WHERE batch_id=?",
            (parsed, declared, inserted, identical, suspect, skipped, note, self.batch_id),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # 人工复核
    # ------------------------------------------------------------------

    def list_review(self, limit: int = 200) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM txn WHERE review LIKE 'suspect%' ORDER BY ts LIMIT ?", (limit,)
        ).fetchall()

    def review_counterpart(self, row) -> Optional[sqlite3.Row]:
        if row["dup_of"]:
            return self.conn.execute(
                "SELECT * FROM txn WHERE id = ?", (row["dup_of"],)
            ).fetchone()
        rows = self.conn.execute(
            "SELECT * FROM txn WHERE fingerprint = ? AND id != ? LIMIT 1",
            (row["fingerprint"], row["id"]),
        ).fetchone()
        return rows

    def resolve(self, txn_id: int, decision: str, note: str = "") -> bool:
        """人工裁定。decision = confirmed_dup | confirmed_ok。"""
        if decision not in ("confirmed_dup", "confirmed_ok"):
            raise ValueError("decision 只能是 confirmed_dup 或 confirmed_ok")
        cur = self.conn.execute(
            "UPDATE txn SET review = ?, review_note = ? WHERE id = ?",
            (decision, note, txn_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # 报表
    # ------------------------------------------------------------------

    def month_summary(self, month: str) -> dict:
        """month 形如 2026-09。只统计方向为 expense 的交易。"""
        rows = self.conn.execute(
            "SELECT direction, category, COUNT(*) AS n, SUM(CAST(amount AS REAL)) AS total "
            "FROM txn WHERE substr(ts,1,7) = ? AND review != 'confirmed_dup' "
            "GROUP BY direction, category ORDER BY direction, total DESC",
            (month,),
        ).fetchall()
        return {"month": month, "rows": rows}

    def overall(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n,"
            " SUM(CASE WHEN review LIKE 'suspect%' THEN 1 ELSE 0 END) AS suspect,"
            " SUM(CASE WHEN review = 'confirmed_dup' THEN 1 ELSE 0 END) AS confirmed_dup,"
            " SUM(CASE WHEN direction = 'expense' THEN CAST(amount AS REAL) ELSE 0 END) AS expense,"
            " MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM txn"
        ).fetchone()
        batches = self.conn.execute("SELECT COUNT(*) AS n FROM batch").fetchone()
        return {"txn": row, "batches": batches["n"]}
