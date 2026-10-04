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

明文库迁移（ML-009）
--------------------
vault 里发现旧版留下的 `ledger.db` 时，会自动迁移一次，但**不是**
「加密写出去就算完」：

    1. 先核对源文件确实是账本（有 txn / batch 表），否则拒绝，一个字都不写
    2. 迁移前算一份指纹（表清单 + 笔数 + 金额合计 + 时间范围 + 最大 id）
    3. 写完密文，立刻把它当成陌生文件重新打开，再算一次指纹
    4. 两次指纹不一致 → 删掉这份密文、原样保留明文、抛 MigrationError
    5. 一致 → 把明文三件套（含 -wal / -shm）移入
       `_plaintext_quarantine\\<时间戳>\\`（移动，不删除）

之后每次打开账本都会检查一遍明文残留并强提醒 —— 安全提示只提醒一次等于没提醒。

"""

from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Callable, Iterator, Optional

from . import credentials
from .store import Ledger

ENCRYPTED_NAME = "ledger.db.enc"
PLAINTEXT_NAME = "ledger.db"

# 迁移成功后，明文副本挪到这里（移动，不删除）。
QUARANTINE_DIRNAME = "_plaintext_quarantine"

# 一个 MyLedger 账本必须有的表。迁移前先核对，见 _require_ledger_schema。
REQUIRED_TABLES = ("txn", "batch")


class MigrationError(RuntimeError):
    """明文库迁移没做成。

    抛出这个异常之前，本次运行写出去的密文一定已经被撤掉了：
    半成品密文比没有密文更危险 —— 下次 open_ledger 会优先读它，
    然后告诉你「账本解不开」，而真正的数据其实还躺在明文里。
    """


def encrypted_path(vault_dir: str) -> str:
    return os.path.join(vault_dir, ENCRYPTED_NAME)


def plaintext_path(vault_dir: str) -> str:
    return os.path.join(vault_dir, PLAINTEXT_NAME)


def plaintext_artifacts(vault_dir: str) -> list[str]:
    """vault 里还留着的明文副本：主文件 + 附属文件。

    -wal / -shm 必须一起看。只盯主文件的话，最近一次还没 checkpoint 的改动
    仍然留在 -wal 里 —— 那份东西一样能被读出来，而且更像「没人注意的角落」。
    """
    plain = plaintext_path(vault_dir)
    return [p for p in (plain, f"{plain}-wal", f"{plain}-shm") if os.path.exists(p)]


def quarantine_dir(vault_dir: str) -> str:
    """隔离目录：迁移后明文副本的落脚点。"""
    return os.path.join(vault_dir, QUARANTINE_DIRNAME)


def find_residue(vault_dir: str) -> list[str]:
    """vault 里所有还留着的明文副本：根目录下的三件套 + 隔离目录里的。

    隔离目录必须一起算。「挪了个位置」不等于「数据消失了」，
    而恰恰是这一步最容易骗过自己：文件不在原来的地方了，
    于是以为已经处理过。
    """
    found = list(plaintext_artifacts(vault_dir))
    box = quarantine_dir(vault_dir)
    if os.path.isdir(box):
        for name in sorted(os.listdir(box)):
            entry = os.path.join(box, name)
            if os.path.isdir(entry):
                found.extend(sorted(os.path.join(entry, inner)
                                    for inner in os.listdir(entry)))
            else:
                found.append(entry)
    return found


def quarantine_plaintext(vault_dir: str,
                         warn: Callable[[str], None]) -> list[str]:
    """把明文三件套移进 `vault\\_plaintext_quarantine\\<时间戳>\\`，返回新路径。

    为什么是移动而不是删除：密文刚刚校验过（见 _verify_encrypted_copy），
    理论上可以删了 —— 但「校验逻辑说它没问题」和「你敢替用户删掉他的账本」
    是两件事，校验逻辑本身也可能有 bug。移动同样立刻消除了
    「下次启动读到明文」的风险，而且真出事时数据还在。
    真正的删除交给用户确认之后自己做。
    """
    paths = plaintext_artifacts(vault_dir)
    if not paths:
        return []

    base = os.path.join(quarantine_dir(vault_dir),
                        datetime.now().strftime("%Y%m%d-%H%M%S"))
    target, suffix = base, 1
    while os.path.exists(target):          # 同一秒里连迁两次也不能互相覆盖
        suffix += 1
        target = f"{base}.{suffix}"
    os.makedirs(target)

    moved: list[str] = []
    for path in paths:
        dest = os.path.join(target, os.path.basename(path))
        try:
            shutil.move(path, dest)
            moved.append(dest)
        except OSError as exc:
            warn(f"！移动明文副本失败：{path}（{exc}）")
    return moved


def _warn_residue(vault_dir: str, warn: Callable[[str], None]) -> list[str]:
    """每次打开账本都检查一遍明文残留，有就响亮地提醒。

    为什么必须每次提醒（ML-009）：原来的实现只在迁移那一次打印一句
    「明文文件不会被自动删除，请你手动处理它」，之后就再也没人提过。
    而人不会因为一句提示就立刻去删文件 —— 于是「迁移完成」实际变成了
    「明文永久留在那里，而且再也没人提醒」。安全提示只提醒一次等于没提醒。
    """
    residue = find_residue(vault_dir)
    if not residue:
        return []
    warn("！vault 里还有明文副本，账本等于没加密：")
    for path in residue:
        warn(f"    {path}")
    warn("  在删掉之前，任何能读到这个目录的人都能读到你的全部账目。")
    warn("  确认密文一切正常之后，用下面这条删掉它们"
         "（只删上面列的明文副本，不动加密账本）：")
    warn("    Remove-Item -LiteralPath "
         + ",".join(f"'{p}'" for p in residue) + " -Recurse -Force")
    return residue


def ledger_fingerprint(conn) -> dict:
    """一份能代表「这是同一本账」的摘要。

    迁移前算一次，把密文重新打开后再算一次，两边相等才算迁移成功。

    只用与扫描顺序无关的聚合值：表清单 + 笔数 + 金额合计 + 时间范围 + 最大 id。
    金额合计走 printf('%.2f', ...)：钱只到分，舍到两位小数之后，
    浮点求和的最后一位差异就不会造成假报警。
    """
    tables = sorted(row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"))
    # 一律按位置取值：源库是裸 sqlite3.connect()（行是元组），
    # 内存库的 row_factory 是 sqlite3.Row。用列名的话总有一边会炸。
    row = conn.execute(
        "SELECT COUNT(*) AS n,"
        " COALESCE(printf('%.2f', SUM(CAST(amount AS REAL))), '0.00') AS total,"
        " COALESCE(MIN(ts), '') AS first_ts,"
        " COALESCE(MAX(ts), '') AS last_ts,"
        " COALESCE(MAX(id), 0) AS max_id FROM txn").fetchone()
    batches = conn.execute("SELECT COUNT(*) FROM batch").fetchone()[0]
    return {
        "tables": tables,
        "txn": row[0],
        "total": row[1],
        "first_ts": row[2],
        "last_ts": row[3],
        "max_id": row[4],
        "batches": batches,
    }


def _require_ledger_schema(conn, path: str) -> None:
    """确认这个 sqlite 文件确实是 MyLedger 的账本。

    不核对就直接迁移的话，vault 里随便一个同名的 sqlite 文件都会被
    「加密归档」，而真正的账本可能还在别处 —— 密文看起来是成功了，
    打开却是空的。宁可在这里拒绝。
    """
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    missing = [name for name in REQUIRED_TABLES if name not in tables]
    if missing:
        raise MigrationError(
            f"{path} 里缺少账本该有的表：{'、'.join(missing)}。\n"
            f"  这看起来不是一个 MyLedger 账本，迁移已中止，没有写任何东西。\n"
            f"  如果它确实是你的账本（比如更早的版本），先把它挪到别处再手动处理。"
        )


def _verify_encrypted_copy(enc_path: str) -> dict:
    """把刚写出来的密文当成一个陌生文件重新打开，返回它的指纹。

    不重开一遍就不算验证过：protect() 返回只说明字节写出去了，
    不代表这些字节能变回一个能用的数据库。
    """
    with open(enc_path, "rb") as fh:
        blob = fh.read()
    conn = _memory_conn()
    try:
        conn.deserialize(credentials.unprotect(blob))
        _require_ledger_schema(conn, enc_path)
        return ledger_fingerprint(conn)
    finally:
        conn.close()


def _discard_failed_migration(enc_path: str, warn: Callable[[str], None]) -> None:
    """删掉本次刚写出来的密文。

    只在迁移分支里调用 —— 进入那个分支的前提正是「密文本来不存在」，
    所以删掉它等于把状态恢复成动手之前的样子。
    """
    try:
        os.remove(enc_path)
    except OSError as exc:
        warn(f"！删除校验失败的密文时出错：{exc}")
        warn(f"  请手动删掉 {enc_path}，否则下次启动会去解一份坏掉的密文。")


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
    # 序列化前显式提交。
    #
    # serialize() 序列化的是「已提交的数据库镜像」。少一次 commit，
    # 拿到的就是旧内容；紧接着 close() 又把未提交的事务回滚掉，
    # 改动凭空消失而且**不报错** —— 这类静默丢数据最难查。
    # 调用方大多会自己 commit，但把所有落盘路径都堵死在同一个地方更省心。
    ledger.conn.commit()
    raw = ledger.conn.serialize()
    blob = credentials.protect(raw)
    _atomic_write(ledger.db_path, blob)
    return len(blob)


def open_ledger(vault_dir: str, *, warn: Callable[[str], None] = print) -> Ledger:
    """打开（或新建）加密账本。"""
    _require_serialize()
    enc = encrypted_path(vault_dir)

    if os.path.exists(enc):
        # 加密账本已经在了，那明文的出现只可能是三种情况：
        # 迁移残留、用户自己拷回来的、从备份恢复出来的。
        # 三种都值得每次提醒一遍 —— 见 _warn_residue 的注释。
        _warn_residue(vault_dir, warn)
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
        warn("  正在迁移到加密存储……")
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
            _require_ledger_schema(src, plain)
            before = ledger_fingerprint(src)
            raw = src.serialize()
        finally:
            src.close()

        _atomic_write(enc, credentials.protect(raw))

        # ------------------------------------------------------------------
        # 写完不等于写对。
        #
        # 这一段是 ML-009 的核心。原来的流程到上一行就结束了，然后打印一句
        # 「明文文件不会被自动删除，请你手动处理它」—— 也就是说，在没有任何
        # 证据的情况下就宣布「迁移完成」。加解密、序列化、原子替换，任何一步
        # 出错都可能留下一份打不开或缺表的密文，而明文还躺在旁边，
        # 一切看起来正常。等你手滑把明文删了，才会发现备份是空的。
        #
        # 所以现在：把密文当陌生文件重开一遍，逐项核对。
        # 核对不过就撤销 —— 删掉这份密文、原样保留明文、抛错。
        # 半成品密文比没有密文更危险：下次启动会优先读它，然后说「账本解不开」。
        # ------------------------------------------------------------------
        try:
            after = _verify_encrypted_copy(enc)
        except Exception as exc:
            _discard_failed_migration(enc, warn)
            raise MigrationError(
                f"迁移校验没过：刚写出的 {enc} 打不开或不像账本"
                f"（{type(exc).__name__}: {exc}）。\n"
                f"  已经撤销本次迁移（删掉了那份密文），明文库原样保留：{plain}\n"
                f"  你的数据没有丢，也没有被动过，可以放心重试。"
            ) from exc

        if after != before:
            _discard_failed_migration(enc, warn)
            diff = "、".join(k for k in before if before.get(k) != after.get(k))
            raise MigrationError(
                f"迁移校验没过：密文里的内容和源库对不上（不一致的项：{diff}）。\n"
                f"  已经撤销本次迁移，明文库原样保留：{plain}\n"
                f"  这种情况下**不要**删明文，先把情况查清楚。"
            )

        conn = _memory_conn()
        conn.deserialize(raw)
        ledger = Ledger(conn, enc, init_schema=False)
        warn(f"  已迁移到 {enc}")
        warn(f"  校验通过：{before['txn']} 笔，合计 {before['total']} 元，"
             f"时间范围 {before['first_ts']} ~ {before['last_ts']}")

        # 校验过了才敢动明文。挪进隔离目录，不删除 —— 见函数注释。
        moved = quarantine_plaintext(vault_dir, warn)
        if moved:
            warn(f"  明文副本已移入隔离目录（{len(moved)} 个文件，没有删除）：")
            for path in moved:
                warn(f"    {path}")
            warn("  请先确认加密账本能正常打开；确认之后再删掉隔离目录，"
                 "否则账本等于没加密。")
        else:
            warn("（没有找到明文文件，可能是刚才已经被移走了）")
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
    artifacts = plaintext_artifacts(vault_dir)
    info = {
        "vault": vault_dir,
        "encrypted_path": enc,
        "encrypted_exists": os.path.exists(enc),
        "encrypted_size": os.path.getsize(enc) if os.path.exists(enc) else 0,
        "plaintext_exists": os.path.exists(plain),
        "plaintext_path": plain,
        "plaintext_artifacts": artifacts,
        "wal_files": [p for p in artifacts if p != plain],
    }
    return info
