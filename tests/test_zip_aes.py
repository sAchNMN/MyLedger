"""WinZip AES 加密的 zip：标准库解不了，必须能降级到 7-Zip。

真实事故
--------
用户导入银行账单失败，密码明明输对了。日志里写的是
「压缩包 None 解不开：需要密码，或给定密码不对」。

两个问题叠在一起：
  1. **「压缩包 None」本身就是 bug。** `open_document` 用
     `zipfile.ZipFile(io.BytesIO(data))` 构造，`zf.filename` 拿到的是
     那个 BytesIO 对象，打印出来就是一个难看的地址；那次
     传进去的甚至不是字符串，所以显示成 `None`。
  2. **「需要密码，或给定密码不对」是误报。** 那个包根本不是
     传统 ZipCrypto，是 **WinZip AES-256**。`zipfile` 只支持 ZipCrypto，
     在 Python 3.13 上遇到 AES 包会抛一句含糊的「Bad password」——
     于是「算法不支持」被说成了「密码不对」。用户输一百遍正确密码
     都解不开，最后开始怀疑自己记错了密码。

     CPython 3.14 才给 zipfile 加了 AES 支持，本机是 3.13。

修法
----
新增 `importer/archive.py`：AES 包自动降级调本机 7-Zip（WinRAR 也留了位）。
标准库优先，只在它解不了时才走外部工具。

这个测试钉住四件事：
  1. `describe_encryption` 说得清用的什么加密、哪条路能解
  2. 传统 ZipCrypto 仍然走标准库（不能为了 AES 把主路径改坏）
  3. AES 包能真的解出内容，且解析成表格
  4. 缺 7-Zip / 密码错时，报错**指向真实原因**，不再说「压缩包 None」

AES 样本怎么造
--------------
`zipfile` 只能写不加密的包，所以加密样本必须用 7-Zip 造
（`-mem=AES256 -p...`）。找不到 7z 的机器上这三条用例会跳过，
但其余全部照跑 —— 不该因为造样本的工具不在就让整个测试文件空转。

用法：<venv>\\Scripts\\python.exe tests/test_zip_aes.py
返回码 0 表示全部通过。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from importer import archive  # noqa: E402
from importer.bank_pdf import extract_rows  # noqa: E402
from importer.sniff import open_document  # noqa: E402

FAILURES: list[str] = []
SKIPPED: list[str] = []

AES_PASSWORD = "TestAesZip-2026"


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n         得到 {got!r}\n         期望 {want!r}")
        FAILURES.append(label)


def skip(label: str, why: str) -> None:
    print(f"  skip {label}（{why}）")
    SKIPPED.append(label)


def make_pdf_bytes() -> bytes:
    """造一份带文字层的合成 PDF。列名用 ASCII —— 合成 PDF 里没有中文字体，
    中文会渲染成黑点，「表头里有某个中文词」这种断言就变成永远失败的假测试。
    这里要验的是「zip → PDF → 二维表」这条管道，不是字体。
    """
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    rows = [
        ("TXN_DATE", "CHANNEL", "REMARK", "COUNTERPARTY", "INCOME", "EXPENSE", "BALANCE"),
        ("2026-09-01", "POS", "SHOPPING", "ACME STORE", "", "35.20", "1200.00"),
        ("2026-09-02", "ATM", "WITHDRAW", "ATM CASH", "", "200.00", "1000.00"),
        ("2026-09-03", "TRANSFER", "SALARY", "ACME CORP", "5000.00", "", "6000.00"),
    ]
    y = 100
    for row in rows:
        x = 50
        for cell in row:
            page.insert_text((x, y), cell, fontsize=9)
            x += 110
        y += 18
    data = doc.tobytes()
    doc.close()
    return data


def write_aes_zip(directory: str, outer: str, inner: str,
                  payload: bytes, password: str = AES_PASSWORD) -> str | None:
    """用 7-Zip 造一个 WinZip AES-256 加密的 zip。失败返回 None。"""
    exe = archive._find_7z()
    if not exe:
        return None
    src = os.path.join(directory, "payload.pdf")
    with open(src, "wb") as fh:
        fh.write(payload)
    dest = os.path.join(directory, outer)
    try:
        proc = subprocess.run(
            [exe, "a", "-tzip", "-mem=AES256", f"-p{password}", "-y",
             f"-o{directory}", dest, src],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    os.remove(src)
    return dest if os.path.isfile(dest) else None


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------

def case_describe_encryption():
    print("\n[1] describe_encryption 要说清用的什么加密")
    with tempfile.TemporaryDirectory() as tmp:
        plain = os.path.join(tmp, "plain.zip")
        with zipfile.ZipFile(plain, "w") as zf:
            zf.writestr("a.pdf", b"%PDF-1.4 x")
        check("不加密的包", archive.describe_encryption(plain), "没有加密")

        # 传统 ZipCrypto：手写一个（算法照抄 CPython zipfile._ZipDecrypter）
        crypto = os.path.join(tmp, "crypto.zip")
        _write_zipcrypto(crypto, "a.pdf", b"%PDF-1.4 hello world", b"pw")
        check("传统 ZipCrypto", archive.describe_encryption(crypto),
              "传统 ZipCrypto 加密（Python 的 zipfile 能解）")
        check("传统包不需要外部工具", archive.needs_external_tool(crypto), False)

    aes = write_aes_zip(tempfile.mkdtemp(), "a.zip", "a.pdf", make_pdf_bytes())
    if not aes:
        skip("AES 样本", "本机没有 7-Zip，造不出 AES 包")
        return
    try:
        desc = archive.describe_encryption(aes)
        check("AES 包被认出", "WinZip AES" in desc, True)
        check("认出具体强度（AES-256）", "AES-256" in desc, True)
        check("AES 包需要外部工具", archive.needs_external_tool(aes), True)
        # 这条是本次事故的核心：报错里必须出现「Python 解不了」这个事实
        check("明说 Python 的 zipfile 解不了", "zipfile 解不了" in desc, True)
    finally:
        for f in (aes,):
            try:
                os.remove(f)
            except OSError:
                pass


def case_zipcrypto_still_works():
    print("\n[2] 传统 ZipCrypto 仍走标准库（不能为了 AES 改坏主路径）")
    with tempfile.TemporaryDirectory() as tmp:
        payload = make_pdf_bytes()
        path = os.path.join(tmp, "statement.zip")
        _write_zipcrypto(path, "statement.pdf", payload, b"secret")
        data, origin = open_document(path, "secret", (".pdf",))
        check("标准库解出 PDF 字节", data[:4], b"%PDF")
        check("内容与原始一致", data == payload, True)
        check("来源说明写明在包内", "压缩包内" in origin, True)

        rows, meta = extract_rows(path, "secret")
        check("页数是 1（不是 0）", meta["pages"], 1)
        check("抽出了表头", "TXN_DATE" in "".join(rows[0]), True)
        check("抽出了三行流水",
              len([r for r in rows if r and r[0].startswith("2026")]), 3)


def case_aes_extracts_and_parses():
    print("\n[3] AES 包：密码对时要真能解出内容并解析")
    tmp = tempfile.mkdtemp()
    try:
        path = write_aes_zip(tmp, "26100501103672708650.zip",
                             "26100501103672708650.pdf", make_pdf_bytes())
        if not path:
            skip("AES 解密", "本机没有 7-Zip")
            return
        # 漏斗第一步：字节要能拿出来
        data, origin = open_document(path, AES_PASSWORD, (".pdf",))
        check("解出 PDF 字节", data[:4], b"%PDF")
        check("来源说明写明在包内", "压缩包内" in origin, True)
        # 漏斗第二步：字节要能解析成表格（这是最终目的）
        rows, meta = extract_rows(path, AES_PASSWORD)
        check("页数是 1", meta["pages"], 1)
        check("有文字层", meta["text_layer"], True)
        check("抽出了表头", "TXN_DATE" in "".join(rows[0]), True)
        check("抽出了三行流水",
              len([r for r in rows if r and r[0].startswith("2026")]), 3)
    finally:
        for f in os.listdir(tmp):
            try:
                os.remove(os.path.join(tmp, f))
            except OSError:
                pass
        os.rmdir(tmp)


def case_error_points_at_truth():
    print("\n[4] 报错必须指向真实原因（本次事故的核心）")
    tmp = tempfile.mkdtemp()
    try:
        path = write_aes_zip(tmp, "26100501103672708650.zip", "s.pdf",
                             make_pdf_bytes())
        if not path:
            skip("报错文案", "本机没有 7-Zip")
        else:
            try:
                open_document(path, "definitely-wrong", (".pdf",))
                got = "居然成功了（不对）"
            except Exception as exc:
                got = f"{type(exc).__name__}\n{exc}"
            check("报的是 ArchiveError", "ArchiveError" in got, True)
            # 本次事故里最刺眼的两个症状
            check("不再出现「压缩包 None」", "None 解不开" not in got, True)
            check("说明了是 WinZip AES", "AES" in got, True)
            # 密码错时要给出真正有用的下一步
            check("给出可操作的下一步", ("7-Zip" in got or "拖出来" in got), True)
    finally:
        for f in os.listdir(tmp):
            try:
                os.remove(os.path.join(tmp, f))
            except OSError:
                pass
        os.rmdir(tmp)

    # 没有 7z 时的提示：必须说清「装 7-Zip」和「或者手动解压」，不能只说密码不对
    saved = archive.SEVENZIP_CANDIDATES
    saved_which = archive.shutil.which
    try:
        archive.SEVENZIP_CANDIDATES = ("C:\\不存在的路径\\7z.exe",)
        archive.shutil.which = lambda *a, **k: None
        with tempfile.TemporaryDirectory() as tmp:
            path = write_aes_zip_before_finder(tmp)
            if path:
                try:
                    open_document(path, AES_PASSWORD, (".pdf",))
                    got = "居然成功了（不对）"
                except Exception as exc:
                    got = str(exc)
                check("缺 7z 时提示装它", "7-Zip" in got, True)
                check("缺 7z 时也给了手动方案", "拖出来" in got, True)
                check("缺 7z 时没说「密码不对」", "密码不对" not in got, True)
    finally:
        archive.SEVENZIP_CANDIDATES = saved
        archive.shutil.which = saved_which


def write_aes_zip_before_finder(directory: str) -> str | None:
    """在 archive._find_7z 被临时改掉**之前**造一个 AES 包。"""
    exe = archive._find_7z()
    if not exe:
        return None
    src = os.path.join(directory, "p.pdf")
    with open(src, "wb") as fh:
        fh.write(make_pdf_bytes())
    dest = os.path.join(directory, "aes.zip")
    try:
        subprocess.run([exe, "a", "-tzip", "-mem=AES256", f"-p{AES_PASSWORD}",
                        "-y", f"-o{directory}", dest, src],
                       capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    os.remove(src)
    return dest if os.path.isfile(dest) else None


# --------------------------------------------------------------------------
# 手写 ZipCrypto（造「传统加密但不需要 7z」的样本）
# --------------------------------------------------------------------------

_CRT: list[int] = []


def _gen_crc(byte: int) -> int:
    """与 CPython zipfile._gen_crc 完全一致。

    两个容易写错的地方（都踩过）：
      * 它**不**做 zlib 那种 `crc ^ 0xffffffff` 前后翻转。
      * key1 的乘数是 134775813 再 +1。PKWARE 规范另有一版 2517751，
        两版都流传很广，CPython 只用前者。
    写错的表现是 zipfile 报「密码不对」，看不出是哪一步错的。
    """
    crc = byte
    for _ in range(8):
        crc = (crc >> 1) ^ 0xEDB88320 if crc & 1 else crc >> 1
    return crc & 0xFFFFFFFF


def _crctable() -> list[int]:
    if not _CRT:
        _CRT.extend(_gen_crc(i) for i in range(256))
    return _CRT


def _crc32b(ch: int, crc: int) -> int:
    return (crc >> 8) ^ _crctable()[(crc ^ ch) & 0xFF]


def _update_keys(keys: list[int], plain: int) -> None:
    k0, k1, k2 = keys
    k0 = _crc32b(plain, k0)
    k1 = (k1 + (k0 & 0xFF)) & 0xFFFFFFFF
    k1 = (k1 * 134775813 + 1) & 0xFFFFFFFF
    k2 = _crc32b((k1 >> 24) & 0xFF, k2)
    keys[0], keys[1], keys[2] = k0, k1, k2


def _encrypt_byte(keys: list[int], plain: int) -> int:
    k = keys[2] | 2
    return plain ^ (((k * (k ^ 1)) >> 8) & 0xFF)


def _write_zipcrypto(path: str, name: str, data: bytes, password: bytes) -> str:
    """写一个单条目、store 模式、传统 ZipCrypto 加密的 zip。"""
    import zlib
    crc = zlib.crc32(data) & 0xFFFFFFFF
    keys = [0x12345678, 0x23456789, 0x34567890]
    for b in password:
        _update_keys(keys, b)
    # flag bit3 未置，所以加密头最后一字节必须是 CRC 高 8 位，解密方拿它快速校验
    head = bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x78, 0x99, 0xAA, 0xBB,
                  (crc >> 24) & 0xFF])
    enc = bytearray()
    for b in head + data:
        enc.append(_encrypt_byte(keys, b))
        _update_keys(keys, b)
    payload = bytes(enc)

    nb = name.encode("utf-8")

    # local header 固定头 30 字节。逐字段对着规范写，不靠「反正都是 0」蒙 ——
    # 少一个字段会让后面所有偏移偏掉，而症状是
    # 「There is no item named 'xxx' in the archive」：报「包里没这个条目」，
    # 而条目明明就在包里。踩过一次：central 少写 disk_start+internal_attr
    # 两个字段，namelist 就变成了 'ement.pdf'（少了前 3 个字符）。
    local = bytearray(b"PK\x03\x04")
    local += (20).to_bytes(2, "little")        # version needed
    local += (0x0001).to_bytes(2, "little")    # flag: bit0 加密
    local += (0).to_bytes(2, "little")         # method: store
    local += (0).to_bytes(2, "little")         # mod time
    local += (0x21).to_bytes(2, "little")      # mod date（2020-01-01，合法值即可）
    local += crc.to_bytes(4, "little")
    local += len(payload).to_bytes(4, "little")  # compressed（含 12 字节加密头）
    local += len(data).to_bytes(4, "little")     # uncompressed
    local += len(nb).to_bytes(2, "little")        # name len
    local += (0).to_bytes(2, "little")            # extra len
    local += nb
    local += payload

    # central directory 固定头 46 字节，字段顺序严格按规范
    central = bytearray(b"PK\x01\x02")
    central += (20).to_bytes(2, "little")       # version made by
    central += (20).to_bytes(2, "little")       # version needed
    central += (0x0001).to_bytes(2, "little")   # flag
    central += (0).to_bytes(2, "little")        # method
    central += (0).to_bytes(2, "little")        # mod time
    central += (0x21).to_bytes(2, "little")     # mod date
    central += crc.to_bytes(4, "little")
    central += len(payload).to_bytes(4, "little")
    central += len(data).to_bytes(4, "little")
    central += len(nb).to_bytes(2, "little")     # name len
    central += (0).to_bytes(2, "little")         # extra len
    central += (0).to_bytes(2, "little")         # comment len
    central += (0).to_bytes(2, "little")         # disk number start
    central += (0).to_bytes(2, "little")         # internal attrs
    central += ((0o100644 << 16) | 0x20).to_bytes(4, "little")   # external attrs
    central += (0).to_bytes(4, "little")         # local header offset
    central += nb

    # EOCD 固定 22 字节。尾部只有 comment_len / disk / internal_attrs 三个
    # 2 字节字段（再往后的偏移字段属于 ZIP64，不属于这里）。
    end = bytearray(b"PK\x05\x06")
    end += (0).to_bytes(2, "little")             # disk number
    end += (0).to_bytes(2, "little")             # disk with central dir
    end += (1).to_bytes(2, "little")             # entries on this disk
    end += (1).to_bytes(2, "little")             # total entries
    end += len(central).to_bytes(4, "little")
    end += len(local).to_bytes(4, "little")
    end += (0).to_bytes(2, "little")             # comment len
    end += (0).to_bytes(2, "little")             # disk number（EOCD 自己的）
    end += (0).to_bytes(2, "little")             # internal attrs

    with open(path, "wb") as fh:
        fh.write(bytes(local) + bytes(central) + bytes(end))
    return path


def main() -> int:
    case_describe_encryption()
    case_zipcrypto_still_works()
    case_aes_extracts_and_parses()
    case_error_points_at_truth()

    print()
    print("=" * 78)
    if SKIPPED:
        print(f"跳过 {len(SKIPPED)} 项（本机没有 7-Zip，造不出 AES 样本）")
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
