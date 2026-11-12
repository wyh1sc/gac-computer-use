# GAC Computer Use (DSH plugin)

Computer Use / GUI automation for Windows, packaged as a **DSH plugin** for the
DSH plugin marketplace. It lets an agent drive the local machine — screenshot,
move the pointer, click, type, read/write the clipboard, launch apps, switch
desktops — through a set of Model Context Protocol (MCP) tools exposed to DSH.

This is a **self-contained reimplementation** of the GAC protocol v1 server and
client. No third-party runtime is redistributed: the wire crypto is the public
RFC 8439 ChaCha20-Poly1305, the protocol client is written from the wire
spec, and the Win32 backend uses only `ctypes` plus the Python standard
library. You do **not** need Doubao, `gac_runtime_extract`, or any pip package.

## What it does

- **`gac-mcp-bridge`** — a single stdio MCP server (`python/gac_server/mcp_bridge.py`)
  that boots its own GAC TCP server on a random loopback port and serves 18
  computer-use tools to DSH via the `@deepseek-ai/dsh-mcp-client` host.
- **`gac-pip-view`** — a sidebar tab + same-origin HTTP proxy that streams the
  live picture of an **isolated "pip" virtual desktop**. Apps launched there
  never touch your real screen, mouse, or keyboard.

### Tools exposed (`serverName: gac`)

| Tool | Purpose |
|------|---------|
| `gac_screenshot` | Capture the active plane (full screen on `main`, front window on `pip`). Returns a PNG. |
| `gac_list_apps` | List launchable Windows applications. |
| `gac_launch_app` | Launch an app by exact name. |
| `gac_click` / `gac_double_click` / `gac_right_click` | Click at normalized 0–1000 coordinates. |
| `gac_hover` | Move the pointer. |
| `gac_drag` | Drag between two normalized points. |
| `gac_scroll` | Scroll at a point. |
| `gac_type` | Type Unicode text. |
| `gac_hotkey` / `gac_press` | Key combination / single key. |
| `gac_wait` | Wait for milliseconds. |
| `gac_get_clipboard` / `gac_set_clipboard` | Clipboard read/write. |
| `gac_copy_selection` | Ctrl+C then read the clipboard. |
| `gac_switch_desktop` | Switch plane: `main` (real desktop) or `pip` (isolated). |
| `gac_handoff` | **Pause and hand control to a human** for login / 2FA / captcha / payment, with a "Done" button in the sidebar. |

## Platform

**Windows only.** The server drives Win32 APIs (`user32` / `gdi32` / `kernel32`)
through `ctypes`. Requires **Python 3.10+** on the machine (standard library
only — no `pip install` needed).

## Install

### From the marketplace
Install `gac-computer-use` from the DSH plugin marketplace, then run the
installer so the MCP command is bound to your Python:

```bash
cd <installed plugin dir>
node scripts/setup.mjs
```

Then reload the DSH profile (or restart DSH).

### Manual / local development
```bash
git clone <repo> gac-computer-use
cd gac-computer-use
node scripts/setup.mjs        # writes your Python path into cordis.patch.yml
# symlink or copy this folder into your DSH plugins directory, then reload
```

`setup.mjs` finds Python 3.10+ (`py -3` on Windows first, then `python3` /
`python`), resolves it to an absolute executable, warns if the OS is not
Windows, and rewrites `cordis.patch.yml` in place. Re-running is safe.

## How it fits together

```
DSH ──(stdio)──> mcp_bridge.py  ──(in-process TCP)──> GAC server (server.py)
                       │                                      │
                       │ ComputerUse client (gac_client.py)    │ RFC 8439 AEAD
                       │                                      │
                  sidebar tab  <──HTTP /gac-pip/*──  pip_view.py (MJPEG + handoff)
                       │                                      │
                  lib/client.js  <──proxy──  lib/index.js  <── 127.0.0.1:52360
```

- **Protocol**: TCP, `GACF` 4-byte magic + u32 length frames. A token-challenge
  HMAC-SHA256 handshake derives the session key via HKDF; every command is then
  sealed with ChaCha20-Poly1305 (RFC 8439). See `gac_crypto.py` and `server.py`.
- **Coordinates**: normalized to a 0–1000 space (the client scales to the real
  screen / window). This matches the upstream computer-use convention.
- **Pip desktop**: `gac_switch_desktop("pip")` moves subsequent work onto a
  private `HDESK`; the sidebar shows it live and lets you take over for
  sensitive steps via `gac_handoff`.

## Security model

- The server binds **loopback only** (`127.0.0.1`); nothing is reachable from
  the network.
- The session is authenticated by a shared secret (`HKDF(token, secret)`); the
  secret is generated per launch and passed to the client via environment
  variables scoped to the bridge process.
- `gac_handoff` exists precisely so the agent does **not** perform login / 2FA /
  payment on your behalf — it blocks and hands the keyboard to you.

## Development & tests

Self-contained end-to-end test (no external runtime), exercising all 19 command
paths through the real client and server in one process:

```bash
python python/gac_server/test_e2e.py
```

To run the MCP bridge manually against a DSH-like stdio transport:

```bash
python python/gac_server/mcp_bridge.py      # speaks JSON-RPC over stdin/stdout
```

## License

MIT — see [LICENSE](./LICENSE).
