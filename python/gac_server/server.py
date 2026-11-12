"""Minimal standalone GAC protocol v1 server for Windows.

Speaks the wire protocol used by the extracted `gac_runtime` client so that
`import gac_runtime_extract.computer_use as cu` can drive this machine without
the Doubao client. Implements all 18 Computer Use commands with Win32 APIs.

Run:  python gac_server.py --port 52351 --secret <base64>
Then set CUA_GAC_* env vars for the client (see client_env.bat).
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import hmac
import io
import json
import os
import socket
import struct
import sys
import threading
import time
import zlib
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gac_server.gac_crypto import hkdf, seal_inplace, open_inplace

# ---------------------------------------------------------------- wire helpers

FRAME_MAGIC = b"GACF"
U32 = struct.Struct("<I")
HEADER = struct.Struct("<4sI")
ENVELOPE = struct.Struct("<BBI")


def read_exact(conn, size):
    out = bytearray()
    while len(out) < size:
        chunk = conn.recv(size - len(out))
        if not chunk:
            raise EOFError("connection closed")
        out.extend(chunk)
    return bytes(out)


def read_plain_frame(conn):
    magic, size = HEADER.unpack(read_exact(conn, 8))
    if magic != FRAME_MAGIC or size > 4096:
        raise ValueError("invalid plain frame")
    return read_exact(conn, size)


def write_plain_frame(conn, data):
    conn.sendall(HEADER.pack(FRAME_MAGIC, len(data)) + data)


def read_encrypted(conn, key):
    header = read_exact(conn, 8)
    magic, size = HEADER.unpack(header)
    if magic != FRAME_MAGIC or not 28 <= size <= 128 * 1024 * 1024:
        raise ValueError("invalid encrypted frame")
    nonce = read_exact(conn, 12)
    frame = bytearray(read_exact(conn, size - 12))
    open_inplace(key, nonce, header, frame)
    return bytes(frame)


def write_encrypted(conn, key, plain):
    frame = bytearray(plain)
    nonce = os.urandom(12)
    header = HEADER.pack(FRAME_MAGIC, len(frame) + 28)
    seal_inplace(key, nonce, header, frame)
    conn.sendall(header)
    conn.sendall(nonce)
    conn.sendall(frame)


def write_bytes(buf, data):
    buf.write(U32.pack(len(data)))
    buf.write(data)


def write_str(buf, text):
    write_bytes(buf, text.encode("utf-8"))


def read_u32(buf):
    return U32.unpack(buf.read(4))[0]


def read_bytes(buf):
    size = read_u32(buf)
    return buf.read(size) if size else b""


def encode_response(param=None, png_info=None, error=None):
    """status u8 + u32(payload_len) + payload; payload = str(json) [+ png frame]."""
    buf = io.BytesIO()
    if error:
        buf.write(b"\x01")
        payload = io.BytesIO()
        write_str(payload, json.dumps(error, ensure_ascii=False))
        write_bytes(buf, payload.getvalue())
        return buf.getvalue()
    buf.write(b"\x00")
    payload = io.BytesIO()
    write_str(payload, json.dumps(param or {}, ensure_ascii=False))
    if png_info:
        width, height, png = png_info
        payload.write(U32.pack(width))
        payload.write(U32.pack(height))
        write_bytes(payload, png)
    write_bytes(buf, payload.getvalue())
    return buf.getvalue()


def decode_request(frame):
    buf = io.BytesIO(frame)
    version, module_id, command_id = ENVELOPE.unpack(buf.read(6))
    payload = read_bytes(buf)
    pbuf = io.BytesIO(payload)
    text = read_bytes(pbuf).decode("utf-8")
    return version, module_id, command_id, json.loads(text or "{}")


# ------------------------------------------------------------- Win32 backend

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               wintypes.DWORD, ctypes.c_void_p]
user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, wintypes.DWORD,
                               ctypes.c_void_p]
user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.EmptyClipboard.argtypes = []
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.CloseClipboard.argtypes = []
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int,
                         wintypes.DWORD]
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                            wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p,
                            wintypes.UINT]
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004
INPUT_KEYBOARD = 1


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", INPUTUNION)]


VK = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "shift": 0x10,
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "win": 0x5B, "cmd": 0x5B,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "capslock": 0x14, "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78,
    "f10": 0x79, "f11": 0x7A, "f12": 0x7B, "insert": 0x2D,
}


def screen_size():
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def to_px(x, y):
    w, h = screen_size()
    return int(float(x) * w / 1000), int(float(y) * h / 1000)


def vk_of(key):
    k = str(key).lower()
    if k in VK:
        return VK[k]
    if len(k) == 1:
        return ord(k.upper())
    return 0


def png_chunk(tag, data):
    body = tag + data
    return U32.pack(len(data))[::-1] + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def make_png(width, height, bgra):
    """Encode BGRA DIB bytes as PNG.

    Uses strided slice assignment (C-level) instead of a per-pixel Python loop:
    a 1707x1067 frame drops from seconds to tens of milliseconds, which is what
    makes the live pip preview possible.
    """
    # BGRA -> tightly packed RGB, one pass per channel.
    rgb = bytearray(width * height * 3)
    rgb[0::3] = bgra[2::4]
    rgb[1::3] = bgra[1::4]
    rgb[2::3] = bgra[0::4]
    # Filter byte 0 per scanline; DIB rows are bottom-up, so reverse them.
    row = width * 3 + 1
    raw = bytearray(row * height)
    span = width * 3
    for y in range(height):
        src = (height - 1 - y) * span
        dst = y * row
        raw[dst + 1:dst + row] = rgb[src:src + span]
    return (b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + png_chunk(b"IEND", b""))


def grab_screen():
    width, height = screen_size()
    hdc = user32.GetDC(0)
    memdc = gdi32.CreateCompatibleDC(hdc)
    hbmp = gdi32.CreateCompatibleBitmap(hdc, width, height)
    gdi32.SelectObject(memdc, hbmp)
    gdi32.BitBlt(memdc, 0, 0, width, height, hdc, 0, 0, SRCCOPY)
    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight = width, height
    bi.biPlanes, bi.biBitCount = 1, 32
    bi.biCompression = 0
    buf = (ctypes.c_ubyte * (width * height * 4))()
    gdi32.GetDIBits(memdc, hbmp, 0, height, buf, ctypes.byref(bi), DIB_RGB_COLORS)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(0, hdc)
    return width, height, make_png(width, height, bytes(buf))


def get_clipboard_text():
    for _ in range(5):
        if user32.OpenClipboard(0):
            break
        time.sleep(0.05)
    else:
        return ""
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = kernel32.GlobalLock(h)
        text = ctypes.wstring_at(p) if p else ""
        kernel32.GlobalUnlock(h)
        return text
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text):
    data = text.encode("utf-16-le") + b"\x00\x00"
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    user32.OpenClipboard(0)
    user32.EmptyClipboard()
    user32.SetClipboardData(CF_UNICODETEXT, h)
    user32.CloseClipboard()


def send_unicode(text):
    for ch in text:
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            inp = INPUT()
            inp.type = INPUT_KEYBOARD
            inp.u.ki = KEYBDINPUT(0, ord(ch), flags, 0, None)
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.002)


def app_catalog():
    """Start-menu shortcuts plus common App Paths entries."""
    names = set()
    roots = [
        os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
        os.path.join(os.environ.get("PROGRAMDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
    ]
    for root in roots:
        for base, _dirs, files in os.walk(root):
            for f in files:
                if f.lower().endswith(".lnk"):
                    names.add(os.path.splitext(f)[0])
            if len(names) > 300:
                break
    for builtin in ("Notepad", "Calculator", "Explorer", "Command Prompt",
                    "PowerShell", "Microsoft Edge", "Chrome", "Task Manager"):
        names.add(builtin)
    return sorted(names)


APP_PATHS = {
    "Notepad": "notepad.exe",
    "Calculator": "calc.exe",
    "Explorer": "explorer.exe",
    "Command Prompt": "cmd.exe",
    "PowerShell": "powershell.exe",
    "Microsoft Edge": "msedge.exe",
    "Chrome": "chrome.exe",
    "Task Manager": "taskmgr.exe",
}


def launch(name):
    target = APP_PATHS.get(name, name)
    try:
        os.startfile(target)  # noqa: S606 - user-requested local launch
        return True
    except OSError:
        return False


# ------------------------------------------------------------- command table

CMD = {
    1: "list_apps", 2: "launch_app", 3: "screenshot", 5: "click",
    6: "hover", 7: "drag", 8: "scroll", 9: "type", 10: "hotkey",
    11: "wait", 12: "get_clipboard", 13: "set_clipboard", 14: "press",
    15: "key_down", 16: "key_up", 17: "copy_selection", 18: "switch_desktop",
}

# ------------------------------------------------------------ desktop planes
# "main" drives the user's real desktop (BitBlt + SendInput/SetCursorPos).
# "pip"  drives an isolated HDESK (PrintWindow + PostMessage), so the user keeps
#        the mouse and keyboard. Implementation: gac_server/pip_desktop.py
PLANE = {"current": "main"}
_PIP = None

PIP_ROUTED = {
    "screenshot", "click", "hover", "drag", "scroll", "type", "hotkey",
    "press", "key_down", "key_up", "get_clipboard", "set_clipboard",
    "copy_selection",
}


def get_pip():
    global _PIP
    if _PIP is None:
        from gac_server.pip_desktop import PipDesktop
        _PIP = PipDesktop()
    return _PIP


def switch_plane(target):
    """Switch the active plane. Returns the new plane name, raises on failure.

    The GAC `switch_desktop` command is a NoneResult: the wire reply must be an
    empty object, so failures have to surface as protocol errors instead.
    """
    t = str(target or "").strip().lower()
    if t in ("main", "cu", ""):
        PLANE["current"] = "main"
        return "main"
    if t == "pip":
        pip = get_pip()
        if not pip.ensure():
            PLANE["current"] = "main"
            raise RuntimeError("pip desktop unavailable: CreateDesktop or SetThreadDesktop failed")
        PLANE["current"] = "pip"
        return "pip"
    raise ValueError("unknown desktop %r (valid: main, pip)" % (target,))


def execute(command_id, p):
    name = CMD.get(command_id)
    if name is None:
        raise ValueError("unknown command %s" % command_id)

    # Dry-run: exercise the full wire protocol for side-effecting input commands
    # without touching the user's real desktop.
    if os.environ.get("GAC_DRY_RUN") == "1" and name in {
        "launch_app", "click", "hover", "drag", "scroll", "type", "hotkey",
        "press", "key_down", "key_up", "set_clipboard", "copy_selection",
    }:
        if name == "launch_app":
            return {"app": {"name": p.get("name", "")},
                    "status": "accepted_unverified",
                    "launch_method": "apps_folder"}, None
        if name == "copy_selection":
            return {"text": ""}, None
        return {}, None

    # ---- plane routing -----------------------------------------------------
    if name == "switch_desktop":
        switch_plane(p.get("desktop", "main"))
        return {}, None

    if PLANE["current"] == "pip":
        if name == "launch_app":
            app = str(p.get("name", ""))
            return get_pip().launch(app, APP_PATHS.get(app, app)), None
        if name == "list_apps":
            return {"apps": [{"name": n} for n in app_catalog()]}, None
        if name in PIP_ROUTED:
            return get_pip().execute(name, p)

    if name == "list_apps":
        return {"apps": [{"name": n} for n in app_catalog()]}, None

    if name == "launch_app":
        ok = launch(p.get("name", ""))
        return {"app": {"name": p.get("name", "")},
                "status": "accepted_unverified" if ok else "accepted_unverified",
                "launch_method": "apps_folder"}, None

    if name == "screenshot":
        w, h, png = grab_screen()
        return {"width": w, "height": h}, (w, h, png)

    if name in ("click", "hover", "drag", "scroll"):
        if name == "click":
            x, y = to_px(p["x"], p["y"])
            user32.SetCursorPos(x, y)
            button = str(p.get("button", "left")).lower()
            down, up = {
                "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
                "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
                "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
            }.get(button, (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP))
            for _ in range(int(p.get("clicks", 1))):
                user32.mouse_event(down, 0, 0, 0, None)
                user32.mouse_event(up, 0, 0, 0, None)
        elif name == "hover":
            x, y = to_px(p["x"], p["y"])
            user32.SetCursorPos(x, y)
        elif name == "drag":
            x1, y1 = to_px(p["x1"], p["y1"])
            x2, y2 = to_px(p["x2"], p["y2"])
            user32.SetCursorPos(x1, y1)
            user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, None)
            steps = 20
            for i in range(1, steps + 1):
                user32.SetCursorPos(x1 + (x2 - x1) * i // steps,
                                    y1 + (y2 - y1) * i // steps)
                time.sleep(float(p.get("duration_ms", 1000)) / 1000 / steps)
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, None)
        else:
            x, y = to_px(p["x"], p["y"])
            user32.SetCursorPos(x, y)
            delta = int(p.get("delta", 0))
            flag = MOUSEEVENTF_HWHEEL if p.get("horizontal") else MOUSEEVENTF_WHEEL
            user32.mouse_event(flag, 0, 0, ctypes.c_uint(delta * 120).value, None)
        return {}, None

    if name == "type":
        send_unicode(str(p.get("text", "")))
        return {}, None

    if name == "hotkey":
        keys = [vk_of(k) for k in p.get("keys", [])]
        for v in keys:
            if v:
                user32.keybd_event(v, 0, 0, None)
        for v in reversed(keys):
            if v:
                user32.keybd_event(v, 0, KEYEVENTF_KEYUP, None)
        return {}, None

    if name in ("press", "key_down", "key_up"):
        v = vk_of(p.get("key", ""))
        if v:
            flags = KEYEVENTF_KEYUP if name == "key_up" else 0
            user32.keybd_event(v, 0, flags, None)
            if name == "press":
                user32.keybd_event(v, 0, KEYEVENTF_KEYUP, None)
        return {}, None

    if name == "wait":
        time.sleep(min(float(p.get("duration_ms", 0)) / 1000, 180))
        return {}, None

    if name == "get_clipboard":
        return {"text": get_clipboard_text()}, None

    if name == "set_clipboard":
        set_clipboard_text(str(p.get("text", "")))
        return {}, None

    if name == "copy_selection":
        user32.keybd_event(0x11, 0, 0, None)   # Ctrl
        user32.keybd_event(ord("C"), 0, 0, None)
        user32.keybd_event(ord("C"), 0, KEYEVENTF_KEYUP, None)
        user32.keybd_event(0x11, 0, KEYEVENTF_KEYUP, None)
        time.sleep(0.15)
        return {"text": get_clipboard_text()}, None

    return {}, None


# ---------------------------------------------------------------- connection

def handle(conn, secret):
    try:
        complex_id = read_plain_frame(conn).decode("utf-8", "replace")
        token = os.urandom(64)
        write_plain_frame(conn, token)
        digest = read_plain_frame(conn)
        expected = hmac.new(secret, token, hashlib.sha256).digest()
        if not hmac.compare_digest(digest, expected):
            print("[!] HMAC mismatch from", complex_id, file=sys.stderr, flush=True)
            return
        key = hkdf(token, secret)
        print("[+] session:", complex_id, file=sys.stderr, flush=True)
        while True:
            plain = read_encrypted(conn, key)
            version, module, command_id, params = decode_request(plain)
            try:
                param, png = execute(command_id, params)
                reply = encode_response(param, png)
            except Exception as exc:  # noqa: BLE001 - surface as protocol error
                reply = encode_response(error={"code": type(exc).__name__,
                                               "message": str(exc)[:400]})
                print("[!] cmd", command_id, CMD.get(command_id), "->", exc, file=sys.stderr, flush=True)
            write_encrypted(conn, key, reply)
    except (EOFError, ConnectionResetError, OSError):
        pass
    except Exception as exc:  # noqa: BLE001
        print("[!] session error:", type(exc).__name__, exc, file=sys.stderr, flush=True)
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=52351)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--secret", default="")
    args = ap.parse_args()
    secret = base64.urlsafe_b64decode(args.secret + "==") if args.secret else os.urandom(32)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(8)
    print("[+] GAC v1 server on %s:%d" % (args.host, args.port), file=sys.stderr, flush=True)
    print("[+] secret_b64 =", base64.urlsafe_b64encode(secret).decode().rstrip("="), file=sys.stderr, flush=True)
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle, args=(conn, secret), daemon=True).start()


if __name__ == "__main__":
    main()
