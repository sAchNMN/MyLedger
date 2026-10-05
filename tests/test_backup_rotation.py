"""vault 备份轮转的测试。

为什么必须有这个文件：

    写库前自动备份是个好习惯，但它原本**只进不出** —— 每跑一次 CLI
    就在 vault 里留一份 `ledger.db.enc.backup-<时间戳>`，实测一天堆到
    15 份。手工清一次解决不了：下一次跑 CLI 又长回来。

    所以轮转必须挂在每次备份之后自动发生。这里钉住四件事：

        1. 超过限度的**确实**会被挪走，且挪走的是最早的
        2. 被挪走的没有消失，去了 `%TEMP%` —— 本项目「移动代替删除」的纪律
        3. 轮转再怎么失败，**都不能挡住已经成功的写库**
           （最要紧的一条：备份已经落地了，轮转只是善后。
            在这里抛异常会让用户以为写库失败，重跑一次更乱）
        4. 只有 `<账本名>.backup-` 前缀的文件才动，
           主文件和旁边无关的邻接文件一根毫毛都不能碰

用法：<venv>\\Scripts\\python.exe tests/test_backup_rotation.py
返回码 0 表示全部通过。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from importer import fixups  # noqa: E402

FAILURES: list[str] = []

QUIET = lambda *_a, **_k: None  # noqa: E731


def check(name: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: 期望 {want!r}，实际 {got!r}")
    print(f"  [{'✓' if ok else '✗'}] {name:<46} -> {got!r}")


def write(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def backups_in(vault: str) -> list[str]:
    return sorted(n for n in os.listdir(vault) if ".backup-" in n)


def snapshot_rotation_dir() -> set:
    try:
        return set(os.listdir(fixups._rotation_dir()))
    except OSError:
        return set()


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="myledger-rotate-")
    rotation_before = snapshot_rotation_dir()
    try:
        return run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        # 本测试往 %TEMP% 里挪的东西测完擦掉，别留垃圾
        try:
            folder = fixups._rotation_dir()
            for name in snapshot_rotation_dir() - rotation_before:
                os.remove(os.path.join(folder, name))
        except OSError:
            pass


def run(tmp: str) -> int:
    print()
    print("=" * 78)
    print("基本盘：备份本身还得是对的（不能为了轮转把备份搞坏）")
    print("=" * 78)
    vault = os.path.join(tmp, "vault")
    os.makedirs(vault)
    enc = os.path.join(vault, "ledger.db.enc")
    write(enc, "原始密文")
    target = fixups.backup(enc, emit=QUIET)
    check("备份内容和原件一致", read(target), "原始密文")
    check("这份新的留在 vault 里", os.path.exists(target), True)
    check("主文件还在", os.path.exists(enc), True)
    check("vault 里只有这一份备份", backups_in(vault), [os.path.basename(target)])

    print()
    print("=" * 78)
    print("超过限度的要轮走：再跑 5 次，只允许留下最近 3 份")
    print("=" * 78)
    rotation_before = snapshot_rotation_dir()
    stamps = [os.path.basename(target)]
    for index in range(5):
        # 文件名里的时间戳是秒级的，不跨秒就会撞名、互相覆盖，
        # 数量反而不对 —— 这个坑在 batch_id 上踩过一次。
        # 放在循环开头是为了保证跟上一份也不在同一秒。
        time.sleep(1.05)
        write(enc, f"密文-{index:03d}")
        stamps.append(os.path.basename(fixups.backup(enc, emit=QUIET)))

    kept = backups_in(vault)
    check("一共造了 6 份", len(set(stamps)), 6)
    check("vault 里只剩 3 份", len(kept), 3)
    check("留下的是最新的 3 份", kept, sorted(set(stamps))[-3:])
    check("最早的几份已经离开 vault", [n for n in sorted(set(stamps))[:3]
                                if n in kept], [])

    rotation = fixups._rotation_dir()
    arrived = sorted(snapshot_rotation_dir() - rotation_before)
    check("轮出去的确实到了 %TEMP%", len(arrived), 3)
    check("轮出的是最早的那些", arrived, sorted(set(stamps))[:3])
    check("没有真的删掉（vault + 临时目录 = 全部）",
          sorted(set(kept) | set(arrived)), sorted(set(stamps)))
    for name in arrived:
        check(f"  {name[-14:]} 内容没坏",
              os.path.getsize(os.path.join(rotation, name)) > 0, True)

    print()
    print("=" * 78)
    print("keep=0 表示不轮转：保持原来的行为，全留着")
    print("=" * 78)
    vault2 = os.path.join(tmp, "vault-keep-all")
    os.makedirs(vault2)
    enc2 = os.path.join(vault2, "ledger.db.enc")
    for index in range(5):
        write(enc2, f"密文-{index:03d}")
        fixups.backup(enc2, emit=QUIET, keep=0)
        time.sleep(1.05)
    check("5 次全留在 vault", len(backups_in(vault2)), 5)

    print()
    print("=" * 78)
    print("轮转失败不许挡住写库（最要紧的一条）")
    print("=" * 78)
    vault3 = os.path.join(tmp, "vault-rotation-broken")
    os.makedirs(vault3)
    enc3 = os.path.join(vault3, "ledger.db.enc")
    write(enc3, "待备份的内容")

    real_move = shutil.move
    try:
        def exploding_move(src, dst, *a, **k):     # 模拟「7-Zip 还占着句柄」
            raise PermissionError("另一个程序正在使用此文件")
        shutil.move = exploding_move               # type: ignore[assignment]
        got = fixups.backup(enc3, emit=QUIET, keep=1)
    except Exception as exc:                       # noqa: BLE001
        FAILURES.append(f"轮转失败时 backup 抛了异常: {exc!r}")
        got = ""
    finally:
        shutil.move = real_move                    # type: ignore[assignment]

    check("backup 没有把异常抛给调用方", got != "", True)
    check("备份仍然落下来了", os.path.exists(got), True)
    check("写库那一步没有受影响", len(backups_in(vault3)), 1)

    print()
    print("=" * 78)
    print("边界：只动自己前缀的文件")
    print("=" * 78)
    vault4 = os.path.join(tmp, "vault-mixed")
    os.makedirs(vault4)
    enc4 = os.path.join(vault4, "ledger.db.enc")
    neighbour = os.path.join(vault4, "notes.txt")
    write(enc4, "密文")
    write(neighbour, "和账本无关的文件")
    for index in range(4):
        write(enc4, f"密文-{index:03d}")
        fixups.backup(enc4, emit=QUIET, keep=2)
        time.sleep(1.05)
    check("只有 2 份备份", len(backups_in(vault4)), 2)
    check("主文件没被挪走", read(enc4), "密文-003")
    check("旁边的无关文件没被挪走", os.path.exists(neighbour), True)

    print()
    print("=" * 78)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
