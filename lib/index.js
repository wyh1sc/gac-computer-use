/**
 * gac-pip-view - server half.
 *
 * The GAC pip plane renders on a private HDESK and publishes an MJPEG feed from
 * a Python HTTP server on loopback (port + pid are recorded in
 * ~/.dsh/gac/pip-view.json by gac_server/pip_view.py).
 *
 * This plugin re-serves that feed on DSH's own origin (/gac-pip/*) so the
 * sidebar <img> is a same-origin request: no CORS preflight, and no CSP
 * surprises from pointing an Electron renderer at http://127.0.0.1:52360.
 */
import http from 'node:http';
import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

export const name = 'gac-pip-view';
export const inject = ['webServer'];

const STATE_FILE = join(homedir(), '.dsh', 'gac', 'pip-view.json');
const FALLBACK_PORT = 52360;

function upstreamPort() {
  try {
    const raw = readFileSync(STATE_FILE, 'utf8').replace(/^\uFEFF/, '');
    const parsed = JSON.parse(raw);
    if (parsed && Number(parsed.port) > 0) return Number(parsed.port);
  } catch (error) {
    // The pip server has not started yet; fall back to the preferred port.
  }
  return FALLBACK_PORT;
}

function fail(res, code, text) {
  try {
    res.writeHead(code, {
      'content-type': 'text/plain; charset=utf-8',
      'cache-control': 'no-store',
    });
    res.end(text);
  } catch (error) {
    // Socket already gone - nothing to report to.
  }
}

function forward(req, res, upstreamPath) {
  const port = upstreamPort();
  let answered = false;

  const headers = { accept: req.headers.accept || '*/*' };
  if (req.headers['content-type']) headers['content-type'] = req.headers['content-type'];
  if (req.headers['content-length']) headers['content-length'] = req.headers['content-length'];

  const upstream = http.request(
    {
      host: '127.0.0.1',
      port,
      path: upstreamPath,
      method: req.method,
      headers,
    },
    (upRes) => {
      answered = true;
      if (upstreamPath === '/stream') {
        res.writeHead(200, {
          'content-type': upRes.headers['content-type'] || 'multipart/x-mixed-replace; boundary=frame',
          'cache-control': 'no-store',
          connection: 'close',
        });
      } else {
        const replyHeaders = { ...upRes.headers };
        delete replyHeaders.connection;
        delete replyHeaders['transfer-encoding'];
        res.writeHead(upRes.statusCode || 502, replyHeaders);
      }
      upRes.pipe(res);
      upRes.on('error', () => {
        try { res.destroy(); } catch (error) { /* ignore */ }
      });
    },
  );

  upstream.on('error', (err) => {
    if (answered) return;
    fail(res, 503,
      'pip view is not running on 127.0.0.1:' + port + ' (' + err.message + '). ' +
      'Switch to the pip plane first, e.g. gac_switch_desktop("pip").');
  });

  // Destroy the upstream socket as soon as the browser stops listening,
  // otherwise every tab switch would leak one MJPEG connection.
  res.on('close', () => {
    try { upstream.destroy(); } catch (error) { /* ignore */ }
  });

  // Stream the request body through - needed for POST /handoff and /input.
  req.pipe(upstream);
}

const ROUTES = {
  '/gac-pip/stream': '/stream',
  '/gac-pip/frame.png': '/frame.png',
  '/gac-pip/status': '/status',
  '/gac-pip/handoff': '/handoff',
  '/gac-pip/handoff/done': '/handoff/done',
  '/gac-pip/input': '/input',
};

const GET_ONLY = ['/gac-pip/stream', '/gac-pip/frame.png', '/gac-pip/status'];

export function apply(ctx) {
  const disposers = Object.keys(ROUTES).map((path) =>
    ctx.webServer.register({
      kind: 'exact',
      path,
      handler: (req, res) => {
        const method = String(req.method || 'GET').toUpperCase();
        const allowed = GET_ONLY.indexOf(path) >= 0
          ? (method === 'GET' || method === 'HEAD')
          : (method === 'GET' || method === 'POST' || method === 'HEAD');
        if (!allowed) {
          fail(res, 405, 'method not allowed');
          return;
        }
        forward(req, res, ROUTES[path]);
      },
    }),
  );

  return () => {
    for (const dispose of disposers) {
      try {
        if (typeof dispose === 'function') dispose();
      } catch (error) {
        // ignore
      }
    }
  };
}
