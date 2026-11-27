// ---- shared helpers: injected into update-sac.js and diagnose-sac.js by the pack script ----
// Everything printed to the console stays ASCII: a Windows console in code page 936
// would turn Chinese output into mojibake. Chinese text only goes into files.
function execText(cmd, argv, timeout) {
  try {
    return execFileSync(cmd, argv, {
      encoding: 'utf8', timeout: timeout || 20000, windowsHide: true, stdio: ['ignore', 'pipe', 'ignore'],
    }) || '';
  } catch (e) { return ''; }
}
function asarFromExe(exePath) {
  if (!exePath) return null;
  let p = String(exePath).trim().replace(/^"|"$/g, '');
  if (!p) return null;
  if (/\.exe$/i.test(p)) p = path.dirname(p);
  p = p.replace(/[\\/]+$/, '');
  return path.join(p, 'resources', 'app.asar');
}
function regCandidates() {
  if (process.platform !== 'win32') return [];
  const out = [];
  const keys = [
    'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall',
    'HKLM\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall',
    'HKLM\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall',
  ];
  for (const key of keys) {
    const text = execText('reg', ['query', key, '/s', '/f', 'DeepSeek', '/t', 'REG_SZ']);
    for (const line of text.split(/\r?\n/)) {
      if (!/InstallLocation|DisplayIcon|UninstallString/i.test(line)) continue;
      const m = line.match(/REG_SZ\s+(.+)$/i);
      if (!m) continue;
      const c = asarFromExe(m[1].trim());
      if (c) out.push(c);
    }
  }
  return out;
}
function processCandidates() {
  if (process.platform !== 'win32') return [];
  const out = [];
  for (const img of ['DeepSeek Harness.exe', 'deepseek-harness.exe', 'DeepSeekHarness.exe']) {
    const text = execText('wmic', ['process', 'where', "name='" + img + "'", 'get', 'ExecutablePath', '/value']);
    for (const line of text.split(/\r?\n/)) {
      const m = line.match(/ExecutablePath=(.+)$/i);
      if (m) { const c = asarFromExe(m[1]); if (c) out.push(c); }
    }
  }
  return out;
}
function fsScanCandidates(maxDirs) {
  const out = [];
  const roots = [];
  if (process.platform === 'win32') {
    for (const r of [process.env.ProgramFiles, process.env['ProgramFiles(x86)'],
      process.env.ProgramW6432, process.env.LOCALAPPDATA, process.env.APPDATA, process.env.USERPROFILE]) {
      if (r) roots.push(r);
    }
    for (let d = 67; d <= 90; d++) {
      const drive = String.fromCharCode(d) + ':\\';
      try { if (fs.existsSync(drive)) roots.push(drive); } catch (e) {}
    }
  } else {
    roots.push('/Applications', path.join(os.homedir(), 'Applications'), '/opt', '/usr/local');
  }
  const skip = /^(windows|system32|syswow64|node_modules|\.git|\$recycle\.bin|proc|sys|dev|users)$/i;
  let visited = 0;
  const queue = roots.map((r) => ({ dir: r, depth: 0 }));
  while (queue.length && visited < (maxDirs || 3000)) {
    const item = queue.shift();
    visited++;
    try {
      if (fs.existsSync(path.join(item.dir, 'resources', 'app.asar'))) {
        out.push(path.join(item.dir, 'resources', 'app.asar'));
        if (out.length >= 3) break;
      }
      if (item.depth >= 3) continue;
      for (const e of fs.readdirSync(item.dir, { withFileTypes: true })) {
        if (!e.isDirectory() || skip.test(e.name)) continue;
        queue.push({ dir: path.join(item.dir, e.name), depth: item.depth + 1 });
      }
    } catch (err) {}
  }
  return out;
}
/** The header of an app.asar: { fd, tree, base } for the read helpers below. */
function openAsar(asarPath) {
  if (!asarPath || !fs.existsSync(asarPath)) return null;
  const fd = fs.openSync(asarPath, 'r');
  try {
    const head = Buffer.alloc(16);
    fs.readSync(fd, head, 0, 16, 0);
    const jsonLen = head.readUInt32LE(12);
    if (!jsonLen || jsonLen > 64 * 1024 * 1024) throw new Error('bad asar header');
    const buf = Buffer.alloc(jsonLen);
    fs.readSync(fd, buf, 0, jsonLen, 16);
    return { fd, tree: JSON.parse(buf.toString('utf8')), base: 12 + head.readUInt32LE(8) };
  } catch (e) { try { fs.closeSync(fd); } catch (_) {} return null; }
}
function asarNode(tree, p) {
  let n = tree;
  for (const s of p.split('/')) { n = n && n.files && n.files[s]; if (!n) return null; }
  return n;
}
function asarRead(ar, p) {
  const n = asarNode(ar.tree, p);
  if (!n || n.offset === undefined || n.size > 16 * 1024 * 1024) return null;
  const b = Buffer.alloc(n.size);
  fs.readSync(ar.fd, b, 0, n.size, ar.base + Number(n.offset));
  return b.toString('utf8');
}
function looksLikeDshAsar(p) {
  const ar = openAsar(p);
  if (!ar) return false;
  try { return !!asarNode(ar.tree, 'dsh/node_modules/@deepseek-ai/dsh-base/package.json'); } finally { try { fs.closeSync(ar.fd); } catch (e) {} }
}
function findAppAsar(explicit) {
  if (explicit) return explicit;
  const cands = [];
  const push = (p) => { if (p && cands.indexOf(p) < 0) cands.push(p); };
  try {
    let dir = path.dirname(process.execPath);
    for (let i = 0; i < 8; i++) {
      push(path.join(dir, 'app.asar'));
      const up = path.dirname(dir);
      if (up === dir) break;
      dir = up;
    }
  } catch (e) {}
  const roots = [
    process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, 'Programs'),
    process.env.ProgramFiles, process.env['ProgramFiles(x86)'],
    process.env.LOCALAPPDATA, process.env.APPDATA,
    process.env.USERPROFILE && path.join(process.env.USERPROFILE, 'AppData', 'Local', 'Programs'),
    process.env.ProgramW6432,
  ].filter(Boolean);
  for (const root of roots) {
    push(path.join(root, 'DeepSeek Harness', 'resources', 'app.asar'));
    try {
      for (const e of fs.readdirSync(root, { withFileTypes: true })) {
        if (!e.isDirectory() || !/deepseek|harness/i.test(e.name)) continue;
        push(path.join(root, e.name, 'resources', 'app.asar'));
      }
    } catch (err) {}
  }
  for (const p of regCandidates()) push(p);
  for (const p of processCandidates()) push(p);
  for (const c of cands) { if (fs.existsSync(c) && looksLikeDshAsar(c)) return c; }
  for (const p of fsScanCandidates(12000)) { if (fs.existsSync(p) && looksLikeDshAsar(p)) return p; }
  return null;
}
const PRESET_PKG = 'dsh/node_modules/@deepseek-ai/dsh-agent-preset/package.json';
const PRESETS_DIR = 'dsh/node_modules/@deepseek-ai/dsh-web-app/presets';
/**
 * What this client build can do. `preset` is false on builds without the
 * agent-preset plugin or without a standard preset definition to derive from.
 */
