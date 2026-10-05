"""zip 解密：标准库优先，WinZip AES 降级到 7-Zip。

为什么需要这个模块
------------------
`zipfile` 只支持一种加密算法：**传统 ZipCrypto**（PKWARE）。
而实测有一家银行导出的账单用的是 **WinZip AES-256**：

    extra 字段 0x9901 → version=2, vendor=65, strength=69('E')
    compress_type = 99（这是「AES 加密」的标记，不是压缩方法）

`zipfile` 遇到 method 99 直接抛
`NotImplementedError("compression type 99")` 或一句含糊的
「Bad password for file」——**后者会把「算法不支持」误报成「密码不对」**，
用户于是反复重输正确密码，永远解不开。这是本项目最忌讳的那类错误：
**报错指向的原因和真实原因不是一回事。**

CPython 3.14 才给 zipfile 加了 AES 支持，本机是 3.13，所以只能绕。

绕的办法：调本机的 7-Zip。它是 Windows 上最常见的解包器，
也是唯一不用装东西就能拿到 AES 支持的路径（WinRAR 同理）。

**为什么不是「让用户自己先解压」**
那等于把每月都要做一次的麻烦推给用户，而且违反「拿文件那一步只能你手动」
之外的另一条设计原则：能自动的就别让人动手。

**安全边界**
1. 7z.exe 的路径来自 `_find_7z()`，只查已知安装目录，**不接受外部传入** ——
   否则一个被篡改的 vault 就能让程序执行任意 exe。
2. 解压到临时目录，**用完立刻删**（`tempfile.mkdtemp` + `finally`），
   不落在 vault 里。
3. 密码不落盘、不进日志、不进任务参数；命令行里出现是已知且被接受的
   代价（7z 不支持读 stdin），理由与替代方案见 `_extract_with_7z` 的注释。
4. 7z 缺失时不静默失败，明确告诉用户装哪儿。
5. **不打印任何账单内容。** 只报成功/失败和加密方式。
"""

from __future__ import annotations

import io
import os
import shutil
import struct
import subprocess
import tempfile
import zipfile
from typing import Optional

# 7-Zip 常见安装位置。只读这些，不搜 PATH 之外的任意地方 ——
# 见文件头「安全边界」第 1 条。
SEVENZIP_CANDIDATES = (
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
    r"C:\ProgramData\chocolatey\bin\7z.exe",
)

WINZIP_CANDIDATES = (
    r"C:\Program Files\WinRAR\WinRAR.exe",
    r"C:\Program Files (x86)\WinRAR\WinRAR.exe",
    r"C:\Program Files\WinRAR\UnRAR.exe",
    r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
)

# WinZip AES 的 extra 字段 id 与 method 标记
AES_EXTRA_ID = 0x9901
AES_COMPRESS_TYPE = 99


class ArchiveError(ValueError):
    """压缩包解不开。**报错必须指向真实原因**，不能让用户去试错。"""


def _find_7z() -> Optional[str]:
    for path in SEVENZIP_CANDIDATES:
        if os.path.isfile(path):
            return path
    found = shutil.which("7z") or shutil.which("7z.exe")
    return found


def _find_unrar() -> Optional[str]:
    for path in WINZIP_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def describe_encryption(path: str) -> str:
    """这个 zip 用什么方式加密的？返回一句能直接给人看的中文。

    刻意只用中央目录，不解压、不解密、不碰任何账单内容。
    """
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except (OSError, zipfile.BadZipFile) as exc:
        return f"读不出来（{type(exc).__name__}）"

    if not infos:
        return "空压缩包"
    if not any(i.flag_bits & 0x1 for i in infos):
        return "没有加密"

    for info in infos:
        if info.compress_type != AES_COMPRESS_TYPE:
            continue
        strength = ""
        extra = info.extra or b""
        off = 0
        while off + 4 <= len(extra):
            hid, hsz = struct.unpack_from("<HH", extra, off)
            if hid == AES_EXTRA_ID and off + 4 + hsz <= len(extra):
                # WinZip AES extra 字段的字节布局（实测样本逐字节核对过）：
                #   [0:2] header id  = 0x9901
                #   [2:4] data size
                #   [4:6] version    = 2
                #   [6:8] vendor id  = 0x4145（"AE"，2 字节）
                #   [8]   strength   = 1/2/3  ← AES-128/192/256
                #   [9:11] 实际压缩方法
                #
                # 第一版按 "<HBBH" 从 off+4 解，第二个 B 读到的是 vendor
                # 的低字节（0x41），所以 strength 永远显示成「AES-?」。
                val = extra[off + 8]
                strength = {1: "AES-128", 2: "AES-192", 3: "AES-256"}.get(val, "AES-?")
                break
            off += 4 + hsz
        return f"WinZip AES 加密（{strength or 'AES'}，Python 的 zipfile 解不了）"

    return "传统 ZipCrypto 加密（Python 的 zipfile 能解）"


