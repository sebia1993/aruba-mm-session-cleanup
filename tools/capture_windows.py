"""Capture one real Tk window on Windows; no desktop pixels or network access.

Pillow is a documentation-workflow dependency, not an application dependency.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import socket
import subprocess
import time
from ctypes import wintypes
from pathlib import Path


def block_network() -> None:
    """Fail closed if any capture fixture accidentally attempts a socket connection."""

    def blocked(*_args, **_kwargs):
        raise RuntimeError("Documentation capture forbids network connections")

    socket.create_connection = blocked
    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked


def settle(window, seconds: float = 0.4) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        window.update_idletasks()
        window.update()
        time.sleep(0.025)


def capture_window(window, path: Path) -> None:
    """Save only the supplied top-level client area, including offscreen regions."""
    if os.name != "nt":
        raise RuntimeError("This capture helper requires Windows and a real Tk window")
    from PIL import Image, ImageStat

    settle(window)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.UINT,
    ]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]

    class BitmapInfoHeader(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("width", wintypes.LONG),
            ("height", wintypes.LONG),
            ("planes", wintypes.WORD),
            ("bit_count", wintypes.WORD),
            ("compression", wintypes.DWORD),
            ("size_image", wintypes.DWORD),
            ("xppm", wintypes.LONG),
            ("yppm", wintypes.LONG),
            ("used", wintypes.DWORD),
            ("important", wintypes.DWORD),
        ]

    inner = window.winfo_id()
    hwnd = user32.GetParent(inner) or inner
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    width, height = rect.right, rect.bottom
    if width < 200 or height < 100:
        raise RuntimeError(f"Unexpected window dimensions: {width}x{height}")
    dc = user32.GetDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(dc)
    bitmap = gdi32.CreateCompatibleBitmap(dc, width, height)
    old_bitmap = gdi32.SelectObject(memory_dc, bitmap)
    try:
        # PW_CLIENTONLY | PW_RENDERFULLCONTENT; never copy the user's desktop.
        if not user32.PrintWindow(hwnd, memory_dc, 3):
            raise RuntimeError("PrintWindow failed")
        gdi32.SelectObject(memory_dc, old_bitmap)
        header = BitmapInfoHeader(
            ctypes.sizeof(BitmapInfoHeader), width, -height, 1, 32, 0, width * height * 4, 0, 0, 0, 0
        )
        data = ctypes.create_string_buffer(width * height * 4)
        if gdi32.GetDIBits(memory_dc, bitmap, 0, height, data, ctypes.byref(header), 0) != height:
            raise RuntimeError("GetDIBits did not read the complete window")
        picture = Image.frombuffer("RGB", (width, height), data.raw, "raw", "BGRX", 0, 1)
        if max(ImageStat.Stat(picture).stddev) < 8:
            raise RuntimeError("Captured window is blank or nearly uniform")
        path.parent.mkdir(parents=True, exist_ok=True)
        picture.save(path)
        with Image.open(path) as reopened:
            reopened.verify()
        print(f"captured: {path.name} ({width}x{height})")
    finally:
        gdi32.SelectObject(memory_dc, old_bitmap)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, dc)


def write_manifest(output: Path, *, app: str, version: str, method: str) -> None:
    source_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    payload = {
        "application": app,
        "application_version": version,
        "source_commit": source_sha,
        "capture_os": platform.platform(),
        "python": platform.python_version(),
        "method": method,
        "data": "synthetic documentation fixtures only",
        "network": "socket connection attempts blocked; no device or account access",
        "workflow_run": os.environ.get("GITHUB_RUN_ID", "local"),
        "images": [
            {"file": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(output.glob("*.png"))
        ],
    }
    (output / "capture-manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def prepare_capture_desktop() -> None:
    """Give ephemeral Actions runners enough desktop area for real widget painting.

    Tk does not paint all offscreen child surfaces, even when PrintWindow succeeds.
    Local Windows hosts are checked but their display configuration is never changed.
    DEVMODEW layout follows Microsoft's wingdi.h; only width/height are modified.
    """
    if os.name != "nt":
        raise RuntimeError("Windows capture requires Windows")
    import struct

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetProcessDPIAware()
    if os.environ.get("GITHUB_ACTIONS") == "true":
        mode = ctypes.create_string_buffer(220)
        struct.pack_into("H", mode, 68, 220)  # dmSize in DEVMODEW
        user32.EnumDisplaySettingsW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
        if not user32.EnumDisplaySettingsW(None, 0xFFFFFFFF, mode):
            raise RuntimeError("Cannot read runner display settings")
        fields = struct.unpack_from("I", mode, 72)[0]
        struct.pack_into("I", mode, 72, fields | 0x00080000 | 0x00100000)
        struct.pack_into("II", mode, 172, 1920, 1080)
        user32.ChangeDisplaySettingsW.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        result = user32.ChangeDisplaySettingsW(mode, 0)
        if result != 0:
            raise RuntimeError(f"Cannot set ephemeral runner display: {result}")
        time.sleep(0.25)
    width, height = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    print(f"capture desktop: {width}x{height}")
    if width < 1600 or height < 1050:
        raise RuntimeError("Capture needs a desktop of at least 1600x1050; offscreen Tk regions would be blank")