function clientCapabilities(asarPath) {
  const caps = { asar: asarPath || null, preset: false, standardPreset: null, compaction: false, compactRegion: null, dshVersion: null };
  if (!asarPath) return caps;
  const ar = openAsar(asarPath);
  if (!ar) return caps;
  try {
    caps.preset = !!asarNode(ar.tree, PRESET_PKG);
    const cli = safeJson(asarRead(ar, 'dsh/node_modules/@deepseek-ai/dsh/package.json'));
    caps.dshVersion = cli ? cli.version : null;
    caps.compaction = !!asarNode(ar.tree, 'dsh/node_modules/@deepseek-ai/dsh-compaction-basic/package.json');
    const lib = caps.compaction ? asarRead(ar, 'dsh/node_modules/@deepseek-ai/dsh-compaction-basic/lib/index.js') : null;
    caps.compactRegion = lib ? lib.indexOf('compactRegion') >= 0 : null;
    const dir = asarNode(ar.tree, PRESETS_DIR);
    const names = dir && dir.files ? Object.keys(dir.files).filter((n) => /\.(ya?ml)$/i.test(n)) : [];
    for (const name of names) {
      const text = asarRead(ar, PRESETS_DIR + '/' + name);
      if (text && /preset-standard/.test(text)) { caps.standardPreset = text; break; }
    }
  } finally { try { fs.closeSync(ar.fd); } catch (e) {} }
  caps.preset = caps.preset && !!caps.standardPreset;
  return caps;
}
function safeJson(text) { try { return JSON.parse(text); } catch (e) { return null; } }
/** Remove a trailing `# comment` that stands outside quotes. */
function stripComment(line) {
  let quote = null;
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (quote) { if (ch === quote) quote = null; } else if (ch === "'" || ch === '"') quote = ch;
    else if (ch === '#' && (i === 0 || /\s/.test(line[i - 1]))) return line.slice(0, i);
  }
  return line;
}
function parseScalar(raw, where) {
  const text = raw.trim();
  if ((text.charAt(0) === "'" && text.charAt(text.length - 1) === "'") || (text.charAt(0) === '"' && text.charAt(text.length - 1) === '"')) {
    return text.slice(1, -1);
  }
  if (text === 'true' || text === 'false') return text === 'true';
  if (/^-?\d+(\.\d+)?$/.test(text)) return Number(text);
  if (/^[A-Za-z_][\w.-]*$/.test(text)) return text;
  throw new Error(where + ': unsupported value "' + text + '"');
}
/**
 * The `config:` block of the `sac-compact` entry of a loader patch, as an
 * object. Understands `key: value` and block lists of scalars, the only forms
 * these blocks use, and throws on anything else.
 */
