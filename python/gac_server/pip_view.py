"""Live view of the pip desktop over a local HTTP server.

Endpoints (loopback only, CORS open so a DSH sidebar tab or a normal browser
can both consume it):

    GET  /              dark HTML page with the live picture
    GET  /stream        MJPEG (multipart/x-mixed-replace) - use in <img>
    GET  /frame.png     one PNG snapshot
    GET  /status        JSON: plane state, front window, pending handoff
    POST /handoff       {"reason": str} - model asks the human to take over
    POST /handoff/done  {"seq": int}    - human clicked "done"
    POST /input         {"type":"click"|"type"|"key", ...} - forward input

Handoff exists because the pip desktop is NOT on the user's screen: when the
agent hits a login prompt it blocks on /handoff while the sidebar turns the
picture into a clickable surface plus a "done" button.

Why MJPEG: <img src=".../stream"> needs no client JS. A full-screen PNG encodes
in ~55 ms after the strided-slice rewrite in server.make_png.
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFERRED_PORT = 52360
STATE_DIR = os.path.join(os.path.expanduser("~"), ".dsh", "gac")
STATE_FILE = os.path.join(STATE_DIR, "pip-view.json")

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>pip 虚拟桌面</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; background:#15181d; color:#e6e9ef;
         font:13px/1.5 "Segoe UI","Microsoft YaHei",system-ui,sans-serif;
         display:flex; flex-direction:column; height:100vh; }
  header { display:flex; align-items:center; gap:10px; padding:8px 12px;
           background:#1c2027; border-bottom:1px solid #2a2f38; flex:none; }
  #dot { width:8px; height:8px; border-radius:50%; background:#6b7280; flex:none; }
  #dot.on { background:#22c55e; box-shadow:0 0 6px #22c55e; }
  #title { font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  #meta { margin-left:auto; color:#8b93a1; font-variant-numeric:tabular-nums; flex:none; }
  #handoff { display:none; padding:10px 12px; background:#3b2a12; border-bottom:1px solid #7c5a1e;
             color:#fde68a; flex:none; }
  #handoff.on { display:block; }
  #handoff .row { display:flex; align-items:center; gap:10px; }
  #handoff .why { flex:1; }
  #handoff button { background:#f59e0b; color:#1a1206; border:0; border-radius:6px;
                    padding:6px 14px; font-weight:700; cursor:pointer; flex:none; }
  #handoff button:disabled { opacity:.6; cursor:default; }
  main { flex:1; min-height:0; overflow:auto; display:flex; justify-content:center;
         align-items:flex-start; padding:10px; }
  img { width:100%; height:auto; box-shadow:0 6px 24px rgba(0,0,0,.5); border-radius:4px;
        display:none; cursor:default; }
  body.takeover img { cursor:crosshair; }
  #empty { color:#8b93a1; text-align:center; line-height:1.9; margin:auto; }
  #typebar { display:none; gap:8px; padding:8px 12px; background:#1c2027;
             border-top:1px solid #2a2f38; flex:none; }
  #typebar.on { display:flex; }
  #typebar input { flex:1; background:#0f1216; border:1px solid #2a2f38; color:#e6e9ef;
                   border-radius:6px; padding:7px 10px; font:inherit; }
  #typebar button { background:#2563eb; color:#fff; border:0; border-radius:6px;
                    padding:7px 14px; font-weight:600; cursor:pointer; }
  .k { color:#7dd3fc; }
</style></head>
<body>
<header>
  <span id="dot"></span>
  <span id="title">等待 pip 虚拟桌面…</span>
  <span id="meta"></span>
</header>
<div id="handoff"><div class="row">
  <span class="why" id="why">需要你接管</span>
  <button id="done">我已完成</button>
</div></div>
<main>
  <img id="view" alt="pip desktop">
  <div id="empty">
    <div>还没有画面</div>
    <div>先切到 pip 平面并启动一个应用，例如：</div>
    <div><code>switch_desktop(<span class="k">"pip"</span>)</code> → <code>launch_app(<span class="k">"Command Prompt"</span>)</code></div>
  </div>
</main>
<div id="typebar">
  <input id="text" placeholder="在这里输入文字，回车发送到 pip 桌面">
  <button id="send">发送</button>
</div>
<script>
const img = document.getElementById('view');
const dot = document.getElementById('dot');
const titleEl = document.getElementById('title');
const metaEl = document.getElementById('meta');
const handoffEl = document.getElementById('handoff');
const whyEl = document.getElementById('why');
const doneBtn = document.getElementById('done');
const typebar = document.getElementById('typebar');
const textEl = document.getElementById('text');
let frames = 0, t0 = Date.now(), loaded = false;
let handoffSeq = 0;

img.onload = () => { loaded = true; img.style.display = 'block';
  document.getElementById('empty').style.display = 'none'; frames++; };
img.onerror = () => { img.src = '/stream?t=' + Date.now(); };
img.src = '/stream';

function tick() {
  const fps = frames / Math.max(1, (Date.now() - t0) / 1000);
  metaEl.textContent = loaded ? fps.toFixed(1) + ' fps' : '';
  frames = 0; t0 = Date.now();
}
setInterval(tick, 2000);

function post(path, body) {
  return fetch(path, { method: 'POST', headers: { 'content-type': 'application/json' },
                       body: JSON.stringify(body || {}) }).then(r => r.json());
}

doneBtn.onclick = async () => {
  doneBtn.disabled = true;
  try { await post('/handoff/done', { seq: handoffSeq }); } catch (e) {}
  doneBtn.disabled = false;
};

img.onclick = (e) => {
  if (!document.body.classList.contains('takeover')) return;
  const r = img.getBoundingClientRect();
  post('/input', { type: 'click',
                   x: (e.clientX - r.left) / r.width,
                   y: (e.clientY - r.top) / r.height }).catch(() => {});
};

function sendText() {
  const text = textEl.value;
  if (!text) return;
  textEl.value = '';
  post('/input', { type: 'type', text }).catch(() => {});
}
document.getElementById('send').onclick = sendText;
textEl.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); sendText(); } };

async function poll() {
  try {
    const r = await fetch('/status', { cache: 'no-store' });
    const s = await r.json();
    if (s.active && s.window) {
      dot.classList.add('on'); titleEl.textContent = s.window;
    } else {
      dot.classList.remove('on');
      titleEl.textContent = s.active ? 'pip 桌面空闲（未启动应用）' : (s.reason || 'pip 桌面未创建');
    }
    const h = s.handoff || {};
    handoffSeq = h.seq || 0;
    const on = !!h.active;
    handoffEl.classList.toggle('on', on);
    document.body.classList.toggle('takeover', on);
    typebar.classList.toggle('on', on);
    if (on) whyEl.textContent = h.reason || '需要你接管操作';
  } catch (e) {
    dot.classList.remove('on');
    titleEl.textContent = '无法连接本地服务';
  }
}
poll(); setInterval(poll, 1200);
</script>
</body></html>
"""


