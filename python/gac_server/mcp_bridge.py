"""MCP stdio bridge exposing the standalone GAC Computer Use server to DSH.

Single process: starts the GAC TCP server in a background thread, points the
extracted gac_runtime client at it, then serves MCP over stdin/stdout.
DSH mounts this with @deepseek-ai/dsh-mcp-client (serverName: gac, transport: stdio).
"""
from __future__ import annotations

import atexit
import base64
import json
import os
import socket
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gac_server import server as gac

SECRET = b"0123456789abcdef0123456789abcdef"
SECRET_B64 = base64.urlsafe_b64encode(SECRET).decode().rstrip("=")


def start_gac():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]
    os.environ.update({
        "CUA_GAC_TRANSPORT": "tcp",
        "CUA_GAC_SERVER_PORT": str(port),
        "CUA_GAC_SECRET": SECRET_B64,
        "CUA_GAC_SESSION_ID": "dsh-bridge",
        "CUA_GAC_EXECUTION_ID": "dsh-bridge",
        "CUA_GAC_PROTOCOL_VERSION": "1",
    })

    def accept_loop():
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=gac.handle, args=(conn, SECRET), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    return port


TOOLS = [
    ("gac_screenshot", "Capture the target plane: full screen on 'main', the front window on 'pip'", {}),
    ("gac_list_apps", "List launchable Windows applications", {}),
    ("gac_launch_app", "Launch an application by exact name (on the pip desktop when the pip plane is active)", {"name": "string"}),
    ("gac_click", "Click at normalized 0-1000 coordinates (relative to the window on 'pip', to the screen on 'main')", {"x": "number", "y": "number"}),
    ("gac_double_click", "Double-click at normalized coordinates", {"x": "number", "y": "number"}),
    ("gac_right_click", "Right-click at normalized coordinates", {"x": "number", "y": "number"}),
    ("gac_hover", "Move the pointer", {"x": "number", "y": "number"}),
    ("gac_drag", "Drag between two normalized points", {"x1": "number", "y1": "number", "x2": "number", "y2": "number"}),
    ("gac_scroll", "Scroll at a point", {"x": "number", "y": "number", "delta": "number"}),
    ("gac_type", "Type text (Unicode supported)", {"text": "string"}),
    ("gac_hotkey", "Press a key combination", {"keys": "array of strings"}),
    ("gac_press", "Tap a single key", {"key": "string"}),
    ("gac_wait", "Wait for milliseconds", {"ms": "number"}),
    ("gac_get_clipboard", "Read clipboard text", {}),
    ("gac_set_clipboard", "Write clipboard text", {"text": "string"}),
    ("gac_copy_selection", "Ctrl+C then read clipboard", {}),
    ("gac_switch_desktop", "Switch plane: 'pip' = isolated desktop that never touches the user's mouse/keyboard/screen (apps go there); 'main' = the real desktop. A switch affects every later call. Screenshot again after switching.", {"desktop": "string"}),
    ("gac_handoff", "PAUSE and hand the keyboard to the human. The sidebar shows the live pip desktop with a 'done' button and the picture becomes clickable, then this call BLOCKS until they finish or the timeout expires. Use for login, 2FA, captcha, QR scan, payment confirmation - anything you must not do for them.", {"reason": "string", "timeout_s": "number"}),
]


def schema(props):
    types = {"string": {"type": "string"}, "number": {"type": "number"},
             "array of strings": {"type": "array", "items": {"type": "string"}}}
    return {"type": "object",
            "properties": {k: types[v] for k, v in props.items()},
            "required": list(props)}


def tool_defs():
    out = []
    for name, desc, props in TOOLS:
        spec = schema(props)
        if name == "gac_handoff":
            # timeout_s has a server-side default; only the reason is mandatory.
            spec["required"] = ["reason"]
        out.append({"name": name, "description": desc, "inputSchema": spec})
    return out


