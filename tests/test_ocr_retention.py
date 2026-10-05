"""OCR 产物的保留期限（ML-011）。

为什么必须有这个文件：

    OCR 会留下三份中间文件：`<名字>.raw.txt` / `.rows.tsv` / `.ocr.log`。
    它们的身份很尴尬 —— 是账单明文的副本，却**不在账本里**：不加密、
    不会被 recompute 清点、不在做任何写入前自动备份的那条链路上，
    也没有人记得它们还在。

    换句话说，这是 vault 里最容易被人忘掉的一份明文。所以给它一个期限：
    跑完 OCR 顺手把超过 N 天的移走。

    为什么是「顺手」而不是单独一条命令：单独的命令没人会记得跑 ——
    这跟备份轮转是同一个道理（见 test_backup_rotation.py）。
    清理必须挂在会发生的动作之后，而不是靠记得。

这里钉住五件事：

    1. 超过期限的**确实**被移走，没到期的一个都不动
    2. 移走的去了 `%TEMP%`，没有真的删掉
    3. 只认那三种后缀，旁边的文件（比如待导入的行 CSV）不能动
    4. keep_days=0 表示不清理（给「我想自己决定」留个口）
    5. 单个文件移不动不影响别人，也**不能**把异常抛给调用方

用法：<venv>\\Scripts\\python.exe tests/test_ocr_retention.py
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

DAY = 86400


def check(name: str, got, want) -> None:
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: 期望 {want!r}，实际 {got!r}")
    print(f"  [{'✓' if ok else '✗'}] {name:<46} -> {got!r}")


def touch(path: str, days_ago: float) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("合成内容\n")
    stamp = time.time() - days_ago * DAY
    os.utime(path, (stamp, stamp))


def rotation_dir() -> str:
    return os.path.join(tempfile.gettempdir(), fixups.OCR_ROTATION_DIRNAME)


def snapshot() -> set:
    try:
        return set(os.listdir(rotation_dir()))
    except OSError:
        return set()


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="myledger-ocr-")
    before = snapshot()
    try:
        return run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            folder = rotation_dir()
            for name in snapshot() - before:
                os.remove(os.path.join(folder, name))
        except OSError:
            pass


def run(tmp: str) -> int:
    ocr = os.path.join(tmp, "ocr")
    os.makedirs(ocr)

    print()
    print("=" * 78)
    print("1. 到期的移走，没到期的一个都不动")
    print("=" * 78)
    touch(os.path.join(ocr, "A.raw.txt"), days_ago=0.5)     # 今天
    touch(os.path.join(ocr, "A.rows.tsv"), days_ago=3)      # 一周内
    touch(os.path.join(ocr, "A.ocr.log"), days_ago=6.9)     # 临界内侧
    touch(os.path.join(ocr, "B.raw.txt"), days_ago=10)      # 过期
    touch(os.path.join(ocr, "B.rows.tsv"), days_ago=40)     # 很久以前
    touch(os.path.join(ocr, "B.ocr.log"), days_ago=90)

    before_dst = snapshot()
    moved = fixups.cleanup_ocr_artifacts(ocr, keep_days=7, emit=QUIET)
    check("移走了 3 份到期的", sorted(moved),
          ["B.ocr.log", "B.raw.txt", "B.rows.tsv"])
    check("没到期的原样留在 vault", sorted(os.listdir(ocr)),
          ["A.ocr.log", "A.raw.txt", "A.rows.tsv"])

    dst = rotation_dir()
    arrived = sorted(snapshot() - before_dst)
    check("到期产物确实去了 %TEMP%", arrived,
          ["B.ocr.log", "B.raw.txt", "B.rows.tsv"])
    for name in arrived:
        with open(os.path.join(dst, name), encoding="utf-8") as fh:
            check(f"  {name} 内容还在", fh.read().strip(), "合成内容")

    print()
    print("=" * 78)
    print("2. 只认那三种后缀，旁边的东西不能动")
    print("=" * 78)
    os.makedirs(ocr, exist_ok=True)
    touch(os.path.join(ocr, "keep.ocr.log"), days_ago=0.2)
    touch(os.path.join(ocr, "pending-import.csv"), days_ago=60)
    touch(os.path.join(ocr, "notes.md"), days_ago=60)
    moved = fixups.cleanup_ocr_artifacts(ocr, keep_days=7, emit=QUIET)
    check("这次一份都没清", moved, [])
    check("待导入的 CSV 还在", os.path.exists(os.path.join(ocr, "pending-import.csv")), True)
    check("随手放的笔记还在", os.path.exists(os.path.join(ocr, "notes.md")), True)

    print()
    print("=" * 78)
    print("3. keep_days=0 表示不清理")
    print("=" * 78)
    ocr2 = os.path.join(tmp, "ocr-off")
    os.makedirs(ocr2)
    touch(os.path.join(ocr2, "C.raw.txt"), days_ago=100)
    moved = fixups.cleanup_ocr_artifacts(ocr2, keep_days=0, emit=QUIET)
    check("一份都没动", moved, [])
    check("文件还在原位", len(os.listdir(ocr2)), 1)

    print()
    print("=" * 78)
    print("4. 目录不存在 / 文件被占用：都不许把异常抛上去")
    print("=" * 78)
    check("目录不存在时静静返回",
          fixups.cleanup_ocr_artifacts(os.path.join(tmp, "nothing-here"),
                                       keep_days=7, emit=QUIET), [])

    ocr3 = os.path.join(tmp, "ocr-busy")
    os.makedirs(ocr3)
    touch(os.path.join(ocr3, "D.raw.txt"), days_ago=20)
    touch(os.path.join(ocr3, "E.raw.txt"), days_ago=20)
    real_move = shutil.move
    try:
        def half_broken_move(src, dst, *a, **k):
            if src.endswith("D.raw.txt"):          # 模拟被别的进程占着
                raise PermissionError("文件正在使用")
            return real_move(src, dst, *a, **k)
        shutil.move = half_broken_move             # type: ignore[assignment]
        moved = fixups.cleanup_ocr_artifacts(ocr3, keep_days=7, emit=QUIET)
    except Exception as exc:                       # noqa: BLE001
        FAILURES.append(f"清理过程抛了异常: {exc!r}")
        moved = []
    finally:
        shutil.move = real_move                    # type: ignore[assignment]
    check("没抛异常，且能挪的都挪了", sorted(moved), ["E.raw.txt"])
    check("挪不动的留在原地", os.path.exists(os.path.join(ocr3, "D.raw.txt")), True)

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