class _FrameSource:
    """Single-flight frame grabber with a tiny cache so N clients share one grab."""

    def __init__(self, pip):
        self.pip = pip
        self._lock = threading.Lock()
        self._png = None
        self._at = 0.0
        self._err = "pip desktop not created"

    def frame(self):
        with self._lock:
            now = time.time()
            if self._png is not None and now - self._at < 0.08:
                return self._png
            pip = self.pip
            if pip is None or not pip.running():
                self._err = "pip desktop not created"
                self._png = None
                return None
            try:
                png = pip.snapshot()
            except Exception as exc:  # noqa: BLE001
                self._err = "%s: %s" % (type(exc).__name__, exc)
                self._png = None
                return None
            if png is None:
                self._err = "no visible window on the pip desktop"
                self._png = None
                return None
            self._png = png
            self._at = time.time()
            self._err = None
            return png

    def status(self):
        pip = self.pip
        if pip is None or not pip.running():
            return {"active": False, "reason": "pip desktop not created"}
        try:
            wins = pip.windows()
        except Exception as exc:  # noqa: BLE001
            return {"active": True, "error": "%s: %s" % (type(exc).__name__, exc)}
        return {"active": True,
                "window": wins[0] if wins else None,
                "windows": wins}


class Handoff:
    """One-pending-handoff mailbox shared by the MCP tool and the sidebar."""

    def __init__(self):
        self._lock = threading.Lock()
        self._seq = 0
        self._active = False
        self._reason = ""
        self._since = 0.0
        self._done = 0

    def begin(self, reason):
        with self._lock:
            self._seq += 1
            self._active = True
            self._reason = str(reason or "")
            self._since = time.time()
            return self._seq

    def finish(self, seq=None):
        with self._lock:
            target = self._seq if seq is None else int(seq)
            if target >= self._done:
                self._done = target
            self._active = False
            return self._done

    def state(self):
        with self._lock:
            return {"active": self._active, "reason": self._reason,
                    "since": self._since, "seq": self._seq, "done": self._done}

    def wait(self, seq, timeout_s, poll=0.25):
        """Block until the human clicks done (True) or the timeout expires (False)."""
        deadline = time.time() + max(1.0, float(timeout_s))
        while time.time() < deadline:
            with self._lock:
                if self._done >= seq:
                    self._active = False
                    return True
                if not self._active:
                    return False
            time.sleep(poll)
        with self._lock:
            self._active = False
        return False