def handoff(args):
    """Block until the human clicks Done in the sidebar (or the timeout fires)."""
    pip = gac.get_pip()
    view = getattr(pip, "_view", None)
    if view is None:
        raise RuntimeError("pip view server is not running; switch to the pip plane first")
    reason = str(args.get("reason") or "需要你接管操作")
    try:
        timeout_s = float(args.get("timeout_s") or 300)
    except (TypeError, ValueError):
        timeout_s = 300.0
    seq = view.handoff.begin(reason)
    started = time.time()
    done = view.handoff.wait(seq, timeout_s)
    waited = round(time.time() - started, 1)
    return [{"type": "text", "text": json.dumps({
        "completed": bool(done),
        "reason": reason,
        "waited_s": waited,
        "note": ("The human clicked Done. Take a fresh screenshot before continuing."
                 if done else "Timed out: the human never confirmed. Do not assume the step succeeded."),
    }, ensure_ascii=False)}]


def call(name, args):
    from gac_server.gac_client import ComputerUse
    cu = ComputerUse()

    if name == "gac_screenshot":
        frame = cu.screenshot(show=False)
        return [{"type": "image",
                 "data": base64.b64encode(bytes(frame)).decode(),
                 "mimeType": "image/png"}]
    if name == "gac_list_apps":
        apps = cu.list_apps()
        return [{"type": "text", "text": json.dumps([a["name"] for a in apps], ensure_ascii=False)}]
    if name == "gac_handoff":
        return handoff(args)
    simple = {
        "gac_launch_app": lambda: cu.launch_app(args["name"]),
        "gac_click": lambda: cu.click(args["x"], args["y"]),
        "gac_double_click": lambda: cu.left_double(args["x"], args["y"]),
        "gac_right_click": lambda: cu.right_single(args["x"], args["y"]),
        "gac_hover": lambda: cu.hover(args["x"], args["y"]),
        "gac_drag": lambda: cu.drag(args["x1"], args["y1"], args["x2"], args["y2"]),
        "gac_scroll": lambda: cu.scroll(args["x"], args["y"], args["delta"]),
        "gac_type": lambda: cu.type(args["text"]),
        "gac_hotkey": lambda: cu.hotkey(*args["keys"]),
        "gac_press": lambda: cu.press(args["key"]),
        # cu.wait() takes SECONDS (validated 0..180); the MCP tool exposes ms.
        "gac_wait": lambda: cu.wait(min(max(float(args.get("ms", 0)) / 1000.0, 0.0), 180.0)),
        "gac_get_clipboard": lambda: cu.get_clipboard(),
        "gac_set_clipboard": lambda: cu.set_clipboard(args["text"]),
        "gac_copy_selection": lambda: cu.copy_selection(),
        "gac_switch_desktop": lambda: cu.switch_desktop(args["desktop"]),
    }
    if name not in simple:
        raise ValueError("unknown tool " + name)
    result = simple[name]()
    text = "" if result is None else str(result)
    return [{"type": "text", "text": text or "ok"}]


def main():
    port = start_gac()
    print("[gac-mcp] GAC server on 127.0.0.1:%d, %d tools" % (port, len(TOOLS)), file=sys.stderr, flush=True)

    # Apps started on the pip desktop outlive this process unless we stop them;
    # an HDESK dies with its last handle but the processes do not.
    def _cleanup_pip():
        pip = getattr(gac, "_PIP", None)
        if pip is not None:
            try:
                pip.close()
            except Exception:  # noqa: BLE001
                pass

    atexit.register(_cleanup_pip)

    # Start the live-preview HTTP server right away (it does not create the
    # desktop), so the sidebar tab has something to talk to from the start.
    try:
        gac.get_pip().start_view()
    except Exception as exc:  # noqa: BLE001
        print("[gac-mcp] pip view unavailable:", type(exc).__name__, exc, file=sys.stderr, flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid, method = req.get("id"), req.get("method")
        if rid is None:
            continue
        if method == "initialize":
            out = {"protocolVersion": req.get("params", {}).get("protocolVersion", "2025-06-18"),
                   "capabilities": {"tools": {}},
                   "serverInfo": {"name": "gac", "version": "1.0.0"}}
        elif method == "tools/list":
            out = {"tools": tool_defs()}
        elif method == "tools/call":
            params = req.get("params", {})
            try:
                content = call(params.get("name"), params.get("arguments") or {})
                out = {"content": content}
            except Exception as exc:  # noqa: BLE001
                out = {"content": [{"type": "text", "text": "error: %s: %s" % (type(exc).__name__, exc)}],
                       "isError": True}
        else:
            out = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": out}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
