#!/usr/bin/env node
/**
 * setup.mjs — installer for the GAC Computer Use DSH plugin.
 *
 * What it does (no vendored downloads — the Python server is stdlib + ctypes):
 *   1. Locate a usable Python 3.10+ and resolve it to an absolute executable.
 *   2. Rewrite this plugin's cordis.patch.yml, replacing the {{PYTHON}} and
 *      {{PLUGIN_ROOT}} placeholders with the absolute machine paths, so DSH can
 *      launch the MCP server. (A published patch must not hardcode a user's
 *      machine path; the installer writes them — same rule windows-mcp-bridge
 *      follows.)
 *
 * Re-running is safe: it re-detects and re-writes idempotently.
 *
 * Usage:  node scripts/setup.mjs
 */
import { spawnSync } from 'node:child_process';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir, platform } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PLUGIN_ROOT = path.resolve(path.dirname(HERE));
const PATCH = path.join(PLUGIN_ROOT, 'cordis.patch.yml');

function log(msg) { process.stdout.write('[gac setup] ' + msg + '\n'); }
function warn(msg) { process.stdout.write('  ! ' + msg + '\n'); }

/** Split a candidate that may carry flags (e.g. "py -3") into [cmd, ...flags]. */
function toArgv(candidate) {
  const parts = candidate.trim().split(/\s+/);
  return { cmd: parts[0], flags: parts.slice(1) };
}

/** Try one python candidate; return { exe, args, version } or null. */
function tryPython(candidate) {
  const { cmd, flags } = toArgv(candidate);
  if (!cmd) return null;
  const probe = spawnSync(cmd, [...flags, '--version'], { encoding: 'utf8' });
  if (probe.status !== 0) return null;
  const text = (probe.stdout || '') + (probe.stderr || '');
  const m = text.match(/Python\s+(\d+)\.(\d+)(?:\.(\d+))?/);
  if (!m) return null;
  const major = Number(m[1]);
  const minor = Number(m[2]);
  if (major < 3 || (major === 3 && minor < 10)) {
    return { tooOld: true, version: `${major}.${minor}` };
  }
  // Resolve the real interpreter path so the patch carries an absolute command.
  const resolve = spawnSync(cmd, [...flags, '-c', 'import sys;print(sys.executable)'], {
    encoding: 'utf8',
  });
  const exe = (resolve.stdout || '').trim();
  if (!exe || !existsSync(exe)) return { exe: cmd, args: flags, version: `${major}.${minor}` };
  return { exe, args: flags, version: `${major}.${minor}` };
}

function detectPython() {
  const candidates = [];
  if (process.env.GAC_PYTHON && existsSync(process.env.GAC_PYTHON)) {
    candidates.push(process.env.GAC_PYTHON);
  }
  // Try every spelling on every platform; a missing interpreter simply fails its
  // probe and is skipped, so including `py` on non-Windows (and `python3` on
  // Windows) is harmless and avoids depending on os.platform() reporting win32.
  candidates.push('py -3', 'py', 'python3', 'python');
  let tooOld = null;
  for (const c of candidates) {
    const r = tryPython(c);
    if (!r) continue;
    if (r.tooOld) { tooOld = r; continue; }
    return r;
  }
  return tooOld || null;
}

function main() {
  log('GAC Computer Use installer');
  log('plugin root : ' + PLUGIN_ROOT);

  if (platform() !== 'win32') {
    warn('This plugin is Windows-only: the server drives Win32 APIs via ctypes.');
    warn('On this OS the MCP tools will fail to start. Install on Windows to use them.');
  }

  const py = detectPython();
  if (!py) {
    process.stdout.write(
      '\n  Could not find Python 3.10+ on this machine.\n' +
      '  Install Python 3.10+ (https://www.python.org/downloads/) and re-run:\n' +
      '    node scripts/setup.mjs\n' +
      '  Or set GAC_PYTHON to the interpreter, e.g.:\n' +
      '    GAC_PYTHON="C:\\\\Python312\\\\python.exe" node scripts/setup.mjs\n\n',
    );
    process.exit(1);
  }
  if (py.tooOld) {
    process.stdout.write(
      `\n  Found Python ${py.version}, but GAC needs 3.10 or newer.\n` +
      '  Install Python 3.10+ and re-run setup.\n\n',
    );
    process.exit(1);
  }
  log(`python      : ${py.exe}  (${py.version})`);

  if (!existsSync(PATCH)) {
    warn('cordis.patch.yml not found at ' + PATCH + ' — nothing to patch.');
    writeNextSteps();
    return;
  }

  const mcpScript = path.join(PLUGIN_ROOT, 'python', 'gac_server', 'mcp_bridge.py');
  if (!existsSync(mcpScript)) {
    warn('mcp_bridge.py not found at ' + mcpScript + ' — the plugin is corrupt.');
    process.exit(1);
  }

  let tmpl = readFileSync(PATCH, 'utf8');
  const before = tmpl;
  // Windows: paths may contain backslashes; YAML single-quoted scalars accept
  // them literally, so native separators are fine here.
  tmpl = tmpl.split('{{PYTHON}}').join(py.exe);
  tmpl = tmpl.split('{{PLUGIN_ROOT}}').join(PLUGIN_ROOT);
  if (tmpl === before) {
    log('cordis.patch.yml already patched (no placeholders left) — leaving as-is.');
  } else {
    writeFileSync(PATCH, tmpl, 'utf8');
    log('wrote MCP command into cordis.patch.yml');
  }

  // Record the discovered Python next to the patch so a future run (or a
  // mirror/upgrade) can re-derive it without re-probing PATH.
  try {
    writeFileSync(
      path.join(PLUGIN_ROOT, '.gac-setup.json'),
      JSON.stringify({ python: py.exe, version: py.version, at: Date.now() }, null, 2) + '\n',
      'utf8',
    );
  } catch {
    /* best-effort record; not required */
  }

  writeNextSteps();
}

function writeNextSteps() {
  process.stdout.write(
    '\n  Done. To finish activating the plugin:\n' +
    '    1. Reload the DSH profile (or restart DSH).\n' +
    '    2. The "gac" MCP server will expose the computer-use tools.\n' +
    '    3. Open the "pip 桌面" sidebar tab for the live isolated-desktop view.\n\n' +
    '  Optional: GAC_PYTHON to pin an interpreter; on Windows "py -3" is tried first.\n',
  );
}

main();
