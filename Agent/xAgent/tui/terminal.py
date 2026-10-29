"""Windows 控制台字体配置（仅 TUI 层使用）。

cmd.exe / conhost 的默认字体经常缺少等宽中文字形，导致 CJK 字符、
制表边框和 Markdown 表格错位。xAgent 启动时尝试把当前控制台切换为
黑体；如果接口不可用（例如 Windows Terminal 或非 Windows），静默跳过。

可以用环境变量覆盖：
    XAGENT_CONSOLE_FONT=Consolas
    XAGENT_NO_FONT_SETUP=1
"""

from __future__ import annotations

import ctypes
import os

DEFAULT_CONSOLE_FONT = "黑体"
_FALLBACK_FONTS = ("SimHei", "Consolas")


def console_is_active() -> bool | None:
    """判断传统 cmd.exe/conhost 窗口当前是否位于前台。

    Windows Terminal 和普通终端不适用时返回 None。该检测用于弥补
    conhost 不上报 AppFocus/AppBlur 事件的问题。
    """
    if os.name != "nt" or os.getenv("WT_SESSION"):
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32.GetConsoleWindow.restype = ctypes.c_void_p
        user32.GetForegroundWindow.restype = ctypes.c_void_p

        console_window = kernel32.GetConsoleWindow()
        if not console_window:
            return None
        foreground_window = user32.GetForegroundWindow()
        return int(foreground_window or 0) == int(console_window)
    except (AttributeError, OSError, ValueError):
        return None


class _Coord(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class _ConsoleFontInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("nFont", ctypes.c_uint32),
        ("dwFontSize", _Coord),
        ("FontFamily", ctypes.c_uint32),
        ("FontWeight", ctypes.c_uint32),
        ("FaceName", ctypes.c_wchar * 32),
    ]


def configure_console_font(face_name: str | None = None) -> bool:
    """尽力把当前 Windows 控制台字体切换为等宽中文字体。

    Returns:
        True 表示字体设置接口返回成功；False 表示当前环境不支持或被禁用。
        失败不会影响 TUI 启动。
    """
    if os.name != "nt" or os.getenv("XAGENT_NO_FONT_SETUP") == "1":
        return False

    requested = (
        face_name
        or os.getenv("XAGENT_CONSOLE_FONT", "").strip()
        or DEFAULT_CONSOLE_FONT
    )
    candidates = tuple(dict.fromkeys((requested, *_FALLBACK_FONTS)))

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not kernel32.GetConsoleWindow():
            return False

        # stdout 可能被重定向到管道；CONOUT$ 始终指向当前控制台。
        kernel32.CreateFileW.restype = ctypes.c_void_p
        handle = kernel32.CreateFileW(
            "CONOUT$",
            0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
            0x00000001 | 0x00000002,  # FILE_SHARE_READ | FILE_SHARE_WRITE
            None,
            3,  # OPEN_EXISTING
            0,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if not handle or handle == invalid_handle:
            return False

        try:
            return _set_console_font(kernel32, handle, candidates)
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    except (AttributeError, OSError, ValueError):
        return False


def _set_console_font(
    kernel32: ctypes.WinDLL,
    handle: int,
    candidates: tuple[str, ...],
) -> bool:
    current = _ConsoleFontInfo()
    current.cbSize = ctypes.sizeof(_ConsoleFontInfo)
    if not kernel32.GetCurrentConsoleFontEx(handle, False, ctypes.byref(current)):
        return False

    width = current.dwFontSize.X if current.dwFontSize.X > 0 else 8
    height = current.dwFontSize.Y if current.dwFontSize.Y > 0 else 16

    for candidate in candidates:
        info = _ConsoleFontInfo()
        info.cbSize = ctypes.sizeof(_ConsoleFontInfo)
        info.nFont = 0
        info.dwFontSize = _Coord(width, height)
        info.FontFamily = 0
        info.FontWeight = 400
        info.FaceName = candidate
        if kernel32.SetCurrentConsoleFontEx(handle, False, ctypes.byref(info)):
            return True
    return False
