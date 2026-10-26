"""读取系统剪贴板（TUI 层专用）。

Textual 自己维护的 clipboard 只包含应用内复制的内容，没有系统剪贴板。
在 Windows cmd.exe 中 Ctrl+V 往往不会产生 bracketed-paste 事件，因此
这里直接调用 Win32 剪贴板 API；其它平台尝试 tkinter，失败时返回空串。
"""

from __future__ import annotations

import ctypes
import os


def read_clipboard_text() -> str:
    """尽力读取系统剪贴板中的纯文本。"""
    if os.name == "nt":
        return _read_windows_clipboard()
    return _read_tkinter_clipboard()


def _read_windows_clipboard() -> str:
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        user32.GetClipboardData.restype = ctypes.c_void_p
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]

        if not user32.OpenClipboard(None):
            return ""
        try:
            handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
            if not handle:
                return ""
            pointer = kernel32.GlobalLock(ctypes.c_void_p(handle))
            if not pointer:
                return ""
            try:
                return ctypes.wstring_at(pointer)
            finally:
                kernel32.GlobalUnlock(ctypes.c_void_p(pointer))
        finally:
            user32.CloseClipboard()
    except (AttributeError, OSError, ValueError):
        return ""


def _read_tkinter_clipboard() -> str:
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        try:
            return root.clipboard_get()
        finally:
            root.destroy()
    except Exception:
        return ""
