"""Self-contained E2E test for the standalone GAC stack.

Starts :mod:`gac_server.server` in-process on a random loopback port, then
drives *every* Computer Use command through the reimplemented
:class:`gac_server.gac_client.ComputerUse` facade. No part of this path imports
``gac_runtime_extract``.

GUI input commands are answered by the server's ``GAC_DRY_RUN`` stub so the test
will not move the mouse or type on the real desktop; screenshot and clipboard
reads are real (read-only) so the PNG pipeline is still exercised.

    python gac_server/test_e2e.py
"""
from __future__ import annotations

import base64
import os
import socket
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["GAC_DRY_RUN"] = "1"

from gac_server import server as gac  # noqa: E402

SECRET = b"0123456789abcdef0123456789abcdef"
SECRET_B64 = base64.urlsafe_b64encode(SECRET).decode().rstrip("=")

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 0))
srv.listen(8)
PORT = srv.getsockname()[1]


def accept_loop():
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=gac.handle, args=(conn, SECRET), daemon=True).start()


threading.Thread(target=accept_loop, daemon=True).start()

os.environ.update({
    "CUA_GAC_TRANSPORT": "tcp",
    "CUA_GAC_SERVER_PORT": str(PORT),
    "CUA_GAC_SECRET": SECRET_B64,
    "CUA_GAC_SESSION_ID": "e2e",
    "CUA_GAC_EXECUTION_ID": "e2e",
    "CUA_GAC_PROTOCOL_VERSION": "1",
})

from gac_server.gac_client import ComputerUse  # noqa: E402

cu = ComputerUse()
passed = 0
failed = 0


def check(name, fn):
    global passed, failed
    try:
        fn()
        print("  PASS  %-16s" % name)
        passed += 1
    except Exception as exc:  # noqa: BLE001
        print("  FAIL  %-16s -> %s: %s" % (name, type(exc).__name__, exc))
        failed += 1


check("list_apps", lambda: cu.list_apps() or None)
check("screenshot", lambda: (cu.screenshot().size, None).__class__ and None)
check("get_clipboard", lambda: cu.get_clipboard())
check("set_clipboard", lambda: cu.set_clipboard("pytest-marker"))
check("launch_app", lambda: cu.launch_app("Notepad"))
check("click", lambda: cu.click(500, 500))
check("left_double", lambda: cu.left_double(500, 500))
check("right_single", lambda: cu.right_single(500, 500))
check("hover", lambda: cu.hover(500, 500))
check("drag", lambda: cu.drag(100, 100, 900, 900))
check("scroll", lambda: cu.scroll(500, 500, -3))
check("type", lambda: cu.type("hello world"))
check("hotkey", lambda: cu.hotkey("ctrl", "c"))
check("press", lambda: cu.press("enter"))
check("key_down", lambda: cu.key_down("shift"))
check("key_up", lambda: cu.key_up("shift"))
check("copy_selection", lambda: cu.copy_selection())
check("wait", lambda: cu.wait(0.01))
check("switch_desktop", lambda: cu.switch_desktop("main"))

print("\n[%d passed, %d failed]" % (passed, failed))
sys.exit(1 if failed else 0)
