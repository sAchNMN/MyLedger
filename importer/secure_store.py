"""加密的本地存储：明文一秒都不落盘。

做法：
    SQLite 全程跑在内存（`:memory:`）。退出时 `conn.serialize()` 拿到整个库的字节，
    用 Windows DPAPI 加密后写盘。下次打开：读密文 → DPAPI 解密 → `conn.deserialize()`。

为什么选这个方案：

- **明文零落盘。** 不是「加密文件 + 运行时解密到临时文件」那一套——
  那套会在硬盘上留下明文副本，恰好是要防的东西。内存方案没有这个窗口。
- **不引入任何第三方依赖。** 没有 SQLCipher，也没有自己手写的 AES。
  DPAPI（`CryptProtectData`）是操作系统提供的，绑定当前 Windows 用户。
- **不需要你额外再记一个密码。** 你已经明确要求 zip 密码用完即忘，
  再让你记一个数据库密码是自相矛盾的。

代价（必须知道）：

    密钥由你的 Windows 用户账户派生，所以**重装系统或换 Windows 账户后，这个库解不开**。
    这是「不用记密码」的必然结果，不是 bug。
    兜底手段是 vault 里的 `raw\\` 原始账单归档——真出事了重新导一遍就行。
    如果你更看重「换机器也能带走数据」，那需要改用口令派生密钥（Argon2/scrypt），
    代价是每次打开都要输口令。

用法：

    from importer.secure_store import open_encrypted

    with open_encrypted(vault_dir) as ledger:
        ...  # 退出 with 时自动加密落盘

"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Callable, Iterator, Optional

from . import credentials
from .store import Ledger

ENCRYPTED_NAME = "ledger.db.enc"
PLAINTEXT_NAME = "ledger.db"


def encrypted_path(vault_dir: str) -> str:
    return os.path.join(vault_dir, ENCRYPTED_NAME)


def plaintext_path(vault_dir: str) -> str:
    return os.path.join(vault_dir, PLAINTEXT_NAME)


def _atomic_write(path: str, data: bytes) -> None:
    """先写临时文件再 rename。

    直接覆盖原文件的话，写到一半断电就会同时失去新旧两份数据。
    rename 在同一个卷上是原子的，要么旧的要么新的，不存在中间态。
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _require_serialize() -> None:
    if not hasattr(sqlite3.Connection, "serialize"):
        raise RuntimeError(
            "当前 Python 的 sqlite3 不支持 serialize()/deserialize()，需要 3.11 及以上。"
            f"当前版本：{sqlite3.sqlite_version_info}（Python 版本过低）"
        )


def _memory_conn() -> sqlite3.Connection:
    """建一个内存库连接。

    check_same_thread=False 是必须的：review 的网页界面用 ThreadingHTTPServer，
    每个请求在**新线程**里处理，而连接是在主线程建的。
    Python 的 sqlite3 默认禁止跨线程使用，不关掉这个检查，点一次按钮就抛
    「SQLite objects created in a thread can only be used in that same thread」。

    关掉检查意味着调用方要自己保证不并发访问——review_ui 里用一把锁守住了所有
    账本操作。这是个内存库、只有本机一个小服务在用，一把锁足够。

    （如果哪天要给它加多线程写入，正确做法是每线程一个连接 + 共享缓存，
    而不是继续靠 check_same_thread=False 硬扛。）
    """
    return sqlite3.connect(":memory:", check_same_thread=False)


def save(ledger: Ledger) -> int:
    """把内存里的库序列化 → 加密 → 落盘。返回密文字节数。"""
    raw = ledger.conn.serialize()
    blob = credentials.protect(raw)
    _atomic_write(ledger.db_path, blob)
    return len(blob)