def _read_json(rfile, headers):
    try:
        length = int(headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        length = 0
    raw = rfile.read(length) if length > 0 else b""
    if not raw:
        return {}
    try:
        value = json.loads(raw.decode("utf-8", "replace"))
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def _handler(source, handoff, pip):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a):  # keep stdout/stderr clean
            pass

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")

        def _json(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "content-type")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            payload = _read_json(self.rfile, self.headers)
            if path == "/handoff":
                seq = handoff.begin(payload.get("reason", ""))
                self._json(200, {"ok": True, "seq": seq})
                return
            if path == "/handoff/done":
                seq = handoff.finish(payload.get("seq"))
                self._json(200, {"ok": True, "seq": seq})
                return
            if path == "/input":
                try:
                    self._json(200, self._do_input(payload))
                except Exception as exc:  # noqa: BLE001
                    self._json(400, {"ok": False,
                                     "error": "%s: %s" % (type(exc).__name__, exc)})
                return
            self._json(404, {"ok": False, "error": "unknown endpoint"})

        def _do_input(self, payload):
            if pip is None or not pip.running():
                return {"ok": False, "error": "pip desktop not active"}
            kind = str(payload.get("type", "")).lower()
            worker = pip._worker
            if kind == "click":
                # 0..1 from the rendered <img> -> 0..1000 window-relative.
                x = float(payload.get("x", 0)) * 1000.0
                y = float(payload.get("y", 0)) * 1000.0
                worker.run(pip._click, x, y, str(payload.get("button", "left")), 1)
                return {"ok": True}
            if kind == "type":
                worker.run(pip._type, str(payload.get("text", "")))
                return {"ok": True}
            if kind == "key":
                worker.run(pip._press, str(payload.get("key", "")))
                return {"ok": True}
            return {"ok": False, "error": "unknown input type %r" % (kind,)}

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/status":
                state = source.status()
                state["handoff"] = handoff.state()
                self._json(200, state)
                return
            if path == "/frame.png":
                png = source.frame()
                if png is None:
                    body = (source._err or "no frame").encode("utf-8")
                    self.send_response(503)
                    self._cors()
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png)))
                self.end_headers()
                self.wfile.write(png)
                return
            if path == "/stream":
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    while True:
                        png = source.frame()
                        if png is not None:
                            self.wfile.write(b"--frame\r\n")
                            self.wfile.write(b"Content-Type: image/png\r\n")
                            self.wfile.write(b"Content-Length: " + str(len(png)).encode() + b"\r\n\r\n")
                            self.wfile.write(png)
                            self.wfile.write(b"\r\n")
                        time.sleep(0.12)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
            self.send_response(404)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


class PipViewServer:
    def __init__(self, pip):
        self.pip = pip
        self.source = _FrameSource(pip)
        self.handoff = Handoff()
        self.httpd = None
        self.port = None
        self._thread = None

    def start(self):
        if self.httpd is not None:
            return self.port
        handler = _handler(self.source, self.handoff, self.pip)
        last = None
        for port in (PREFERRED_PORT, 0):
            try:
                httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
                httpd.daemon_threads = True
                self.httpd = httpd
                self.port = httpd.server_port
                break
            except OSError as exc:  # port busy
                last = exc
        if self.httpd is None:
            raise RuntimeError("cannot bind pip view server: %s" % (last,))
        self._thread = threading.Thread(target=self.httpd.serve_forever,
                                        kwargs={"poll_interval": 0.3}, daemon=True)
        self._thread.start()
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as fh:
                json.dump({"url": "http://127.0.0.1:%d/" % self.port,
                           "port": self.port, "pid": os.getpid()}, fh, ensure_ascii=False)
        except OSError:
            pass
        return self.port

    def url(self):
        return "http://127.0.0.1:%d/" % self.port if self.port else None

    def stop(self):
        if self.httpd is not None:
            try:
                self.handoff.finish()
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
            self.httpd = None
