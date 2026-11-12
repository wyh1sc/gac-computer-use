"""Standalone GAC protocol client + Computer Use facade.

This is an independent reimplementation of the client half of the GAC wire
protocol: it opens a TCP connection to the local GAC server, performs the
HMAC token-challenge handshake, derives the session key, and exchanges
encrypted frames. It replaces Doubao's `gac_runtime_extract.computer_use`
module so the plugin is fully self-contained.

The server side is :mod:`gac_server.server`; both ends speak the same RFC 8439
AEAD envelope defined in :mod:`gac_server.gac_crypto`.
"""
from __future__ import annotations

import base64
import io
import json
import os
import socket
import struct
import threading

from gac_server.gac_crypto import hkdf
from gac_server.gac_crypto import seal
from gac_server.gac_crypto import open as aead_open

# Frame header shared with the server.
FRAME_MAGIC = b"GACF"
HEADER = struct.Struct("<4sI")
ENVELOPE = struct.Struct("<BBI")

COMPUTER_USE_MODULE_ID = 2
COMPUTER_USE_PROTOCOL_VERSION = 1

# Command ids (must match gac_server.server.CMD).
CMD_IDS = {
    "list_apps": 1,
    "launch_app": 2,
    "screenshot": 3,
    "click": 5,
    "hover": 6,
    "drag": 7,
    "scroll": 8,
    "type": 9,
    "hotkey": 10,
    "wait": 11,
    "get_clipboard": 12,
    "set_clipboard": 13,
    "press": 14,
    "key_down": 15,
    "key_up": 16,
    "copy_selection": 17,
    "switch_desktop": 18,
}

_MAX_FRAME = 128 * 1024 * 1024


class Frame(bytes):
    """PNG bytes with the captured virtual-desktop dimensions (mirrors upstream)."""

    def __new__(cls, data: bytes, width: int, height: int):
        obj = super().__new__(cls, data)
        obj.width = int(width)
        obj.height = int(height)
        return obj

    @property
    def size(self) -> "tuple[int, int]":
        return self.width, self.height


def _read_exact(conn, size):
    out = bytearray()
    while len(out) < size:
        chunk = conn.recv(size - len(out))
        if not chunk:
            raise EOFError("connection closed")
        out.extend(chunk)
    return bytes(out)


def _read_buffer(bf, size):
    """Read exactly *size* bytes from an in-memory binary stream (BytesIO)."""
    data = bf.read(size)
    if len(data) != size:
        raise EOFError("truncated payload")
    return data


def _write_str(buf, text):
    data = text.encode("utf-8")
    buf.write(struct.pack("<I", len(data)))
    buf.write(data)


def _read_str(buf):
    n = struct.unpack("<I", _read_buffer(buf, 4))[0]
    if n == 0:
        return ""
    return _read_buffer(buf, n).decode("utf-8")


def _encode_request(command, params):
    payload = io.BytesIO()
    _write_str(payload, json.dumps(params, ensure_ascii=False))
    payload = payload.getvalue()
    buf = io.BytesIO()
    buf.write(ENVELOPE.pack(COMPUTER_USE_PROTOCOL_VERSION, COMPUTER_USE_MODULE_ID, CMD_IDS[command]))
    buf.write(struct.pack("<I", len(payload)))
    buf.write(payload)
    return buf.getvalue()


