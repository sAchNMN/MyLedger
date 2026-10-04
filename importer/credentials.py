"""Windows DPAPI 封装：把一段字节加密到「只有当前 Windows 用户能解」。

只有一个用途：加密本地账本文件（见 secure_store.py）。

它解决的是一件事——**不用你记密码，也不留明文**：
    CryptProtectData 的密钥由你的 Windows 登录凭据派生，
    密文拷到别的机器、或者硬盘被拆下来读，都解不开。

它不解决的事（别指望）：
    - 同一个 Windows 账户下运行的恶意程序能解开（任何本地方案都挡不住）
    - 重装系统 / 换 Windows 账户后解不开（这是「不用记密码」的代价）

刻意**不**提供「保存账单 zip 密码」这类功能：
    用户要求 zip 密码用完即忘。把密码存下来和这个要求直接冲突，
    所以这里根本不给那个能力。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

IS_WINDOWS = sys.platform.startswith("win")


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


if IS_WINDOWS:
    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32

    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL

    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL

    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p

_CRYPTPROTECT_UI_FORBIDDEN = 0x01

# 固定的附加熵：让本程序的密文即使和其它用 DPAPI 的程序混在一起也认不出来。
# 它不是密钥，写在源码里没关系——真正的密钥由 Windows 账户派生。
_ENTROPY = b"MyLedger/v1/ledger-at-rest"


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise RuntimeError(
            "账本加密用的是 Windows DPAPI，当前系统不支持。"
            "（这个工具目前只面向 Windows；非 Windows 需要换成口令派生密钥的方案。）"
        )


def _to_blob(data: bytes):
    # 必须把 buffer 一起返回：create_string_buffer 的对象如果被 GC 回收，
    # 指针就悬空了，DPAPI 会读到垃圾数据——而且是随机的垃圾，最难查。
    buf = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf


def protect(data: bytes) -> bytes:
    """明文 → DPAPI 密文。"""
    _require_windows()
    in_blob, _keep = _to_blob(data)
    entropy, _keep2 = _to_blob(_ENTROPY)
    out_blob = _DataBlob()
    ok = _crypt32.CryptProtectData(
        ctypes.byref(in_blob), "MyLedger 账本", ctypes.byref(entropy), None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData 失败")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        _kernel32.LocalFree(out_blob.pbData)


def unprotect(blob: bytes) -> bytes:
    """DPAPI 密文 → 明文。换机器 / 换 Windows 用户后会抛 OSError。"""
    _require_windows()
    in_blob, _keep = _to_blob(blob)
    entropy, _keep2 = _to_blob(_ENTROPY)
    out_blob = _DataBlob()
    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, ctypes.byref(entropy), None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData 失败")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        _kernel32.LocalFree(out_blob.pbData)