def needs_external_tool(path: str) -> bool:
    """这个包是不是必须靠外部工具才能解开。"""
    try:
        with zipfile.ZipFile(path) as zf:
            return any(i.compress_type == AES_COMPRESS_TYPE for i in zf.infolist())
    except (OSError, zipfile.BadZipFile):
        return False


def _extract_with_7z(path: str, member: str, password: Optional[str],
                     dest_dir: str) -> bytes:
    """用 7z.exe 解出一个成员的内容。

    密码怎么传的，以及为什么这么传
    ------------------------------
    7-Zip **不读 stdin**（实测：`-p` 不带值走管道，产出 0 字节文件并报
    「Sub items Errors」）。它只在交互式控制台里才提示输入，
    批处理场景没有这条路。所以密码只能放在命令行的 `-p<pwd>` 里。

    这确实会让密码出现在进程命令行里（`tasklist` / `wmic` 看得到）。
    这里接受这个代价，理由与取舍：
      · 本程序是**单机单用户**的本地隐私工具，不跑在共享机器上。
      · 窗口创建时立刻隐藏（`CREATE_NO_WINDOW`），密码不会闪在屏幕上。
      · 用完立刻从内存丢弃，不落盘、不进日志、不进任务参数。
      · 替代方案（让用户自己用 7-Zip 解压再丢 PDF）是把每月都要做的
        麻烦推给用户，而且会让「全自动」这条线断掉。
    真要更严，得换成 7-Zip 的库绑定（py7zr 等第三方依赖）——
    那是另一个取舍，本项目不在标准库之外引入加密相关的第三方依赖。
    """
    exe = _find_7z()
    if not exe:
        raise ArchiveError(_no_7z_hint(path))

    env = dict(os.environ)
    # 7z 的输出是 OEM/ANSI 码页。不设这两个的话中文路径会变乱码，
    # 反过来又变成「另一个错误指向错的地方」。
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    # CREATE_NO_WINDOW：不弹黑框。7z 是 GUI 程序，不给这个参数会闪一个窗口。
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    cmd = [exe, "e", "-y", f"-o{dest_dir}", path, member, "-bd"]
    if password:
        cmd.append(f"-p{password}")

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            env=env, timeout=120, creationflags=flags,
        )
    except subprocess.TimeoutExpired as exc:
        raise ArchiveError(f"7-Zip 解压超时（120 秒）：{exc}") from exc

    if proc.returncode != 0:
        out = (proc.stdout or "") + (proc.stderr or "")
        # 7z 的错误信息不区分「密码错」和「文件损坏」。
        # 但既然我们能读到这个包、7z 也在，那**最可能**的原因就是密码 ——
        # 所以这么说，同时把「可能不是密码」的情况也点出来，
        # 不让用户以为只要换密码就一定能解开。
        if "Wrong password" in out or "password" in out.lower() \
                or "Data Error" in out:
            raise ArchiveError(
                f"7-Zip 也解不开这个包：{describe_encryption(path)}。\n"
                "  最可能的原因是密码不对。还有两种可能：\n"
                "    · zip 与里面那份 PDF 用的是**两套不同的密码**"
                "（银行常见：邮件里的和短信里的是两个）\n"
                "    · 你输的密码是那份 PDF 的密码，而不是 zip 的密码\n"
                "  最快的办法：用 7-Zip 手动打开这个 zip，把里面的 PDF 拖出来，"
                "把 PDF 直接丢进投递目录 —— PDF 单独导入不需要 zip 功能。"
            ) from None
        raise ArchiveError(
            "7-Zip 也解不开这个包。\n"
            f"  它的输出：{out.strip()[:300] or '（空）'}\n"
            "  这个包可能已损坏，或者不是常规的 zip。"
        ) from None

    dest = os.path.join(dest_dir, os.path.basename(member))
    if not os.path.isfile(dest):
        # 7z 有时会带上原目录层级
        for root, _dirs, files in os.walk(dest_dir):
            for name in files:
                cand = os.path.join(root, name)
                if name.lower().endswith(os.path.basename(member).lower()):
                    with open(cand, "rb") as fh:
                        return fh.read()
        raise ArchiveError(
            f"7-Zip 说解压成功了，但 {dest_dir} 里找不到 {member}。文件可能已损坏。"
        )
    with open(dest, "rb") as fh:
        return fh.read()