class GacClient:
    """A single persistent TCP connection to the local GAC server."""

    def __init__(self, host="127.0.0.1", port=0, secret=None,
                 session_id="dsh-bridge", execution_id="dsh-bridge"):
        self.host = host
        self.port = port
        self.secret = secret
        self.session_id = session_id
        self.execution_id = execution_id
        self.conn = None
        self.key = None
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls):
        port = int(os.environ.get("CUA_GAC_SERVER_PORT", "0"))
        secret_b64 = os.environ.get("CUA_GAC_SECRET", "")
        secret = base64.urlsafe_b64decode(secret_b64 + "==") if secret_b64 else b""
        return cls(
            host="127.0.0.1",
            port=port,
            secret=secret,
            session_id=os.environ.get("CUA_GAC_SESSION_ID", "dsh-bridge"),
            execution_id=os.environ.get("CUA_GAC_EXECUTION_ID", "dsh-bridge"),
        )

    def connect(self):
        conn = socket.create_connection((self.host, self.port), timeout=5.0)
        conn.settimeout(120.0)
        # token-challenge handshake
        complex_id = (self.session_id + ":" + self.execution_id).encode("utf-8")
        conn.sendall(FRAME_MAGIC)
        conn.sendall(struct.pack("<I", len(complex_id)))
        conn.sendall(complex_id)
        # Server wraps the token in a plain frame: 8-byte header (GACF + u32 len)
        # then the 64-byte token. Consume the header first, like server.read_plain_frame.
        header = _read_exact(conn, 8)
        magic, size = HEADER.unpack(header)
        if magic != FRAME_MAGIC or size != 64:
            conn.close()
            raise ValueError("invalid handshake token frame")
        token = _read_exact(conn, size)
        if len(token) != 64:
            conn.close()
            raise ValueError("invalid handshake token")
        digest = hmac_sha256(self.secret, token)
        conn.sendall(FRAME_MAGIC)
        conn.sendall(struct.pack("<I", len(digest)))
        conn.sendall(digest)
        self.key = hkdf(token, self.secret)
        self.conn = conn
        return self

    def _ensure(self):
        if self.conn is None or self.key is None:
            self.connect()

    def transceive(self, command, params):
        with self._lock:
            self._ensure()
            frame = _encode_request(command, params)
            nonce = os.urandom(12)
            header = HEADER.pack(FRAME_MAGIC, len(frame) + 28)
            encrypted = seal(self.key, nonce, header, frame)
            self.conn.sendall(header)
            self.conn.sendall(nonce)
            self.conn.sendall(encrypted)
            # read reply
            header = _read_exact(self.conn, 8)
            magic, size = HEADER.unpack(header)
            if magic != FRAME_MAGIC or not 28 <= size <= _MAX_FRAME:
                raise ValueError("invalid encrypted frame")
            nonce = _read_exact(self.conn, 12)
            blob = bytearray(_read_exact(self.conn, size - 12))
            plaintext = aead_open(self.key, nonce, header, blob)
            return self._decode(plaintext)

    @staticmethod
    def _decode(plaintext):
        buf = io.BytesIO(plaintext)
        head = buf.read(1)
        if not head:
            raise EOFError("empty payload")
        status = head[0]
        inner_len = struct.unpack("<I", _read_buffer(buf, 4))[0]
        inner = _read_buffer(buf, inner_len) if inner_len else b""
        if status != 0:
            try:
                err = json.loads(inner.decode("utf-8", "replace"))
                msg = err.get("message", inner.decode("utf-8", "replace"))
            except Exception:
                msg = inner.decode("utf-8", "replace")
            raise RuntimeError("GAC server error: " + str(msg))
        inner_buf = io.BytesIO(inner)
        result = _read_str(inner_buf)
        parsed = json.loads(result) if result else {}
        # optional PNG attachment
        rest = inner_buf.read()
        if len(rest) >= 12:
            w, h = struct.unpack("<II", rest[:8])
            png_len = struct.unpack("<I", rest[8:12])[0]
            png = rest[12:12 + png_len]
            return parsed, (w, h, png)
        return parsed, None

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None


def hmac_sha256(key, data):
    import hmac as _hmac
    return _hmac.new(key, data, "sha256").digest()


# ------------------------------------------------------- Computer Use facade


class ComputerUse:
    """High-level API mirroring the original Doubao `computer_use` surface.

    Only one GAC server connection is used; it is created lazily per thread and
    reused.
    """

    _local = threading.local()

    def _get_client(self):
        client = getattr(ComputerUse._local, "client", None)
        if client is None:
            client = GacClient.from_env()
            ComputerUse._local.client = client
        return client

    def screenshot(self, show=False):
        parsed, png = self._get_client().transceive("screenshot", {})
        if not png:
            raise RuntimeError("screenshot returned no image")
        w, h, data = png
        return Frame(data, w, h)

    def list_apps(self):
        parsed, _ = self._get_client().transceive("list_apps", {})
        return parsed.get("apps", [])

    def launch_app(self, name):
        return self._get_client().transceive("launch_app", {"name": name})[0]

    def click(self, x, y, button="left", clicks=1):
        return self._get_client().transceive("click",
            {"x": x, "y": y, "button": button, "clicks": clicks})[0]

    def left_double(self, x, y):
        return self.click(x, y, button="left", clicks=2)

    def right_single(self, x, y):
        return self.click(x, y, button="right", clicks=1)

    def hover(self, x, y):
        return self._get_client().transceive("hover", {"x": x, "y": y})[0]

    def drag(self, x1, y1, x2, y2, duration_ms=1000):
        return self._get_client().transceive("drag",
            {"x1": x1, "y1": y1, "x2": x2, "y2": y2,
             "duration_ms": duration_ms})[0]

    def scroll(self, x, y, delta, horizontal=False):
        return self._get_client().transceive("scroll",
            {"x": x, "y": y, "delta": delta,
             "horizontal": horizontal})[0]

    def type(self, text):
        return self._get_client().transceive("type", {"text": text})[0]

    def hotkey(self, *keys):
        return self._get_client().transceive("hotkey", {"keys": list(keys)})[0]

    def press(self, key):
        return self._get_client().transceive("press", {"key": key})[0]

    def key_down(self, key):
        return self._get_client().transceive("key_down", {"key": key})[0]

    def key_up(self, key):
        return self._get_client().transceive("key_up", {"key": key})[0]

    def wait(self, seconds):
        return self._get_client().transceive("wait",
            {"duration_ms": float(seconds) * 1000.0})[0]

    def get_clipboard(self):
        parsed, _ = self._get_client().transceive("get_clipboard", {})
        return parsed.get("text", "")

    def set_clipboard(self, text):
        return self._get_client().transceive("set_clipboard",
            {"text": text})[0]

    def copy_selection(self):
        parsed, _ = self._get_client().transceive("copy_selection", {})
        return parsed.get("text", "")

    def switch_desktop(self, name):
        return self._get_client().transceive("switch_desktop",
            {"desktop": name})[0]