function readSacConfig(patchText) {
  const lines = String(patchText).split(/\r?\n/);
  const indentOf = (line) => line.length - line.replace(/^ +/, '').length;
  const entry = lines.findIndex((line) => /^\s*- id:\s*sac-compact\s*$/.test(stripComment(line)));
  if (entry < 0) throw new Error('no `- id: sac-compact` entry in the patch');
  const entryIndent = indentOf(lines[entry]);
  let configIndex = -1;
  for (let i = entry + 1; i < lines.length; i += 1) {
    const line = stripComment(lines[i]);
    if (!line.trim()) continue;
    if (indentOf(lines[i]) <= entryIndent) break;
    if (indentOf(lines[i]) === entryIndent + 2 && /^config:\s*$/.test(line.trim())) { configIndex = i; break; }
  }
  if (configIndex < 0) throw new Error('the sac-compact entry has no config block');
  const configIndent = indentOf(lines[configIndex]);
  const out = {};
  let list = null;
  for (let i = configIndex + 1; i < lines.length; i += 1) {
    const raw = lines[i];
    const line = stripComment(raw);
    if (!line.trim()) continue;
    const indent = indentOf(raw);
    if (indent <= configIndent) break;
    const where = 'line ' + (i + 1);
    if (indent === configIndent + 4 && /^\s*-\s/.test(line)) {
      if (!list) throw new Error(where + ': list item outside a list');
      list.push(parseScalar(line.replace(/^\s*-\s*/, ''), where));
      continue;
    }
    if (indent !== configIndent + 2) throw new Error(where + ': unexpected indentation in the sac-compact config');
    const match = line.trim().match(/^([A-Za-z][\w]*):\s*(.*)$/);
    if (!match) throw new Error(where + ': expected "key: value", got "' + line.trim() + '"');
    if (match[2] === '') { list = []; out[match[1]] = list; } else { out[match[1]] = parseScalar(match[2], where); list = null; }
  }
  return out;
}
/** Deep equality of plain objects and arrays, keys in the same order. */
function sameValue(a, b) {
  if (a === b) return true;
  if (typeof a !== typeof b || a === null || b === null || typeof a !== 'object') return false;
  const ka = Object.keys(a), kb = Object.keys(b);
  if (ka.length !== kb.length) return false;
  for (let i = 0; i < ka.length; i += 1) {
    if (ka[i] !== kb[i]) return false;
    if (!sameValue(a[ka[i]], b[kb[i]])) return false;
  }
  return true;
}
/** A config value as it is written into a generated preset (plain scalars and lists only). */
function yamlScalar(value) {
  if (typeof value === 'string') return "'" + value.replace(/'/g, "''") + "'";
  return String(value);
}
function emitConfig(config, pad) {
  const lines = [];
  for (const key of Object.keys(config)) {
    const value = config[key];
    if (Array.isArray(value)) {
      lines.push(pad + key + ':');
      for (const item of value) lines.push(pad + '  - ' + yamlScalar(item));
    } else lines.push(pad + key + ': ' + yamlScalar(value));
  }
  return lines;
}