def open_ledger(vault_dir: str, *, warn: Callable[[str], None] = print) -> Ledger:
    """打开（或新建）加密账本。"""
    _require_serialize()
    enc = encrypted_path(vault_dir)

    if os.path.exists(enc):
        with open(enc, "rb") as fh:
            blob = fh.read()
        try:
            raw = credentials.unprotect(blob)
        except OSError as exc:
            raise RuntimeError(
                f"账本解不开：{enc}\n"
                f"  这个库的密钥绑定当前 Windows 用户。换了机器或重装系统后就会这样。\n"
                f"  数据没有丢——vault\\raw\\ 里的原始账单还在，重新导入一次即可。\n"
                f"  （原始错误：{exc}）"
            ) from exc
        conn = _memory_conn()
        conn.deserialize(raw)
        return Ledger(conn, enc, init_schema=False)

    # 没有加密库，但存在旧的明文库 —— 迁移一次
    plain = plaintext_path(vault_dir)
    if os.path.exists(plain):
        warn(f"发现明文库：{plain}")
        warn("  正在迁移到加密存储（明文文件不会被自动删除，请你手动处理它）。")
        src = sqlite3.connect(plain)
        try:
            # 关键一步：先把 WAL 落回主库。
            #
            # WAL 模式下 serialize() 出来的镜像，文件头的读写版本字节仍标着 WAL。
            # 这个镜像装进内存库后，SQLite 会去找根本不存在的 -wal 文件，
            # 于是每一个查询都报 "unable to open database file"——
            # 而且 deserialize() 本身不报错，错误延迟到第一次查询才出现，极难定位。
            #
            # 纯内存往返（:memory: → serialize → deserialize）不会触发，
            # 因为内存库默认不是 WAL。只有从 WAL 文件库迁移时才会撞上。
            # 顺带一提：这条 PRAGMA 也会把 -wal 里的未提交内容并回主库，
            # 不加的话迁移会丢掉最近一次未 checkpoint 的数据。
            src.execute("PRAGMA journal_mode = DELETE")
            raw = src.serialize()
        finally:
            src.close()
        _atomic_write(enc, credentials.protect(raw))
        conn = _memory_conn()
        conn.deserialize(raw)
        ledger = Ledger(conn, enc, init_schema=False)
        warn(f"  已迁移到 {enc}")
        warn(f"  ！请手动删除明文库 {plain}（以及同目录的 -wal / -shm 文件）")
        return ledger

    # 全新账本
    conn = _memory_conn()
    ledger = Ledger(conn, enc, init_schema=True)
    os.makedirs(vault_dir, exist_ok=True)
    return ledger


@contextmanager
def open_encrypted(vault_dir: str, *, save_on_exit: bool = True,
                   warn: Callable[[str], None] = print) -> Iterator[Ledger]:
    """开→用→自动加密落盘并关闭。

    用 with 是为了不给「忘了保存」留缝隙：无论正常结束还是抛异常，
    都会把库写回去（已提交的批次不会丢）。
    `save_on_exit=False` 用于 --dry-run：只读不写回。
    """
    ledger = open_ledger(vault_dir, warn=warn)
    try:
        yield ledger
    finally:
        try:
            if save_on_exit:
                size = save(ledger)
                print(f"账本已加密保存（{size / 1024:.1f} KB）：{ledger.db_path}")
        finally:
            ledger.conn.close()


def status(vault_dir: str) -> dict:
    """给 CLI 看的存储状态。"""
    enc = encrypted_path(vault_dir)
    plain = plaintext_path(vault_dir)
    info = {
        "vault": vault_dir,
        "encrypted_path": enc,
        "encrypted_exists": os.path.exists(enc),
        "encrypted_size": os.path.getsize(enc) if os.path.exists(enc) else 0,
        "plaintext_exists": os.path.exists(plain),
        "plaintext_path": plain,
        "wal_files": [
            f"{plain}{suffix}" for suffix in ("-wal", "-shm")
            if os.path.exists(f"{plain}{suffix}")
        ],
    }
    return info
