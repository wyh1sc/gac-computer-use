"""Isolated 'pip' virtual desktop for the GAC server.

Why each piece is built the way it is (all verified by gac_server/_pip_isolate.py):

  * CreateDesktopW + CreateProcessW(STARTUPINFO.lpDesktop) puts an app on a
    separate HDESK in the same window station. Windows there are invisible to
    the user's main desktop, so the mouse/keyboard stay free - that is the
    whole point of the Doubao `pip` plane.
  * GetDC(0)+BitBlt on a non-active desktop returns an empty surface, so we
    capture with PrintWindow(PW_RENDERFULLCONTENT) per window instead.
  * SetCursorPos/SendInput/mouse_event only reach the *active* desktop, so all
    input is delivered with PostMessage to the innermost child control
    (verified against Notepad's RichEditD2DPT).
  * Every desktop-bound call runs on one dedicated worker thread that called
    SetThreadDesktop, because that API is per-thread and must happen before any
    window is created on the thread.
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
import queue
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32

GENERIC_ALL = 0x10000000
PW_RENDERFULLCONTENT = 0x00000002
DIB_RGB_COLORS = 0
SW_RESTORE = 9

WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_MOUSEWHEEL = 0x020A

MK_LBUTTON = 0x0001
CF_UNICODETEXT = 13
PROCESS_TERMINATE = 0x0001

# Shell helpers that live on every desktop but are never the user's target.
SKIP_TITLES = ("GDI+ Window", "TF_FloatingLangBar", "MSCTFIME", "CiceroUIWndFrame",
               "Default IME", "Tooltip", "Program Manager")
SKIP_CLASSES = ("tooltips_class32", "MSCTFIME UI", "CiceroUIWndFrame",
                "TF_FloatingLangBar_WndTitle", "IME")


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", RECT)]


user32.CreateDesktopW.restype = wintypes.HANDLE
user32.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
user32.GetProcessWindowStation.restype = wintypes.HANDLE
kernel32.CreateProcessW.restype = wintypes.BOOL
user32.WindowFromPoint.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [POINT]
user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]

ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

# Explicit prototypes: on x64 a bare HWND/HDC defaults to a 32-bit int and the
# handle's high bits would be truncated.
_HDC = wintypes.HDC
_HGDIOBJ = wintypes.HANDLE

user32.GetWindowDC.argtypes = [wintypes.HWND]
user32.GetWindowDC.restype = _HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, _HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.PrintWindow.argtypes = [wintypes.HWND, _HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.EnumDesktopWindows.argtypes = [wintypes.HANDLE, ENUMPROC, wintypes.LPARAM]
user32.EnumChildWindows.argtypes = [wintypes.HWND, ENUMPROC, wintypes.LPARAM]
user32.EnumWindows.argtypes = [ENUMPROC, wintypes.LPARAM]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
user32.GetGUIThreadInfo.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
user32.VkKeyScanW.restype = ctypes.c_short
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.EmptyClipboard.argtypes = []
user32.CloseClipboard.argtypes = []
gdi32.CreateCompatibleDC.argtypes = [_HDC]
gdi32.CreateCompatibleDC.restype = _HDC
gdi32.CreateCompatibleBitmap.argtypes = [_HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = _HGDIOBJ
gdi32.SelectObject.argtypes = [_HDC, _HGDIOBJ]
gdi32.GetDIBits.argtypes = [_HDC, _HGDIOBJ, wintypes.UINT, wintypes.UINT,
                           ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
gdi32.DeleteObject.argtypes = [_HGDIOBJ]
gdi32.DeleteDC.argtypes = [_HDC]
kernel32.GetLastError.restype = wintypes.DWORD
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HANDLE
kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetCurrentProcessId.restype = wintypes.DWORD
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]


def _station():
    buf = ctypes.create_unicode_buffer(256)
    n = wintypes.DWORD()
    user32.GetUserObjectInformationW(user32.GetProcessWindowStation(), 2,
                                     ctypes.byref(buf), ctypes.sizeof(buf), ctypes.byref(n))
    return buf.value or "WinSta0"


def _title(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def _klass(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _screen_size():
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


VK_MAP = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
    "esc": 0x1B, "escape": 0x1B, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91, "printscreen": 0x2C,
}
for _i in range(1, 13):
    VK_MAP["f%d" % _i] = 0x6F + _i


def _vk(key):
    k = str(key).lower().strip()
    if k in VK_MAP:
        return VK_MAP[k]
    if len(k) == 1:
        v = user32.VkKeyScanW(ord(k))
        return (v & 0xFF) if v != -1 else 0
    return 0


class _Worker:
    """One thread pinned to the pip desktop via SetThreadDesktop."""

    def __init__(self, hdesk):
        self.hdesk = hdesk
        self.q = queue.Queue()
        self.ready = False
        self._boot = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()
        self._boot.wait(5)

    def _loop(self):
        self.ready = bool(user32.SetThreadDesktop(self.hdesk))
        self._boot.set()
        while True:
            item = self.q.get()
            if item is None:
                break
            fn, args, ev, box = item
            try:
                box["r"] = fn(*args)
            except Exception as exc:  # noqa: BLE001
                box["e"] = exc
            ev.set()

    def run(self, fn, *args, timeout=30):
        if not self.ready:
            raise RuntimeError("pip desktop thread unavailable (SetThreadDesktop failed)")
        ev = threading.Event()
        box = {}
        self.q.put((fn, args, ev, box))
        if not ev.wait(timeout):
            raise RuntimeError("pip desktop operation timed out")
        if "e" in box:
            raise box["e"]
        return box.get("r")


class PipDesktop:
    def __init__(self, name="GacPip"):
        self.name = name
        self._hdesk = None
        self._worker = None
        self._procs = []
        self._view = None

    # ---------------------------------------------------------------- lifecycle

    def ensure(self):
        if self._hdesk is not None:
            return self._worker is not None and self._worker.ready
        h = user32.CreateDesktopW(self.name, None, None, 0, GENERIC_ALL, None)
        if not h:
            return False
        self._hdesk = h
        self._worker = _Worker(h)
        if not self._worker.ready:
            return False
        self.start_view()
        return True

    def start_view(self):
        """Start the live-preview HTTP server without creating the desktop.

        Called at bridge startup so the sidebar can already show "pip plane not
        started" instead of "cannot connect". Failure is never fatal.
        """
        if self._view is not None:
            return self.view_url()
        try:
            from gac_server.pip_view import PipViewServer
            self._view = PipViewServer(self)
            self._view.start()
        except Exception:  # noqa: BLE001
            self._view = None
        return self.view_url()

    def view_url(self):
        return self._view.url() if self._view is not None else None

    def snapshot(self):
        """One PNG of the front window, or None when nothing is there."""
        if not self.running():
            return None
        width, height, png, err = self._worker.run(self._shot, timeout=20)
        if err:
            raise RuntimeError(err)
        return png

    def windows(self):
        """Titles of visible windows on the pip desktop, front first."""
        if not self.running():
            return []
        return [t for _h, t in self._worker.run(self._wins)]

    def close(self):
        if self._view is not None:
            try:
                self._view.stop()
            except Exception:  # noqa: BLE001
                pass
            self._view = None
        for hproc, _pid in self._procs:
            try:
                kernel32.TerminateProcess(hproc, 0)
                kernel32.CloseHandle(hproc)
            except Exception:  # noqa: BLE001
                pass
        self._procs.clear()
        # The HDESK outlives a crashed/closed bridge process, so windows started
        # by an earlier run may still sit here. Only pip-desktop windows are
        # touched - the user's main desktop is never enumerated.
        try:
            self._kill_desktop_procs()
        except Exception:  # noqa: BLE001
            pass
        if self._worker:
            self._worker.q.put(None)
            self._worker = None
        if self._hdesk:
            user32.CloseDesktop(self._hdesk)
            self._hdesk = None

    def purge(self):
        """Terminate every process that still owns a window on the pip desktop."""
        return self._kill_desktop_procs()

    def _kill_desktop_procs(self):
        if not self._hdesk or self._worker is None or not self._worker.ready:
            return []
        return self._worker.run(self._kill_procs_on_desktop, timeout=15)

    def _kill_procs_on_desktop(self):
        pids = set()
        me = int(kernel32.GetCurrentProcessId())

        @ENUMPROC
        def cb(hwnd, _lp):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value and int(pid.value) != me:
                pids.add(int(pid.value))
            return True

        user32.EnumDesktopWindows(self._hdesk, cb, 0)
        killed = []
        for pid in pids:
            h = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if not h:
                continue
            try:
                kernel32.TerminateProcess(h, 0)
                killed.append(pid)
            finally:
                kernel32.CloseHandle(h)
        return killed

    def running(self):
        return self._hdesk is not None and self._worker is not None and self._worker.ready

    def launch(self, name, target):
        if not self.ensure():
            raise RuntimeError("pip desktop unavailable: CreateDesktop or SetThreadDesktop failed")
        si = STARTUPINFOW()
        si.cb = ctypes.sizeof(si)
        si.lpDesktop = "%s\\%s" % (_station(), self.name)
        si.dwFlags = 0x00000001  # STARTF_USESHOWWINDOW
        si.wShowWindow = 1       # SW_SHOWNORMAL
        pi = PROCESS_INFORMATION()
        buf = ctypes.create_unicode_buffer(str(target))
        ok = kernel32.CreateProcessW(None, buf, None, None, False, 0, None, None,
                                     ctypes.byref(si), ctypes.byref(pi))
        if not ok:
            raise RuntimeError("CreateProcessW failed for %r on the pip desktop (WinError %d)"
                               % (target, int(kernel32.GetLastError())))
        self._procs.append((pi.hProcess, pi.dwProcessId))
        try:
            kernel32.CloseHandle(pi.hThread)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.5)
        # The GAC LaunchApp result only accepts status ready|accepted_unverified and
        # launch_method resolved_target|apps_folder. We started a concrete target but
        # cannot promise the window is mapped yet, so report accepted_unverified.
        return {"app": {"name": name}, "status": "accepted_unverified",
                "launch_method": "resolved_target", "pid": pi.dwProcessId}

    # ------------------------------------------------------- desktop-thread ops

    def _wins(self):
        out = []

        @ENUMPROC
        def cb(hwnd, _lp):
            if not user32.IsWindowVisible(hwnd):
                return True
            t = _title(hwnd)
            if not t or any(t.startswith(s) for s in SKIP_TITLES):
                return True
            if _klass(hwnd) in SKIP_CLASSES:
                return True
            out.append((hwnd, t))
            return True

        user32.EnumDesktopWindows(self._hdesk, cb, 0)
        return out

    def _front(self):
        wins = self._wins()
        return wins[0] if wins else None

    def _shot(self):
        fw = self._front()
        if not fw:
            return (0, 0, None, "no visible window on the pip desktop")
        hwnd = fw[0]
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.12)
        r = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if w <= 0 or h <= 0:
            return (0, 0, None, "target window has zero size")
        hdc = user32.GetWindowDC(hwnd)
        mem = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        gdi32.SelectObject(mem, bmp)
        user32.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT)
        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.biWidth, bi.biHeight, bi.biPlanes, bi.biBitCount = w, h, 1, 32
        buf = (ctypes.c_ubyte * (w * h * 4))()
        gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), DIB_RGB_COLORS)
        raw = bytes(buf)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, hdc)
        from gac_server.server import make_png
        return (w, h, make_png(w, h, raw), None)

    def _window_box(self):
        """Current target window and its screen rect."""
        fw = self._front()
        if not fw:
            return 0, None, 0, 0
        hwnd = fw[0]
        r = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        return hwnd, r, w, h

    def _target_at(self, x, y):
        """Map 0-1000 window-relative coords to a child control + screen point.

        The pip plane is window-centric: gac_screenshot returns that window, so
        0-1000 must map onto the window rect (not the whole screen) for what the
        model sees and what it clicks to stay in sync.
        """
        hwnd, r, w, h = self._window_box()
        if not hwnd or w <= 0 or h <= 0:
            return 0, 0, 0
        px = r.left + int(float(x) * w / 1000.0)
        py = r.top + int(float(y) * h / 1000.0)
        pt = POINT(px, py)
        child = user32.WindowFromPoint(pt) or hwnd
        return child, px, py

    def _client_lparam(self, hwnd, px, py):
        cp = POINT(px, py)
        user32.ScreenToClient(hwnd, ctypes.byref(cp))
        return ((cp.y & 0xFFFF) << 16) | (cp.x & 0xFFFF)

    def _click(self, x, y, button="left", clicks=1):
        hwnd, px, py = self._target_at(x, y)
        if not hwnd:
            raise RuntimeError("no target window on the pip desktop")
        lp = self._client_lparam(hwnd, px, py)
        down, up = {
            "left": (WM_LBUTTONDOWN, WM_LBUTTONUP),
            "right": (WM_RBUTTONDOWN, WM_RBUTTONUP),
            "middle": (WM_MBUTTONDOWN, WM_MBUTTONUP),
        }.get(str(button).lower(), (WM_LBUTTONDOWN, WM_LBUTTONUP))
        for _ in range(max(1, int(clicks))):
            user32.PostMessageW(hwnd, down, MK_LBUTTON, lp)
            user32.PostMessageW(hwnd, up, 0, lp)
            time.sleep(0.03)
        return {"target": _title(hwnd)}

    def _hover(self, x, y):
        hwnd, px, py = self._target_at(x, y)
        if not hwnd:
            raise RuntimeError("no target window on the pip desktop")
        user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, self._client_lparam(hwnd, px, py))
        return {"target": _title(hwnd)}

    def _drag(self, x1, y1, x2, y2):
        hwnd, px1, py1 = self._target_at(x1, y1)
        if not hwnd:
            raise RuntimeError("no target window on the pip desktop")
        _t, r, w, h = self._window_box()
        if not r or w <= 0 or h <= 0:
            raise RuntimeError("target window has zero size")
        px2 = r.left + int(float(x2) * w / 1000.0)
        py2 = r.top + int(float(y2) * h / 1000.0)
        user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, self._client_lparam(hwnd, px1, py1))
        steps = 12
        for i in range(1, steps + 1):
            mx = px1 + (px2 - px1) * i // steps
            my = py1 + (py2 - py1) * i // steps
            user32.PostMessageW(hwnd, WM_MOUSEMOVE, MK_LBUTTON, self._client_lparam(hwnd, mx, my))
            time.sleep(0.02)
        user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, self._client_lparam(hwnd, px2, py2))
        return {"target": _title(hwnd)}

    def _scroll(self, x, y, delta):
        hwnd, px, py = self._target_at(x, y)
        if not hwnd:
            raise RuntimeError("no target window on the pip desktop")
        lp = ((py & 0xFFFF) << 16) | (px & 0xFFFF)
        wparam = ((int(delta) * 120) & 0xFFFF) << 16
        user32.PostMessageW(hwnd, WM_MOUSEWHEEL, wparam, lp)
        return {"target": _title(hwnd)}

    def _find_edit(self, hwnd):
        found = []

        @ENUMPROC
        def cb(h, _lp):
            c = _klass(h).lower()
            if "edit" in c or "textbox" in c or "text box" in c:
                found.append(h)
            return True

        user32.EnumChildWindows(hwnd, cb, 0)
        return found[0] if found else 0

    def _focus(self):
        fw = self._front()
        if not fw:
            return 0
        hwnd = fw[0]
        tid = user32.GetWindowThreadProcessId(hwnd, None)
        gui = GUITHREADINFO()
        gui.cbSize = ctypes.sizeof(GUITHREADINFO)
        if user32.GetGUIThreadInfo(tid, ctypes.byref(gui)) and gui.hwndFocus:
            return gui.hwndFocus
        return self._find_edit(hwnd) or hwnd

    def _type(self, text):
        hwnd = self._focus()
        if not hwnd:
            raise RuntimeError("no focus window on the pip desktop")
        for ch in str(text):
            user32.PostMessageW(hwnd, WM_CHAR, 13 if ch == "\n" else ord(ch), 1)
            time.sleep(0.004)
        return {"typed": len(str(text)), "target": _title(hwnd)}

    def _key(self, key, down=True):
        hwnd = self._focus()
        if not hwnd:
            raise RuntimeError("no focus window on the pip desktop")
        vk = _vk(key)
        if not vk:
            raise ValueError("unknown key %r" % (key,))
        user32.PostMessageW(hwnd, WM_KEYDOWN if down else WM_KEYUP, vk, 0)
        return {"key": str(key), "state": "down" if down else "up"}

    def _press(self, key):
        hwnd = self._focus()
        if not hwnd:
            raise RuntimeError("no focus window on the pip desktop")
        vk = _vk(key)
        if not vk:
            raise ValueError("unknown key %r" % (key,))
        user32.PostMessageW(hwnd, WM_KEYDOWN, vk, 0)
        user32.PostMessageW(hwnd, WM_KEYUP, vk, 0)
        return {"key": str(key)}

    def _hotkey(self, keys):
        hwnd = self._focus()
        if not hwnd:
            raise RuntimeError("no focus window on the pip desktop")
        vks = [v for v in (_vk(k) for k in keys) if v]
        if not vks:
            raise ValueError("no usable keys in %r" % (keys,))
        for v in vks:
            user32.PostMessageW(hwnd, WM_KEYDOWN, v, 0)
        for v in reversed(vks):
            user32.PostMessageW(hwnd, WM_KEYUP, v, 0)
        return {"keys": list(keys)}

    def _get_clip(self):
        if not user32.OpenClipboard(0):
            return {"text": ""}
        try:
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return {"text": ""}
            p = kernel32.GlobalLock(h)
            text = ctypes.wstring_at(p) if p else ""
            kernel32.GlobalUnlock(h)
            return {"text": text}
        finally:
            user32.CloseClipboard()

    def _set_clip(self, text):
        data = str(text).encode("utf-16-le") + b"\x00\x00"
        h = kernel32.GlobalAlloc(0x0042, len(data))  # GMEM_MOVEABLE | GMEM_ZEROINIT
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        if not user32.OpenClipboard(0):
            return {"ok": False}
        try:
            user32.EmptyClipboard()
            user32.SetClipboardData(CF_UNICODETEXT, h)
        finally:
            user32.CloseClipboard()
        return {"ok": True}

    def _copy_selection(self):
        self._hotkey(["ctrl", "c"])
        time.sleep(0.25)
        return self._get_clip()

    # ------------------------------------------------------------ dispatch

    def execute(self, name, p):
        """Mirror of server.execute() for the pip plane: returns (param, png).

        Only screenshot / get_clipboard / copy_selection carry a payload on the
        wire; every other GAC command is a NoneResult and MUST reply with an
        empty object, or the client raises "NoneResult must be an empty object".
        """
        w = self._worker
        if w is None or not w.ready:
            raise RuntimeError("pip desktop is not active; call switch_desktop('pip') first")

        if name == "screenshot":
            width, height, png, err = w.run(self._shot, timeout=60)
            if err:
                raise RuntimeError(err)
            return {"width": width, "height": height}, (width, height, png)
        if name == "get_clipboard":
            return w.run(self._get_clip), None
        if name == "copy_selection":
            return w.run(self._copy_selection), None
        if name == "windows":  # internal helper, not part of the GAC command set
            return {"windows": [{"hwnd": h, "title": t} for h, t in w.run(self._wins)]}, None

        if name == "click":
            w.run(self._click, p["x"], p["y"], p.get("button", "left"), p.get("clicks", 1))
        elif name == "hover":
            w.run(self._hover, p["x"], p["y"])
        elif name == "drag":
            w.run(self._drag, p["x1"], p["y1"], p["x2"], p["y2"])
        elif name == "scroll":
            w.run(self._scroll, p["x"], p["y"], p.get("delta", 0))
        elif name == "type":
            w.run(self._type, str(p.get("text", "")))
        elif name == "hotkey":
            w.run(self._hotkey, list(p.get("keys", [])))
        elif name == "press":
            w.run(self._press, p.get("key", ""))
        elif name == "key_down":
            w.run(self._key, p.get("key", ""), True)
        elif name == "key_up":
            w.run(self._key, p.get("key", ""), False)
        elif name == "set_clipboard":
            w.run(self._set_clip, str(p.get("text", "")))
        else:
            raise ValueError("command %r is not routed to the pip plane" % (name,))
        return {}, None
