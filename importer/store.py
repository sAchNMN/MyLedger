"""SQLite 存储层。

只有一个数据库文件，放在 vault 里（仓库外）。

raw 列放的是**来源信息 + 行号 + 形状摘要**，不是整行原文（ML-010）。
它能回答「这笔是从哪份文件的第几行读出来的、那一行有几个字段」，
但读不出商户名、金额或账号 —— 那些信息该在的地方是 txn 的各列，
而不是一份悄悄多出来的原始副本。

review 字段记录每笔交易的审核状态：
    ''             正常，无需处理
    suspect_exact  疑似重复（内容指纹完全一致）
    suspect_near   疑似重复（金额+时间+对方相似）
    confirmed_dup  人工确认为重复
    confirmed_ok   人工确认为独立交易（不再提示）

batch.status 记录一次导入走到哪一步了（见文件末尾的「批次状态」一节）。
刻意不用一个 ok 布尔：导入是逐笔写入的，循环跑完 ≠ 写完了。
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
    status       TEXT NOT NULL DEFAULT 'started',
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
    platform_category TEXT DEFAULT '',
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

-- 渠道记忆：形状 → 渠道。用户手动指定一次，之后同类文件自动认。
-- 键是「形状」（ext:csv / zip:pdf）而不是文件名，因为银行账单文件名
-- 是每月都变的流水号，按文件名记等于每个月都要重指定一次。详见 sourcehint.py。
CREATE TABLE IF NOT EXISTS source_hint (
    hint_key   TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
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


# --------------------------------------------------------------------------
# 批次状态
#
# 为什么批次要有状态，而不是一个 ok 布尔（ML-006）：
#
#     导入是逐笔写入的，单行失败会记下来继续跑 —— 循环跑完不代表写完了。
#     一旦把「程序没崩」当成「业务成功」，源文件就会被移进 _done/，
#     失败的那几笔从此不再重试：报表上永远少这几笔，而且全程没有任何报错。
#     这种事比直接崩掉危险得多，因为没人会去查。
#
#     ok: bool 只有一个 bit，表达不了「98 成 2 败」。所以这里给出显式状态，
#     并把最关键的规则做成硬约束：**archived 只能从 committed 来**。
#
#     started ──→ writing ──→ committed ──→ archived
#                   │
#                   └──→ partial_failed ──→ writing（补完失败行后重试）
#                   └──→ failed
#
#     `writing` 是可选中间态（小文件可以 started → committed 一次写完），
#     但 partial_failed / failed / started 都不能直接跳到 archived。
#
#     老库（没有 status 列）在打开时自动补列，历史批次一律视为 started。
# --------------------------------------------------------------------------

BATCH_STATUSES = (
    "started",          # 批次行刚建好，还没开始写交易
    "writing",          # 正在逐笔写入
    "partial_failed",   # 有行写失败，批次不完整，源文件必须留在原地
    "committed",        # 全部写成功，可以归档
    "failed",           # 整批失败（解析中断、写库异常）
    "archived",         # 源文件已归档，终态
)

_BATCH_TRANSITIONS = {
    "started": ("writing", "partial_failed", "committed", "failed"),
    "writing": ("committed", "partial_failed", "failed"),
    "partial_failed": ("writing", "committed", "failed"),
    "committed": ("archived",),
    "failed": ("writing",),
    "archived": (),
}


def _ensure_batch_status_column(conn: sqlite3.Connection) -> bool:
    """给老库补 status 列。补了就返回 True。

    只有 ALTER 一条路：加密账本是用 init_schema=False 打开的
    （schema 早就在密文里了），只在建新库时补列的话，
    现有用户的库永远补不上，ML-006 的约束对他们等于没有。

    老批次一律算 started：它们是在这个列存在之前写的，
    当时没有「部分失败」这个概念，不能凭空给它们扣一个 partial_failed。
    """
    columns = [row[1] for row in conn.execute("PRAGMA table_info(batch)")]
    if not columns or "status" in columns:
        return False
    conn.execute("ALTER TABLE batch ADD COLUMN status TEXT NOT NULL DEFAULT 'started'")
    conn.commit()
    return True


def _ensure_platform_category_column(conn: sqlite3.Connection) -> bool:
    """给老库补 platform_category 列（支付宝「交易分类」/ 微信「交易类型」原文）。

    为什么不能只写在 SCHEMA 里：
        和 batch.status / source_hint 完全同一个原因 —— 加密账本走
        init_schema=False，schema 早就在密文里了。只靠 executescript 的话，
        现有用户的库里根本没这一列，而 Txn.to_row() 是 asdict 出来的，
        INSERT 语句里带了 :platform_category，SQLite 会直接报
        「table txn has no column named platform_category」——
        整次导入崩掉，而崩的原因看起来跟「加了个平台分类」毫无关系。

    老行一律补空串：它们导入时根本没读这一列，事后编一个就是造假。
    空串在 platformmap 里查不到任何东西，行为和从前完全一致。
    """
    columns = [row[1] for row in conn.execute("PRAGMA table_info(txn)")]
    if not columns or "platform_category" in columns:
        return False
    conn.execute("ALTER TABLE txn ADD COLUMN platform_category TEXT DEFAULT ''")
    conn.commit()
    return True


def _ensure_source_hint_table(conn: sqlite3.Connection) -> bool:
    """给老库补 source_hint 表。补了就返回 True。

    为什么不能只写在 SCHEMA 里：
        加密账本走的是 init_schema=False（schema 早就在密文里了），
        新加的表在老用户的密文里根本不存在。只靠 executescript 的话，
        页面会直接报「no such table: source_hint」—— 整页崩掉，
        而崩溃原因看起来跟「渠道记忆」毫无关系，很难查。

    CREATE TABLE IF NOT EXISTS 是幂等的，每次打开都调也只多一次查询。
    """
    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "source_hint" in names:
        return False
    conn.execute(
        "CREATE TABLE IF NOT EXISTS source_hint ("
        " hint_key TEXT PRIMARY KEY, source TEXT NOT NULL, decided_at TEXT NOT NULL)"
    )
    conn.commit()
    return True


class Ledger:
    """账本。所有写操作都经过去重判定。

    刻意接受一个已经建好的 sqlite 连接，而不是自己按路径打开：
    加密存储（secure_store.py）需要把库整个装进内存操作，
    最后再序列化加密落盘，明文一秒都不碰硬盘。
    如果这里写死「按路径打开文件」，那条路就走不通了。
    """

    def __init__(self, conn: sqlite3.Connection, db_path: str = "",
                 init_schema: bool = True):
        self.conn = conn
        self.db_path = db_path
        self.conn.row_factory = sqlite3.Row
        if init_schema:
            self.conn.executescript(SCHEMA)
            self.conn.commit()
        # 补列补表放在 init_schema 分支外面：init_schema=False 的那条路
        # （加密账本、明文迁移）才是现有用户真正走的路径，见函数注释。
        _ensure_batch_status_column(self.conn)
        _ensure_source_hint_table(self.conn)
        _ensure_platform_category_column(self.conn)
        self._batch_seen_keys: set[str] = set()
        self._batch_seen_fp: dict[str, str] = {}
        self.batch_id = ""

    # ------------------------------------------------------------------
    @classmethod
    def open_plaintext(cls, db_path: str) -> "Ledger":
        """明文库。只用于迁移旧数据和自动化测试，正常运行一律走加密存储。

        刻意不启用 WAL：WAL 模式下 `serialize()` 出来的镜像头会带 WAL 标记，
        装进内存库后会报「unable to open database file」（见 secure_store）。
        明文库是过渡形态，不需要 WAL 带来的并发好处。
        """
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode = DELETE")
        return cls(conn, db_path)

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

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

        # batch_id = 秒级时间戳 + 文件 sha 前 6 位。
        #
        # 为什么还要加序号后缀：部分失败之后马上重跑（或者自动化在同一秒里
        # 跑两次同一个文件）算出来的 id 完全一样，INSERT 直接撞 batch 的主键，
        # 整个导入在 start_batch 就崩掉 —— 源文件永远处理不了。
        # 这条是修 ML-006 时被测试抓出来的：状态机允许重试，id 却不允许。
        # 后缀用可读的 .2 / .3 而不是随机串：出问题时一眼能看出是同一秒的第二次。
        base = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + (sha[:6] or "nohash")
        self.batch_id = base
        suffix = 1
        while self.conn.execute(
                "SELECT 1 FROM batch WHERE batch_id = ?", (self.batch_id,)).fetchone():
            suffix += 1
            self.batch_id = f"{base}.{suffix}"
        self._batch_seen_keys.clear()
        self._batch_seen_fp.clear()
        self.conn.execute(
            "INSERT INTO batch (batch_id, source, path, origin, file_sha1, file_size,"
            " imported_at, parsed, declared, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self.batch_id, source, path, origin, sha, size,
             datetime.now().isoformat(sep=" ", timespec="seconds"), parsed, declared,
             "started"),
        )
        self.conn.commit()
        return self.batch_id

    # ------------------------------------------------------------------
    # 批次状态流转
    # ------------------------------------------------------------------

    def batch_status(self) -> str:
        """当前批次的 status。没有打开的批次就报错，不返回空字符串。"""
        if not self.batch_id:
            raise ValueError("还没有打开的批次（先调 start_batch）")
        row = self.conn.execute(
            "SELECT status FROM batch WHERE batch_id = ?", (self.batch_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"批次 {self.batch_id} 不在库里")
        return row["status"]

    def set_batch_status(self, status: str) -> str:
        """推进批次状态。非法流转抛 ValueError，且不改库。

        刻意做成「拒绝」而不是「纠正」：状态机是防漏记的最后一道闸，
        悄悄把 partial_failed 写成 committed 正是要防的事。
        重复设置同一个状态是空操作（重试路径会走到）。
        """
        if status not in BATCH_STATUSES:
            raise ValueError(
                f"不是合法的批次状态：{status!r}（可选：{'、'.join(BATCH_STATUSES)}）")
        current = self.batch_status()
        if status == current:
            return current
        allowed = _BATCH_TRANSITIONS.get(current, ())
        if status not in allowed:
            raise ValueError(
                f"批次状态不允许从 {current} 变成 {status}"
                f"（批次 {self.batch_id}；允许的下一个状态：{'、'.join(allowed) or '无，已是终态'}）")
        self.conn.execute(
            "UPDATE batch SET status = ? WHERE batch_id = ?", (status, self.batch_id))
        self.conn.commit()
        return status

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
            " direction, counterparty, item, method, status, platform_category, note,"
            " category, match_key,"
            " fingerprint, batch_id, review, review_note, dup_of, raw)"
            " VALUES (:source,:txn_id,:merchant_order_id,:ts,:amount,:direction,"
            " :counterparty,:item,:method,:status,:platform_category,:note,:category,:match_key,"
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
                     skipped: int, note: str = "",
                     status: str = "committed") -> None:
        """收尾：写统计 + 落最终状态。

        状态先过状态机再写统计 —— 非法流转必须在这里就炸，
        而不是先把统计写进去、状态留在半路上。
        """
        if status != self.batch_status():
            self.set_batch_status(status)
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

    # ------------------------------------------------------------------
    # 未分类盘点
    #
    # 「未分类占比不低」是这个项目已知的数据质量问题：规则靠关键词，
    # 总有认不出来的商户。要补规则/补人工指定，第一步得先看清
    # 「到底是哪些商户没分类、各占多少钱」——盲补关键词只会按倒葫芦起了瓢。
    # ------------------------------------------------------------------

    def unmatched_merchants(self, month: Optional[str] = None,
                            limit: int = 200) -> list[sqlite3.Row]:
        """按商户聚合未分类交易，笔数多的排前面。

        支出/收入/中性分开汇总：未分类里既有真消费，也有没归好类的进账，
        混成一个数会误导（生活费进账不是「这笔消费没分类」）。
        """
        where = ["category = '未分类'", "review != 'confirmed_dup'"]
        params: list = []
        if month:
            where.append("substr(ts,1,7) = ?")
            params.append(month)
        params.append(limit)
        return self.conn.execute(
            "SELECT counterparty AS merchant, COUNT(*) AS n,"
            " SUM(CASE WHEN direction='expense' THEN CAST(amount AS REAL) ELSE 0 END) AS expense,"
            " SUM(CASE WHEN direction='income'  THEN CAST(amount AS REAL) ELSE 0 END) AS income,"
            " SUM(CASE WHEN direction='neutral' THEN CAST(amount AS REAL) ELSE 0 END) AS neutral,"
            " SUM(CAST(amount AS REAL)) AS total,"
            " MIN(ts) AS first_ts, MAX(ts) AS last_ts"
            " FROM txn WHERE " + " AND ".join(where) +
            " GROUP BY counterparty ORDER BY n DESC, total DESC LIMIT ?",
            params,
        ).fetchall()

    def unmatched_totals(self, month: Optional[str] = None) -> dict:
        """未分类的总量，以及占同期支出的比例。"""
        where = ["category = '未分类'", "review != 'confirmed_dup'"]
        exp_where = ["direction = 'expense'", "review != 'confirmed_dup'"]
        params: list = []
        exp_params: list = []
        if month:
            where.append("substr(ts,1,7) = ?")
            params.append(month)
            exp_where.append("substr(ts,1,7) = ?")
            exp_params.append(month)

        row = self.conn.execute(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT counterparty) AS merchants,"
            " SUM(CASE WHEN direction='expense' THEN CAST(amount AS REAL) ELSE 0 END) AS expense,"
            " SUM(CASE WHEN direction='income'  THEN CAST(amount AS REAL) ELSE 0 END) AS income"
            " FROM txn WHERE " + " AND ".join(where),
            params,
        ).fetchone()
        expense_all = self.conn.execute(
            "SELECT SUM(CAST(amount AS REAL)) AS v FROM txn WHERE " + " AND ".join(exp_where),
            exp_params,
        ).fetchone()["v"] or 0.0

        return {
            "n": row["n"] or 0,
            "merchants": row["merchants"] or 0,
            "expense": row["expense"] or 0.0,
            "income": row["income"] or 0.0,
            "expense_all": expense_all,
        }

    def resolve(self, txn_id: int, decision: str, note: str = "") -> bool:
        """直接把某笔的复核标记设成 decision，返回是否命中一行。

        低层入口：不检查这条是不是「待复核」，也不看原来是什么状态。
        给终端 `review --resolve <id>` 用 —— 那里是人明确点名要改哪一条，
        而且终端会把目标记录完整打出来（金额、对方、当前状态）让人确认。

        网页界面请走 resolve_review()：那边按 id 提交，会被重放、被手写、
        被两个标签页同时发，必须有状态守卫。

        note 为空时**保留**原有的 review_note：那是「为什么怀疑重复」的判定依据，
        裁定之后依然有用，不该被顺手抹掉。
        """
        if decision not in ("confirmed_dup", "confirmed_ok"):
            raise ValueError("decision 只能是 confirmed_dup 或 confirmed_ok")
        if note:
            cur = self.conn.execute(
                "UPDATE txn SET review = ?, review_note = ? WHERE id = ?",
                (decision, note, txn_id),
            )
        else:
            cur = self.conn.execute(
                "UPDATE txn SET review = ? WHERE id = ?", (decision, txn_id))
        self.conn.commit()
        return cur.rowcount > 0

    def resolve_review(self, txn_id: int, decision: str) -> str:
        """网页复核界面的裁定入口，带状态守卫。返回一个状态码字符串：

            updated          刚刚改掉
            unchanged        已经是这个裁定（重复提交、双击、重放）
            not_found        没有这个 id
            not_reviewable   这条不是待复核记录
            already_decided  已经人工定过案了，不允许被另一个决定覆盖

        为什么界面这条必须守（ML-008）：
            原来的实现是 `UPDATE txn SET review = ? WHERE id = ?`，
            于「幂等重放」和「随便改一条正常记录」在代码里长得一模一样 ——
            而后果天差地别：把一笔正常消费标成 confirmed_dup，
            它会从所有报表里消失，而且没有任何痕迹。

            前端的按钮是盲发的：它只知道「我发了这个 id」。
            所以「这个 id 该不该改」只能由后端判断。
        """
        if decision not in ("confirmed_dup", "confirmed_ok"):
            raise ValueError("decision 只能是 confirmed_dup 或 confirmed_ok")

        row = self.conn.execute(
            "SELECT review FROM txn WHERE id = ?", (txn_id,)).fetchone()
        if row is None:
            return "not_found"

        current = row["review"] or ""
        if current == decision:
            return "unchanged"
        if current in ("confirmed_dup", "confirmed_ok"):
            return "already_decided"
        if not current.startswith("suspect"):
            return "not_reviewable"

        self.conn.execute("UPDATE txn SET review = ? WHERE id = ?", (decision, txn_id))
        self.conn.commit()
        return "updated"

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