def extract_member(path: str, candidates: list[str],
                   password: Optional[str]) -> tuple[bytes, str]:
    """从 zip 里取出第一个能解出来的成员。

    返回 (内容字节, 包内路径)。所有候选都失败时抛 ArchiveError，
    报错里会带上**真实的**加密方式 —— 不让用户对着错误提示反复试密码。
    """
    data = open(path, "rb").read()
    use_7z = needs_external_tool(path)

    passwords: list[Optional[bytes]] = []
    if password:
        passwords.append(password.encode("utf-8"))
    passwords.append(None)

    # 临时目录用完就删。刻意不放 vault：解出来的账单明文不该在
    # 「数据安全区」里留一份不受加密账本保护的副本。
    tmp = tempfile.mkdtemp(prefix="myledger-zip-")
    try:
        for name in candidates:
            for pwd in passwords:
                if not use_7z:
                    # ① 传统 ZipCrypto：标准库直接能解
                    try:
                        with zipfile.ZipFile(io.BytesIO(data)) as zf:
                            return zf.read(name, pwd=pwd), name
                    except RuntimeError:      # 密码错 / 需要密码
                        continue
                    except (OSError, NotImplementedError, zipfile.BadZipFile):
                        # 标准库读不了这个条目（多半是 AES）。
                        # 不 break —— break 会连带跳过下一个密码档位，
                        # 而「不加密的包」正好是靠 pwd=None 那一档解出来的。
                        # 第一版写成 break，结果加密包在「密码错 → 试无密码」
                        # 中途就跳出循环，传统包反而解不开了。
                        break
                else:
                    # ② WinZip AES：标准库一定解不了，直接走 7-Zip
                    try:
                        content = _extract_with_7z(
                            path, name, pwd.decode("utf-8") if pwd else None, tmp)
                        return content, name
                    except ArchiveError:
                        raise
                    except Exception:
                        continue
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    how = describe_encryption(path)
    if _find_7z() is None:
        raise ArchiveError(_no_7z_hint(path))
    raise ArchiveError(_retry_hint(path, how))


def _no_7z_hint(path: str) -> str:
    return (
        f"这个压缩包解不开：{describe_encryption(path)}，"
        "而本机没找到 7-Zip。\n"
        "  两个办法：\n"
        "  1）装 7-Zip（https://www.7-zip.org/，一路下一步），"
        "装完重跑即可自动解开；\n"
        "  2）用别的工具打开这个包，把里面的 PDF 拖出来，"
        "把 PDF 直接丢进投递目录 —— 那条路不需要 zip 功能。"
    )


def _retry_hint(path: str, how: str) -> str:
    return (
        f"这个压缩包解不开：{how}。\n"
        "  密码可能不对。注意 zip 与里面那份 PDF 可能不是同一个密码：\n"
        "  银行有时用两套密码（一个开 zip，一个开 PDF）。\n"
        "  先用 7-Zip 打开它、把 PDF 拖出来，再把那一份 PDF 丢进投递目录即可。"
    )
