"""电源状态闸门。

本地 AI 分类（Ollama 跑 7B 模型）是 CPU/GPU 密集任务，插电才跑。
本机实测：满电续航约 2.7 小时，一小时满载能吃掉近半块电池。
拿半块电池换一次分类，不值。

读不到状态时按「未插电」处理（fail-closed），并留 --ignore-power 显式强制。
这个姿势不需要任何第三方库，直接问 Windows。
"""

from __future__ import annotations

import ctypes
import sys


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),        # 1=已插电, 0=电池, 255=未知
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def power_state() -> dict:
    """返回电源状态。非 Windows 或读取失败时返回 unknown。"""
    if not sys.platform.startswith("win"):
        return {"ac": None, "percent": None, "state": "unknown"}

    status = _SystemPowerStatus()
    try:
        ok = ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status))
        if not ok:
            return {"ac": None, "percent": None, "state": "unknown"}
    except Exception:
        return {"ac": None, "percent": None, "state": "unknown"}

    ac = status.ACLineStatus
    percent = status.BatteryLifePercent
    return {
        "ac": True if ac == 1 else (False if ac == 0 else None),
        "percent": percent if percent <= 100 else None,
        "state": {1: "ac", 0: "battery", 255: "unknown"}.get(ac, "unknown"),
    }


def is_on_ac() -> bool:
    """是否插着电。读不到就认为没插电。"""
    return power_state().get("ac") is True


def describe() -> str:
    st = power_state()
    if st["state"] == "ac":
        pct = f"，电量 {st['percent']}%" if st["percent"] is not None else ""
        return f"已插电{pct}"
    if st["state"] == "battery":
        pct = f"，电量 {st['percent']}%" if st["percent"] is not None else ""
        return f"使用电池{pct}"
    return "电源状态未知"


def require_ac(reason: str, override: bool = False) -> bool:
    """需要插电的操作前置检查。返回 True 表示可以继续。

    不做系统级拦截，只打印中文原因并返回 False，由调用方决定降级还是退出。
    """
    if is_on_ac() or override:
        return True
    print(f"已跳过「{reason}」：当前{describe()}。")
    print("  这是 CPU/GPU 密集任务，插上电源再跑，或加 --ignore-power 强制继续。")
    return False
