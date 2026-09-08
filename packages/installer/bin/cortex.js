#!/usr/bin/env node
"use strict";

// Canonical successor to the dependency-free public 0.0.1 launcher. Release
// publication sets PAYLOAD only after its archive identity has been verified.
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const crypto = require("node:crypto");
const zlib = require("node:zlib");
const https = require("node:https");
const { Transform } = require("node:stream");
const { pipeline } = require("node:stream/promises");
const { fileURLToPath } = require("node:url");
const { spawnSync } = require("node:child_process");
const VERSION = "0.1.003";
const PAYLOAD = null;
const MAX_ARCHIVE = 256 * 1024 * 1024;
const HEX = /^[a-f0-9]{64}$/;
const TEMPLATE_SHA = "fd25fc78a15f85a47fa8bae91179dcea3892cc37f81cd42186face03e962fc64";
const TEMPLATE_SOURCE = "f3660f3c19939d2a6ff3b95be9aab3f85fb8312a";

function refuse(message) { throw new Error(message); }
function digest(bytes) { return crypto.createHash("sha256").update(bytes).digest("hex"); }
function exists(name) { try { fs.lstatSync(name); return true; } catch (e) { if (e.code === "ENOENT") return false; throw e; } }
function noSymlinkParents(name) {
  const absolute = path.resolve(name);
  let part = path.parse(absolute).root;
  for (const component of absolute.slice(part.length).split(path.sep).filter(Boolean)) {
    part = path.join(part, component);
    if (exists(part) && fs.lstatSync(part).isSymbolicLink()) refuse("symlink paths are not supported");
  }
  return absolute;
}
function privateDir(name) {
  const absolute = noSymlinkParents(name);
  fs.mkdirSync(absolute, { recursive: true, mode: 0o700 });
  const st = fs.lstatSync(absolute);
  if (!st.isDirectory() || st.uid !== process.getuid() || (st.mode & 0o777) !== 0o700) refuse("directory must be owned by this user with mode 0700");
  return absolute;
}
function regular(name, max = MAX_ARCHIVE, privateFile = false) {
  noSymlinkParents(name);
  const fd = fs.openSync(name, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  try {
    const st = fs.fstatSync(fd);
    if (!st.isFile() || st.nlink !== 1 || st.size > max || (privateFile && (st.uid !== process.getuid() || (st.mode & 0o777) !== 0o600))) refuse("unsafe file type, size, ownership or permissions");
    return fs.readFileSync(fd);
  } finally { fs.closeSync(fd); }
}
function syncDir(name) { const fd = fs.openSync(name, "r"); try { fs.fsyncSync(fd); } finally { fs.closeSync(fd); } }
function syncFile(name) { const fd = fs.openSync(name, "r"); try { fs.fsyncSync(fd); } finally { fs.closeSync(fd); } }
function atomicJSON(name, value) {
  const tmp = `${name}.${crypto.randomUUID()}.tmp`;
  const fd = fs.openSync(tmp, "wx", 0o600);
  try { fs.writeFileSync(fd, JSON.stringify(value, null, 2) + "\n"); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
  fs.renameSync(tmp, name);
  syncDir(path.dirname(name));
}
function parseURL(raw, local) {
  if (typeof raw !== "string" || raw.length > 8192 || /[\u0000-\u0020\u007f]/.test(raw)) refuse("payload URL is invalid");
  let url; try { url = new URL(raw); } catch { refuse("payload URL must be absolute"); }
  if (url.username || url.password || url.hash) refuse("payload URL credentials and fragments are forbidden");
  if (url.protocol === "file:") {
    if (!local || url.hostname || url.search) refuse("file URLs require an explicit local --payload manifest");
    noSymlinkParents(fileURLToPath(url));
  } else if (url.protocol !== "https:" || !url.hostname) refuse("payload downloads require HTTPS");
  return url;
}
function manifest(value, local = false) {
  if (!value || value.delivery_kind !== 'prebuilt' || !HEX.test(value.installation_sha256 || '') || !['linux/arm64', 'linux/amd64'].includes(value.platform)) refuse('installation requires a verified prebuilt release; source-only archives cannot run');
  if (!value || value.schema !== "cortex.release.v1" || !/^\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?$/.test(value.version || "") || !/^[a-f0-9]{40}$/.test(value.source_revision || "") || !HEX.test(value.sha256 || "") || !HEX.test(value.schema_revision || "")) refuse("invalid Cortex release manifest");
  if (!Array.isArray(value.compatible_schema_revisions) || !value.compatible_schema_revisions.length || value.compatible_schema_revisions.length > 1024 || !value.compatible_schema_revisions.every(x => typeof x === "string" && HEX.test(x)) || !value.compatible_schema_revisions.includes(value.schema_revision)) refuse("manifest must declare exact compatible schema revisions including its own");
  parseURL(value.url, local);
  return { schema: value.schema, version: value.version, source_revision: value.source_revision, schema_revision: value.schema_revision, compatible_schema_revisions: [...new Set(value.compatible_schema_revisions)], url: value.url, sha256: value.sha256, delivery_kind: value.delivery_kind, installation_sha256: value.installation_sha256, platform: value.platform };
}
async function download(url, target, local, get = https.get) {
  url = parseURL(url, local);
  if (url.protocol === "file:") { fs.writeFileSync(target, regular(fileURLToPath(url)), { flag: "wx", mode: 0o600 }); return; }
  const controller = new AbortController();
  // One wall-clock deadline covers every hop and the complete response body.
  const deadline = setTimeout(() => controller.abort(new Error("payload download timed out")), 30000);
  try {
    for (let redirects = 0; ; redirects++) {
      let request, response, output;
      try {
        response = await new Promise((resolve, reject) => {
          // No credentials or request headers are propagated to an asset host.
          request = get(url, { timeout: 30000, signal: controller.signal }, resolve);
          request.on("timeout", () => request.destroy(new Error("payload download timed out")));
          request.on("error", reject);
        });
        if ([301, 302, 303, 307, 308].includes(response.statusCode)) {
          if (redirects >= 5) refuse("payload redirect limit exceeded");
          const location = response.headers.location;
          if (typeof location !== "string" || !location || location.length > 8192 || /[\u0000-\u0020\u007f]/.test(location)) refuse("payload redirect URL is invalid");
          let next; try { next = new URL(location, url).href; } catch { refuse("payload redirect URL is invalid"); }
          url = parseURL(next, false);
          continue;
        }
        if (response.statusCode !== 200) refuse("payload download requires HTTP 200");
        let total = 0;
        const bounded = new Transform({ transform(chunk, _encoding, callback) {
          total += chunk.length;
          callback(total > MAX_ARCHIVE ? new Error("payload exceeds archive size limit") : null, chunk);
        } });
        output = fs.createWriteStream(target, { flags: "wx", mode: 0o600 });
        await pipeline(response, bounded, output, { signal: controller.signal });
        return;
      } finally {
        if (response) response.destroy();
        if (output) output.destroy();
        if (request) request.destroy();
      }
    }
  } finally { clearTimeout(deadline); }
}

// USTAR only: validate the COMPLETE table before writing any member. PAX/GNU
// extensions, links, devices, FIFOs, sparse files and duplicate names fail closed.
function tarEntries(archive) {
  let data;
  try { data = zlib.gunzipSync(archive, { maxOutputLength: MAX_ARCHIVE }); } catch { refuse("payload must be a bounded gzip-compressed USTAR archive"); }
  const entries = [], names = new Map();
  let offset = 0, ended = false;
  const field = (h, a, b) => {
    const bytes = h.subarray(a, b), nul = bytes.indexOf(0);
    const str = bytes.subarray(0, nul < 0 ? bytes.length : nul).toString("utf8");
    if (str.includes("\ufffd")) refuse("invalid archive text encoding");
    return str;
  };
  const octal = (h, a, b) => {
    const text = field(h, a, b).trim();
    if (!/^[0-7]+$/.test(text)) refuse("invalid archive numeric field");
    return parseInt(text, 8);
  };
  while (offset + 512 <= data.length) {
    const h = data.subarray(offset, offset + 512);
    if (h.every(b => b === 0)) {
      if (data.length - offset < 1024 || !data.subarray(offset).every(b => b === 0)) refuse("invalid archive terminator");
      ended = true; break;
    }
    if (field(h, 257, 263) !== "ustar") refuse("only USTAR archives are supported");
    const sum = h.reduce((s, b, i) => s + (i >= 148 && i < 156 ? 32 : b), 0);
    if (sum !== octal(h, 148, 156)) refuse("archive header checksum mismatch");
    const type = String.fromCharCode(h[156]);
    if (!["0", "\0", "5"].includes(type)) refuse("archive links, extensions and special files are forbidden");
    const size = octal(h, 124, 136), mode = octal(h, 100, 108);
    let name = [field(h, 345, 500), field(h, 0, 100)].filter(Boolean).join("/");
    if (name.startsWith("./")) name = name.slice(2);
    if (type === "5" && name.endsWith("/")) name = name.slice(0, -1);
    if (size > MAX_ARCHIVE || offset + 512 + size > data.length || (type === "5" && size !== 0) || (mode & 0o7000)) refuse("unsafe archive member size or mode");
    if ((!name || name === ".") && type === "5") { offset += 512; continue; }
    if (!name || name.startsWith("/") || name.includes("\\") || /[\x00-\x1f\x7f]/.test(name) || name.split("/").some(p => !p || p === "." || p === "..") || name.length > 4096) refuse("unsafe archive member path");
    if (names.has(name)) refuse("duplicate archive member");
    names.set(name, type);
    entries.push({ name, dir: type === "5", executable: Boolean(mode & 0o111), bytes: data.subarray(offset + 512, offset + 512 + size) });
    if (entries.length > 20000) refuse("too many archive members");
    offset += 512 + Math.ceil(size / 512) * 512;
  }
  if (!ended) refuse("truncated archive");
  for (const entry of entries) {
    let parent = path.posix.dirname(entry.name);
    while (parent !== ".") { if (names.has(parent) && names.get(parent) !== "5") refuse("archive file/directory collision"); parent = path.posix.dirname(parent); }
  }
  if (!entries.some(x => x.name === "packages/deploy/cortex-runtime" && !x.dir && x.executable)) refuse("payload has no executable packages/deploy/cortex-runtime");
  return entries;
}
function extract(archive, directory) {
  const entries = tarEntries(archive);
  fs.mkdirSync(directory, { mode: 0o700 });
  for (const e of entries) {
    const target = path.join(directory, e.name);
    fs.mkdirSync(e.dir ? target : path.dirname(target), { recursive: true, mode: 0o700 });
    if (!e.dir) fs.writeFileSync(target, e.bytes, { flag: "wx", mode: e.executable ? 0o700 : 0o600 });
  }
}
function verifyExtracted(archive, directory) {
  const entries = tarEntries(archive), expected = new Set();
  for (const e of entries) {
    let name = e.name;
    while (name !== ".") { expected.add(name); name = path.posix.dirname(name); }
    const target = path.join(directory, e.name);
    noSymlinkParents(target);
    const st = fs.lstatSync(target);
    if (e.dir ? !st.isDirectory() : !st.isFile() || !regular(target).equals(e.bytes) || Boolean(st.mode & 0o111) !== e.executable) refuse("cached payload no longer matches its archive");
  }
  function walk(dir, prefix = "") {
    for (const name of fs.readdirSync(dir)) {
      const relative = prefix ? `${prefix}/${name}` : name, target = path.join(dir, name), st = fs.lstatSync(target);
      if (!expected.has(relative) || st.isSymbolicLink()) refuse("unexpected cached payload member");
      if (st.isDirectory()) walk(target, relative);
    }
  }
  walk(directory);
}
function payloadIdentity(release, directory) {
  let identity;
  try { identity = JSON.parse(regular(path.join(directory, "packages/deploy/release.json"), 64 * 1024)); }
  catch { refuse("payload must contain packages/deploy/release.json"); }
  if (identity.schema !== "cortex.payload.v1" || ["version", "source_revision", "schema_revision"].some(key => identity[key] !== release[key])) refuse("embedded payload identity disagrees with the release manifest");
  const bytes = regular(path.join(directory, 'packages/deploy/install.json'), 2 * 1024 * 1024);
  if (digest(bytes) !== release.installation_sha256) refuse('prebuilt installation identity checksum mismatch');
  const install = JSON.parse(bytes);
  if (install.schema !== 'cortex.install.v1' || install.delivery_kind !== 'prebuilt' || ['version', 'source_revision', 'schema_revision', 'platform'].some(key => install[key] !== release[key])) refuse('prebuilt installation identity disagrees with release');
  const required = ['PROJECTION_MANIFEST.json', 'packages/deploy/release.json', 'packages/deploy/cortex-runtime', 'packages/deploy/image_manifest.py', 'packages/deploy/install-compose.json', 'packages/deploy/image-lock.json'];
  if (!install.files || typeof install.files !== 'object' || Array.isArray(install.files) || !required.every(key => Object.hasOwn(install.files, key))) refuse('prebuilt installation inventory is incomplete');
  for (const [member, expected] of Object.entries(install.files)) {
    if (member.startsWith('/') || member.includes('\\') || member.split('/').some(part => !part || part === '.' || part === '..') || !HEX.test(expected || '')) refuse('invalid prebuilt installation file inventory');
    if (digest(regular(path.join(directory, member))) !== expected) refuse('prebuilt installation file checksum mismatch');
  }
}

function materialiseProvider(home = process.env.OPENKAI_HOME || path.join(os.homedir(), ".openkai")) {
  if (!path.isAbsolute(home)) refuse("OPENKAI_HOME must be absolute");
  privateDir(home);
  const target = path.join(home, ".env");
  if (exists(target)) { regular(target, 256 * 1024, true); return { authored: false, path: target }; }
  const template = regular(path.join(__dirname, "../templates/openkai.env.example"), 256 * 1024);
  if (digest(template) !== TEMPLATE_SHA) refuse("pinned OpenKai template checksum mismatch");
  try {
    // An exclusive create makes first-author ownership atomic across products.
    const fd = fs.openSync(target, "wx", 0o600);
    try { fs.writeFileSync(fd, template); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
  } catch (e) {
    if (e.code !== "EEXIST") throw e;
    regular(target, 256 * 1024, true);
    return { authored: false, path: target };
  }
  syncDir(home);
  const receipt = path.join(home, "cortex-provider-bootstrap.json");
  if (!exists(receipt)) atomicJSON(receipt, { schema: "cortex.provider-bootstrap.v1", author: "cortex", source_repo: "Kaidera-AI/openkai", source_revision: TEMPLATE_SOURCE, source_path: ".env.example", sha256: TEMPLATE_SHA });
  return { authored: true, path: target };
}

function command(name, args) {
  const result = spawnSync(name, args, { env: { ...process.env }, encoding: "utf8", timeout: 30000, maxBuffer: 1024 * 1024 });
  return { ok: !result.error && result.status === 0, out: (result.stdout || "").trim() };
}
function preflightChecks(platform = os.platform(), run = command) {
  const checks = [];
  const check = (name, ok, detail, remedy) => checks.push({ name, ok, detail, ...(ok ? {} : { remedy }) });
  if (!["linux", "darwin"].includes(platform)) return [{ name: "platform", ok: false, detail: platform, remedy: "use macOS or Linux with Podman" }];
  const version = run("podman", ["--version"]);
  check("podman >= 5.0", version.ok && Number((version.out.match(/(\d+)\.\d+\.\d+/) || [])[1]) >= 5, version.out || "not found", "install Podman >= 5.0");
  if (!checks[0].ok) return checks;
  const info = run("podman", ["info", "--format", "{{.Host.Security.Rootless}}"]);
  check("rootless Podman running", info.ok && info.out === "true", info.out || "unavailable", platform === "darwin" ? "start a rootless Podman machine" : "start Podman as your ordinary user");
  const cgroup = run("podman", ["info", "--format", "{{.Host.CgroupManager}}"]);
  check("cgroup manager systemd", cgroup.ok && cgroup.out === "systemd", cgroup.out || "unknown", "configure Podman systemd cgroups");
  const compose = run("podman-compose", ["--version"]);
  check("podman-compose", compose.ok, compose.out || "unavailable", "install podman-compose, the runtime's compose provider");
  const python = run("python3", ["--version"]);
  const pythonVersion = (python.out.match(/Python (\d+)\.(\d+)/) || []).slice(1).map(Number);
  check("Python >= 3.10", python.ok && pythonVersion.length === 2 && (pythonVersion[0] > 3 || (pythonVersion[0] === 3 && pythonVersion[1] >= 10)), python.out || "unavailable", "install Python >= 3.10 for the standalone runtime");
  if (platform === "linux") {
    const linger = run("loginctl", ["show-user", String(process.getuid()), "-p", "Linger", "--value"]);
    check("linger enabled", linger.ok && linger.out === "yes", linger.out || "unknown", `loginctl enable-linger ${process.getuid()}`);
  }
  return checks;
}
function runtime(release, action, options, schema, rollback = false) {
  const executable = path.join(options.stateDir, "releases", release.sha256, "packages/deploy/cortex-runtime");
  const args = [action, "--project", options.project, "--state-dir", options.stateDir, "--payload-dir", path.dirname(path.dirname(path.dirname(executable))), "--api-port", String(options.apiPort)];
  if (schema) args.push("--schema-revision", schema);
  if (rollback) args.push("--rollback");
  if (["backup", "restore"].includes(action)) args.push("--backup-file", options.backupFile);
  if (action === "restore" && options.confirmRestore) args.push("--confirm-restore");
  const result = spawnSync(executable, args, { encoding: "utf8", timeout: 30 * 60 * 1000, maxBuffer: 2 * 1024 * 1024, env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" } });
  if (result.stderr) process.stderr.write(result.stderr);
  if (result.error || result.status !== 0) refuse(`runtime ${action} failed; the pending journal preserves the recovery target`);
  if (action === "up") return;
  let receipt; try { receipt = JSON.parse(result.stdout); } catch { refuse("runtime check returned no valid JSON receipt"); }
  if (action === 'prepare-images') {
    if (!receipt || receipt.images_prepared !== true || receipt.platform !== release.platform || receipt.source_revision !== release.source_revision) refuse('runtime did not prove prepared image identity');
    return receipt;
  }
  if (["backup", "restore"].includes(action)) return receipt;
  if (action === "schema-status") {
    if (!receipt || !HEX.test(receipt.schema_revision || "")) refuse("runtime schema-status returned no valid database inventory");
    return receipt;
  }
  if (!receipt || receipt.healthy !== true || !HEX.test(receipt.schema_revision || "") || typeof receipt.discovery_url !== "string") refuse("runtime check did not prove healthy schema identity");
  return receipt;
}
function stateRecord(value) {
  if (!value || value.schema !== "cortex.installer-state.v1" || !/^[a-z][a-z0-9-]{0,62}$/.test(value.project || "") || !Number.isInteger(value.api_port) || value.api_port < 1024 || value.api_port > 65535) refuse("invalid installer state");
  for (const key of ["current", "previous", "pending"]) if (value[key]) value[key] = manifest(value[key], true);
  if (value.schema_revision !== null && !HEX.test(value.schema_revision || "")) refuse("invalid recorded schema revision");
  return value;
}
async function install(options, get = https.get) {
  if (!options.rollback && !options.payload && !PAYLOAD) { process.stderr.write("cortex install: REFUSED — no published digest-pinned release payload exists. Use --payload FILE only for explicit release qualification.\n"); return 2; }
  if (options.rollback && !exists(path.join(options.stateDir, "state.json"))) { process.stderr.write("cortex install --rollback: REFUSED — no previous verified payload exists.\n"); return 2; }
  const targetInput = options.payload ? manifest(JSON.parse(regular(path.resolve(options.payload), 64 * 1024)), true) : PAYLOAD;
  const checks = preflightChecks();
  if (!checks.every(c => c.ok)) { process.stderr.write(JSON.stringify({ ok: false, checks }) + "\n"); return 1; }
  options.stateDir = privateDir(options.stateDir);
  if (options.stateDir === os.homedir() || options.stateDir === path.parse(options.stateDir).root) refuse("installer state requires a dedicated directory");
  const lock = path.join(options.stateDir, "install.lock");
  let lockFd;
  try { lockFd = fs.openSync(lock, "wx", 0o600); } catch (e) { if (e.code === "EEXIST") refuse("another install is active or interrupted; inspect install.lock before recovery"); throw e; }
  const statePath = path.join(options.stateDir, "state.json");
  try {
    fs.writeFileSync(lockFd, JSON.stringify({ pid: process.pid })); fs.fsyncSync(lockFd);
    let state = exists(statePath) ? stateRecord(JSON.parse(regular(statePath, 256 * 1024, true))) : { schema: "cortex.installer-state.v1", project: options.project, api_port: options.apiPort || 8501, schema_revision: null, current: null, previous: null, pending: null };
    if (state.project !== options.project) refuse("state directory belongs to a different project");
    options.apiPort = options.apiPort || state.api_port;
    if (state.current && options.apiPort !== state.api_port) refuse("an installed project's API port cannot change during upgrade");
    const rollbackTarget = state.pending ? state.current : state.previous;
    if (options.rollback && !rollbackTarget) refuse("no previous verified payload to roll back to");
    const target = manifest(options.rollback ? rollbackTarget : targetInput, Boolean(options.payload) || options.rollback);
    privateDir(path.join(options.stateDir, "archives"));
    privateDir(path.join(options.stateDir, "releases"));
    const archivePath = path.join(options.stateDir, "archives", `${target.sha256}.tar.gz`);
    if (!exists(archivePath)) {
      const temporary = path.join(options.stateDir, "archives", `${crypto.randomUUID()}.download`);
      try { await download(target.url, temporary, Boolean(options.payload), get); if (digest(regular(temporary)) !== target.sha256) refuse("payload SHA-256 mismatch"); syncFile(temporary); fs.renameSync(temporary, archivePath); syncDir(path.dirname(archivePath)); }
      finally { if (exists(temporary)) fs.unlinkSync(temporary); }
    }
    const archive = regular(archivePath, MAX_ARCHIVE, true);
    if (digest(archive) !== target.sha256) refuse("cached archive SHA-256 mismatch");
    const releaseDir = path.join(options.stateDir, "releases", target.sha256);
    if (!exists(releaseDir)) {
      const stage = path.join(options.stateDir, "releases", `${crypto.randomUUID()}.stage`);
      // A failed extraction is retained for inspection; it never becomes runnable.
      extract(archive, stage); fs.renameSync(stage, releaseDir); syncDir(path.dirname(releaseDir));
    }
    verifyExtracted(archive, releaseDir);
    payloadIdentity(target, releaseDir);
    let actual = state.schema_revision;
    // The last verified runtime can read the complete retained ledger even when
    // newer migrations have run. A pending image acquisition may have failed,
    // leaving its helper unavailable; never trust cached schema as a fallback.
    const active = state.current || state.pending;
    if (active) {
      const activeArchive = regular(path.join(options.stateDir, "archives", `${active.sha256}.tar.gz`), MAX_ARCHIVE, true);
      if (digest(activeArchive) !== active.sha256) refuse("active archive SHA-256 mismatch");
      verifyExtracted(activeArchive, path.join(options.stateDir, "releases", active.sha256));
      payloadIdentity(active, path.join(options.stateDir, "releases", active.sha256));
      actual = runtime(active, "schema-status", options, null).schema_revision;
    }
    if (actual && !target.compatible_schema_revisions.includes(actual)) refuse("payload excludes the actual database schema; migrations are forward-only");
    // Acquisition changes only the image cache. Do it before the pending journal
    // so a failed first download remains retryable with no DB/writer side effects.
    runtime(target, 'prepare-images', options, null);
    if (!options.skipOpenkai && !exists(path.join(process.env.OPENKAI_HOME || path.join(os.homedir(), ".openkai"), ".env"))) {
      process.stderr.write("OpenKai is recommended for provider setup. Cortex can initialise its identical empty settings template.\n");
      if (process.stdin.isTTY) {
        const rl = require("node:readline/promises").createInterface({ input: process.stdin, output: process.stderr });
        try { const answer = await rl.question("Continue without OpenKai now? [Y/n] "); if (/^n/i.test(answer.trim())) return 2; } finally { rl.close(); }
      } else refuse("first install without existing OpenKai settings requires --skip-openkai when unattended");
    }
    const providerHome = process.env.OPENKAI_HOME || path.join(os.homedir(), ".openkai");
    const providerReceipt = path.join(options.stateDir, "provider-settings.json");
    if (exists(providerReceipt) && JSON.parse(regular(providerReceipt, 64 * 1024, true)).path !== path.join(providerHome, ".env")) refuse("installed provider authority path cannot change during upgrade");
    const provider = materialiseProvider(providerHome);
    if (!exists(providerReceipt)) atomicJSON(providerReceipt, { schema: "cortex.provider-settings.v1", path: provider.path });
    // Reinstalling code that was previously rolled back must retain the newer
    // database schema just as that rollback did.
    const preserveSchema = options.rollback || (state.current && state.current.sha256 === target.sha256 && actual !== target.schema_revision);
    const requiredSchema = preserveSchema ? actual : target.schema_revision;
    state = { ...state, api_port: options.apiPort, pending: target };
    atomicJSON(statePath, state);
    runtime(target, "up", options, requiredSchema, preserveSchema);
    const checked = runtime(target, "check", options, requiredSchema, preserveSchema);
    if (checked.schema_revision !== requiredSchema) refuse("runtime schema does not match the expected inventory");
    state = { ...state, previous: state.current && state.current.sha256 !== target.sha256 ? state.current : state.previous, current: target, pending: null, schema_revision: checked.schema_revision };
    atomicJSON(statePath, state);
    process.stdout.write(JSON.stringify({ ok: true, version: target.version, sha256: target.sha256, schema_revision: checked.schema_revision, discovery_url: checked.discovery_url, rollback: Boolean(options.rollback) }) + "\n");
    return 0;
  } finally { fs.closeSync(lockFd); fs.unlinkSync(lock); syncDir(options.stateDir); }
}
function maintenance(options) {
  if (!options.backupFile) refuse(`${options.command} requires --backup-file`);
  if (options.command === "restore" && !options.confirmRestore) refuse("restore requires explicit --confirm-restore");
  if (!path.isAbsolute(options.backupFile)) refuse("--backup-file must be absolute");
  noSymlinkParents(options.backupFile);
  if (options.command === "backup" && exists(options.backupFile)) refuse("backup destination already exists");
  if (options.command === "restore" && (!exists(options.backupFile) || !fs.lstatSync(options.backupFile).isFile())) refuse("restore requires a regular backup file");
  const statePath = path.join(options.stateDir, "state.json");
  const state = stateRecord(JSON.parse(regular(statePath, 256 * 1024, true)));
  privateDir(options.stateDir);
  if (state.project !== options.project || !state.current || state.pending) refuse("backup/restore requires a verified current installation with no pending upgrade");
  if (options.apiPort && options.apiPort !== state.api_port) refuse("API port differs from the installed project");
  options.apiPort = state.api_port;
  const lock = path.join(options.stateDir, "install.lock");
  let fd;
  try { fd = fs.openSync(lock, "wx", 0o600); } catch (e) { if (e.code === "EEXIST") refuse("another lifecycle operation is active or interrupted"); throw e; }
  try {
    fs.writeFileSync(fd, JSON.stringify({ pid: process.pid, action: options.command })); fs.fsyncSync(fd);
    const archive = regular(path.join(options.stateDir, "archives", `${state.current.sha256}.tar.gz`), MAX_ARCHIVE, true);
    if (digest(archive) !== state.current.sha256) refuse("cached archive SHA-256 mismatch");
    const directory = path.join(options.stateDir, "releases", state.current.sha256);
    verifyExtracted(archive, directory); payloadIdentity(state.current, directory);
    const result = runtime(state.current, options.command, options, state.schema_revision);
    if (options.command === "restore") runtime(state.current, "check", options, state.schema_revision);
    process.stdout.write(JSON.stringify(result) + "\n"); return 0;
  } finally { fs.closeSync(fd); fs.unlinkSync(lock); syncDir(options.stateDir); }
}
function parseArgs(argv) {
  const options = { command: argv[0] || "help", project: "cortex", stateDir: path.join(os.homedir(), ".local/share/cortex"), apiPort: null, skipOpenkai: false, rollback: false, json: false };
  for (let i = 1; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--rollback") options.rollback = true;
    else if (a === "--confirm-restore") options.confirmRestore = true;
    else if (a === "--skip-openkai") options.skipOpenkai = true;
    else if (a === "--json") options.json = true;
    else if (["--payload", "--project", "--state-dir", "--api-port", "--backup-file"].includes(a)) {
      if (!argv[i + 1] || argv[i + 1].startsWith("--")) refuse(`missing value for ${a}`);
      const value = argv[++i];
      if (a === "--payload") options.payload = value;
      if (a === "--backup-file") options.backupFile = value;
      if (a === "--project") options.project = value;
      if (a === "--state-dir") options.stateDir = value;
      if (a === "--api-port") { if (!/^\d+$/.test(value)) refuse("invalid API port"); options.apiPort = Number(value); }
    } else refuse(`unknown option ${a}`);
  }
  if (!/^[a-z][a-z0-9-]{0,62}$/.test(options.project)) refuse("invalid project name");
  if (!path.isAbsolute(options.stateDir)) refuse("--state-dir must be absolute");
  if (options.apiPort !== null && (options.apiPort < 1024 || options.apiPort > 65535)) refuse("API port must be 1024-65535");
  if (options.rollback && options.payload) refuse("rollback uses the previous verified payload, not --payload");
  if (options.command !== "install" && (options.rollback || options.payload)) refuse("payload and rollback options belong to install");
  return options;
}
async function main(argv, { get = https.get } = {}) {
  const options = parseArgs(argv);
  if (["version", "--version", "-v"].includes(options.command)) { process.stdout.write(VERSION + "\n"); return 0; }
  if (options.command === "install") return install(options, get);
  if (["backup", "restore"].includes(options.command)) return maintenance(options);
  if (options.command === "preflight") {
    const checks = preflightChecks(), ok = checks.every(c => c.ok);
    process.stdout.write(JSON.stringify({ version: VERSION, platform: os.platform(), ok, checks }, null, 2) + "\n"); return ok ? 0 : 1;
  }
  if (!["help", "--help"].includes(options.command)) refuse("unknown command");
  process.stdout.write("cortex — persistent memory and coordination\n\n  cortex preflight [--json]\n  cortex install [--payload FILE] [--skip-openkai]\n  cortex install --rollback\n  cortex backup --backup-file ABS\n  cortex restore --backup-file ABS --confirm-restore\n  cortex version\n\nLifecycle options: --state-dir ABS --project NAME --api-port PORT\nPodman >= 5.0 on Linux and macOS. Schema migrations are forward-only.\n"); return 0;
}
module.exports = { tarEntries, extract, verifyExtracted, manifest, payloadIdentity, parseURL, download, materialiseProvider, preflightChecks, parseArgs, digest, main };
if (require.main === module) main(process.argv.slice(2)).then(code => { process.exitCode = code; }).catch(error => { process.stderr.write(`cortex: REFUSED — ${error.message}\n`); process.exitCode = 2; });
