"""系统文件选择对话框（Windows 原生，跨平台时退回 tkinter）。

为什么不用 tkinter 作首选：部分 Windows 环境缺少 Tcl/Tk 运行库
（``Can't find a usable init.tcl``），所以这里优先调用系统自带的
``comdlg32.GetOpenFileNameW``，不依赖任何第三方 GUI 框架。
"""

from __future__ import annotations

import os
import sys
import threading
from typing import List, Optional, Sequence, Tuple

#: 默认文件类型过滤器：[(显示名, 通配符串)]
DEFAULT_FILTERS: List[Tuple[str, str]] = [
    ("象棋棋谱", "*.pgn *.xqf *.cbf *.cbr"),
    ("文本题库", "*.txt"),
    ("所有文件", "*.*"),
]

_DIALOG_LOCK = threading.Lock()


def ask_open_filename(
    title: str = "选择棋谱文件",
    initial_dir: Optional[str] = None,
    filters: Optional[Sequence[Tuple[str, str]]] = None,
) -> str:
    """弹出「打开」对话框并返回所选文件路径；用户取消时返回空字符串。

    参数:
        title: 对话框标题。
        initial_dir: 初始目录。
        filters: 文件类型过滤器，缺省使用 :data:`DEFAULT_FILTERS`。
    """
    filters = list(filters or DEFAULT_FILTERS)
    initial = initial_dir or os.path.expanduser("~")
    with _DIALOG_LOCK:
        if sys.platform == "win32":
            return _ask_windows(title, initial, filters)
        return _ask_tk(title, initial, filters)


def _ask_windows(title: str, initial_dir: str, filters: Sequence[Tuple[str, str]]) -> str:
    """使用 comdlg32 的原生打开对话框。"""
    import ctypes
    from ctypes import wintypes

    ofn_file_must_exist = 0x00001000
    ofn_path_must_exist = 0x00000800
    ofn_explorer = 0x00080000
    ofn_no_changedir = 0x00000008
    ofn_hidereadonly = 0x00000004

    class OPENFILENAMEW(ctypes.Structure):
        """Win32 OPENFILENAMEW 结构体。"""

        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM),
            ("lpfnHook", ctypes.c_void_p),
            ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", ctypes.c_void_p),
            ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        ]

    # 过滤器格式："显示名\0通配符\0显示名\0通配符\0\0"
    pattern = "\0".join(f"{name}\0{spec}" for name, spec in filters) + "\0\0"
    buffer = ctypes.create_unicode_buffer(4096)
    buffer.value = ""

    dialog = OPENFILENAMEW()
    dialog.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    dialog.hwndOwner = None
    dialog.lpstrFilter = pattern
    dialog.nFilterIndex = 1
    dialog.lpstrFile = ctypes.cast(buffer, wintypes.LPWSTR)
    dialog.nMaxFile = len(buffer)
    dialog.lpstrInitialDir = initial_dir if os.path.isdir(initial_dir) else None
    dialog.lpstrTitle = title
    dialog.Flags = (
        ofn_file_must_exist
        | ofn_path_must_exist
        | ofn_explorer
        | ofn_no_changedir
        | ofn_hidereadonly
    )

    try:
        ok = ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(dialog))
    except OSError as exc:  # pragma: no cover - 极端环境
        raise RuntimeError(f"无法打开系统文件对话框：{exc}") from exc
    return buffer.value if ok else ""


def _ask_tk(title: str, initial_dir: str, filters: Sequence[Tuple[str, str]]) -> str:
    """非 Windows 平台退回 tkinter（缺少 Tk 运行库时给出明确提示）。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("当前环境没有可用的图形对话框，请手动填写文件路径") from exc
    try:
        root = tk.Tk()
    except Exception as exc:  # pragma: no cover - 缺 Tcl/Tk 数据文件
        raise RuntimeError(
            f"系统缺少 Tk 运行库（{exc}），请手动填写文件路径或使用浏览器选择文件"
        ) from exc
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        path = filedialog.askopenfilename(
            title=title,
            initialdir=initial_dir,
            filetypes=[(name, spec) for name, spec in filters],
        )
    finally:
        root.destroy()
    return path or ""
