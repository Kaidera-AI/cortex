"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const zlib = require("node:zlib");
const https = require("node:https");
const { PassThrough } = require("node:stream");
const { pathToFileURL } = require("node:url");
const { spawnSync } = require("node:child_process");
const cli = require("../bin/cortex.js");
const CLI = path.resolve(__dirname, "../bin/cortex.js");
const A = "1".repeat(64), B = "2".repeat(64);
const SOURCE = "a".repeat(40);

function scratch(t) {
  // realpath handles the platform's /var -> /private/var temporary-directory alias.
  const dir = fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), "cortex-installer-test-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}
function tar(files) {
  const chunks = [];
  for (const file of files) {
    const h = Buffer.alloc(512), data = Buffer.from(file.data || "");
    h.write(file.name, 0, 100);
    h.write(`${(file.mode || 0o600).toString(8).padStart(7, "0")}\0`, 100);
    h.write("0000000\0", 108); h.write("0000000\0", 116);
    h.write(`${data.length.toString(8).padStart(11, "0")}\0`, 124);
    h.write("00000000000\0", 136);
    h.fill(32, 148, 156); h.write(file.type || "0", 156);
    h.write(file.link || "", 157, 100); h.write("ustar\0", 257); h.write("00", 263);
    const checksum = h.reduce((a, b) => a + b, 0);
    h.write(`${checksum.toString(8).padStart(6, "0")}\0 `, 148);
    chunks.push(h, data, Buffer.alloc((512 - data.length % 512) % 512));
  }
  return zlib.gzipSync(Buffer.concat([...chunks, Buffer.alloc(1024)]));
}
const RUNTIME = `#!/usr/bin/env node
const fs = require('node:fs'), path = require('node:path');
const args = process.argv.slice(2);
const get = key => args[args.indexOf(key) + 1];
const dir = get('--state-dir'), db = path.join(dir, 'fixture-db.json'), health = path.join(dir, 'fixture-health');
const schema = get('--schema-revision');
const payloadVersion = JSON.parse(fs.readFileSync(path.join(get('--payload-dir'), 'packages/deploy/release.json'))).version;
fs.appendFileSync(path.join(dir, 'fixture-actions'), JSON.stringify(args) + '\\n');
if (args[0] === 'prepare-images') {
  if (process.env.FIXTURE_MISSING_IMAGE === payloadVersion) process.exit(12);
  console.log(JSON.stringify({images_prepared: true, platform: 'linux/arm64', source_revision: '${SOURCE}'}));
} else if (args[0] === 'up') {
  if (process.env.FIXTURE_MISSING_IMAGE === payloadVersion) process.exit(12);
  if (args.includes('--rollback')) {
    if (JSON.parse(fs.readFileSync(db)).schema_revision !== schema) process.exit(5);
  } else fs.writeFileSync(db, JSON.stringify({schema_revision: schema}));
  fs.writeFileSync(health, process.env.FIXTURE_FAIL_UP ? 'unhealthy' : 'healthy');
  if (process.env.FIXTURE_FAIL_UP) process.exit(8);
} else if (args[0] === 'backup') {
  fs.copyFileSync(db, get('--backup-file'));
  console.log(JSON.stringify({backup: get('--backup-file')}));
} else if (args[0] === 'restore') {
  if (!args.includes('--confirm-restore')) process.exit(9);
  fs.copyFileSync(get('--backup-file'), db);
  console.log(JSON.stringify({restored: true}));
} else if (args[0] === 'schema-status') {
  if (process.env.FIXTURE_MISSING_IMAGE === payloadVersion) process.exit(12);
  if (process.env.FIXTURE_UNREADABLE_DB) process.exit(10);
  const actual = JSON.parse(fs.readFileSync(db));
  console.log(JSON.stringify({schema_revision: process.env.FIXTURE_INVALID_SCHEMA ? 'invalid' : actual.schema_revision}));
} else {
  if (process.env.FIXTURE_FAIL_CHECK || fs.readFileSync(health, 'utf8') !== 'healthy') process.exit(11);
  const actual = JSON.parse(fs.readFileSync(db));
  console.log(JSON.stringify({healthy: true, schema_revision: actual.schema_revision, discovery_url: 'http://127.0.0.1:' + get('--api-port') + '/.well-known/cortex'}));
}
`;
function payload(dir, version = "0.1.003", schema = A, compatible = [A, B], extra = []) {
  const archive = path.join(dir, `${version}.tar.gz`);
  const files = [
    { name: "packages/deploy/cortex-runtime", mode: 0o700, data: RUNTIME },
    { name: "packages/deploy/release.json", data: JSON.stringify({ schema: "cortex.payload.v1", version, source_revision: SOURCE, schema_revision: schema }) },
    ...['PROJECTION_MANIFEST.json', 'packages/deploy/image_manifest.py', 'packages/deploy/install-compose.json', 'packages/deploy/image-lock.json'].map(name => ({ name, data: '{}' })),
    ...extra,
  ];
  const install = JSON.stringify({ schema: 'cortex.install.v1', delivery_kind: 'prebuilt', version, source_revision: SOURCE, schema_revision: schema, platform: 'linux/arm64', files: Object.fromEntries(files.map(file => [file.name, cli.digest(Buffer.from(file.data || ''))])) });
  const bytes = tar([...files, { name: 'packages/deploy/install.json', data: install }]);
  fs.writeFileSync(archive, bytes, { mode: 0o600 });
  const value = { schema: "cortex.release.v1", version, source_revision: SOURCE, schema_revision: schema, compatible_schema_revisions: compatible, url: pathToFileURL(archive).href, sha256: cli.digest(bytes), delivery_kind: 'prebuilt', installation_sha256: cli.digest(Buffer.from(install)), platform: 'linux/arm64' };
  const manifest = path.join(dir, `${version}.json`);
  fs.writeFileSync(manifest, JSON.stringify(value));
  return { manifest, value, archive };
}
function fixture(t) {
  const dir = scratch(t), bin = path.join(dir, "bin"), state = path.join(dir, "state"), provider = path.join(dir, "openkai");
  fs.mkdirSync(bin);
  fs.writeFileSync(path.join(bin, "podman"), `#!/bin/sh
case "$*" in
  --version) echo 'podman version 5.6.0';;
  *Rootless*) echo true;;
  *CgroupManager*) echo systemd;;
  'compose version') echo 'compose 1.5.0';;
  *) exit 1;;
esac
`, { mode: 0o700 });
  fs.writeFileSync(path.join(bin, "loginctl"), "#!/bin/sh\necho yes\n", { mode: 0o700 });
  fs.writeFileSync(path.join(bin, "podman-compose"), "#!/bin/sh\necho 'podman-compose version 1.5.0'\n", { mode: 0o700 });
  const env = { ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH}`, OPENKAI_HOME: provider };
  const run = (args, more = {}) => spawnSync(process.execPath, [CLI, ...args, "--state-dir", state, "--project", "cortex-test", "--skip-openkai"], { encoding: "utf8", env: { ...env, ...more }, timeout: 30000 });
  const install = (p, more = {}) => run(["install", "--payload", p.manifest, "--api-port", "8602"], more);
  return { dir, state, provider, run, install, env };
}
function success(result) { assert.equal(result.status, 0, result.stderr); return JSON.parse(result.stdout); }
function state(f) { return JSON.parse(fs.readFileSync(path.join(f.state, "state.json"))); }
function ups(f) { return fs.readFileSync(path.join(f.state, "fixture-actions"), "utf8").trim().split("\n").map(JSON.parse).filter(args => args[0] === "up"); }
function actions(f) { return fs.readFileSync(path.join(f.state, "fixture-actions"), "utf8").trim().split("\n").map(JSON.parse); }

function transport(replies) {
  const calls = [], streams = [];
  const get = (url, options, callback) => {
    calls.push({ url: url.href, options });
    const reply = replies[Math.min(calls.length - 1, replies.length - 1)];
    const request = new PassThrough(), response = new PassThrough();
    response.statusCode = reply.status || 200;
    response.headers = reply.location === undefined ? {} : { location: reply.location };
    streams.push({ request, response });
    process.nextTick(() => {
      callback(response);
      if (reply.error) setImmediate(() => response.destroy(new Error(reply.error)));
      else if (!reply.keepOpen) response.end(reply.body || "");
    });
    return request;
  };
  return { get, calls, streams };
}

test("production install preserves no-payload refusal without touching state", t => {
  const f = fixture(t), result = f.run(["install"]);
  assert.equal(result.status, 2); assert.match(result.stderr, /REFUSED/); assert.equal(fs.existsSync(f.state), false);
  assert.equal(f.run(["install", "--rollback"]).status, 2); assert.equal(fs.existsSync(f.state), false);
});
test("legacy source-only release refuses before state or runtime execution", t => {
  const f = fixture(t), p = payload(f.dir);
  const legacy = { ...p.value };
  delete legacy.delivery_kind; delete legacy.installation_sha256;
  fs.writeFileSync(p.manifest, JSON.stringify(legacy));
  const result = f.install(p);
  assert.equal(result.status, 2);
  assert.match(result.stderr, /prebuilt/);
  assert.equal(fs.existsSync(f.state), false);
});
test('legacy cached current, previous and pending releases refuse upgrade and rollback before execution', t => {
  const f = fixture(t), old = payload(f.dir), next = payload(f.dir, '0.1.004', B);
  success(f.install(old)); success(f.install(next));
  const original = state(f), before = actions(f).length;
  for (const slot of ['current', 'previous', 'pending']) {
    const changed = structuredClone(original);
    changed[slot] ||= { ...changed.current };
    delete changed[slot].delivery_kind;
    fs.writeFileSync(path.join(f.state, 'state.json'), JSON.stringify(changed), { mode: 0o600 });
    for (const result of [f.install(next), f.run(['install', '--rollback'])]) {
      assert.equal(result.status, 2); assert.match(result.stderr, /prebuilt/);
      assert.equal(actions(f).length, before);
    }
  }
});
test('failed image acquisition never journals pending or changes database and is retryable', t => {
  const f = fixture(t), old = payload(f.dir), next = payload(f.dir, '0.1.004', B);
  assert.equal(f.install(old, { FIXTURE_MISSING_IMAGE: '0.1.003' }).status, 2);
  assert.equal(fs.existsSync(path.join(f.state, 'state.json')), false);
  assert.equal(fs.existsSync(path.join(f.state, 'fixture-db.json')), false);
  assert.equal(ups(f).length, 0);
  success(f.install(old));
  const missing = { FIXTURE_MISSING_IMAGE: '0.1.004' };
  assert.equal(f.install(next, missing).status, 2);
  assert.equal(state(f).pending, null);
  assert.equal(JSON.parse(fs.readFileSync(path.join(f.state, 'fixture-db.json'))).schema_revision, A);
  // Simulate a journal from an earlier interrupted implementation: pending
  // helper remains unavailable but current can read the actual newer ledger.
  const interrupted = state(f); interrupted.pending = next.value;
  fs.writeFileSync(path.join(f.state, 'state.json'), JSON.stringify(interrupted));
  fs.writeFileSync(path.join(f.state, 'fixture-db.json'), JSON.stringify({schema_revision: B}));
  const before = actions(f).length;
  assert.equal(success(f.run(['install', '--rollback'], missing)).schema_revision, B);
  const lookup = actions(f).slice(before).find(args => args[0] === 'schema-status');
  assert.ok(lookup.includes(path.join(f.state, 'releases', old.value.sha256)));
  assert.equal(state(f).pending, null);
  assert.equal(f.install(next, missing).status, 2);
  assert.equal(success(f.install(next)).schema_revision, B);
});
test("fresh install -> forward upgrade -> compatible rollback retains schema and private state", t => {
  const f = fixture(t), old = payload(f.dir), next = payload(f.dir, "0.1.004", B);
  assert.equal(success(f.install(old)).schema_revision, A);
  const providerBefore = fs.readFileSync(path.join(f.provider, ".env"));
  assert.equal(success(f.install(next)).schema_revision, B);
  assert.equal(success(f.run(["install", "--rollback"])).schema_revision, B);
  assert.equal(state(f).current.sha256, old.value.sha256);
  assert.equal(state(f).schema_revision, B);
  assert.equal(state(f).pending, null);
  assert.ok(ups(f).at(-1).includes("--rollback"));
  assert.deepEqual(fs.readFileSync(path.join(f.provider, ".env")), providerBefore);
  assert.equal(fs.statSync(f.state).mode & 0o777, 0o700);
  assert.equal(fs.statSync(path.join(f.state, "state.json")).mode & 0o777, 0o600);
  // Reinstalling rolled-back code must preserve the later database inventory.
  assert.equal(success(f.install(old)).schema_revision, B);
  assert.ok(ups(f).at(-1).includes("--rollback"));
});
test("rollback refuses schema incompatibility before changing the runtime", t => {
  const f = fixture(t), old = payload(f.dir, "0.1.003", A, [A]), next = payload(f.dir, "0.1.004", B);
  success(f.install(old)); success(f.install(next));
  const before = ups(f).length, result = f.run(["install", "--rollback"]);
  assert.equal(result.status, 2); assert.match(result.stderr, /forward-only/);
  assert.equal(ups(f).length, before); assert.equal(state(f).current.sha256, next.value.sha256);
});
test("digest mismatch executes nothing and preserves existing provider authority", t => {
  const f = fixture(t), p = payload(f.dir);
  fs.appendFileSync(p.archive, "tampered");
  const result = f.install(p);
  assert.equal(result.status, 2); assert.match(result.stderr, /SHA-256 mismatch/);
  assert.equal(fs.existsSync(path.join(f.state, "fixture-actions")), false);
  assert.equal(fs.existsSync(f.provider), false);
});
test("embedded release identity must match the external manifest", t => {
  const f = fixture(t), p = payload(f.dir);
  fs.writeFileSync(p.manifest, JSON.stringify({ ...p.value, version: "9.0.0" }));
  const result = f.install(p);
  assert.equal(result.status, 2); assert.match(result.stderr, /identity disagrees/);
  assert.equal(fs.existsSync(path.join(f.state, "fixture-actions")), false);
});
test("cached release modifications are refused before execution", t => {
  const f = fixture(t), p = payload(f.dir); success(f.install(p));
  fs.appendFileSync(path.join(f.state, "releases", p.value.sha256, "packages/deploy/cortex-runtime"), "\n// changed");
  const before = ups(f).length, result = f.install(p);
  assert.equal(result.status, 2); assert.match(result.stderr, /no longer matches/); assert.equal(ups(f).length, before);
});
test("failed upgrade persists pending target and can resume same exact payload", t => {
  const f = fixture(t), old = payload(f.dir), next = payload(f.dir, "0.1.004", B);
  success(f.install(old));
  const failed = f.install(next, { FIXTURE_FAIL_UP: "1" });
  assert.equal(failed.status, 2); assert.equal(state(f).pending.sha256, next.value.sha256);
  assert.equal(state(f).current.sha256, old.value.sha256);
  assert.equal(success(f.install(next)).schema_revision, B);
  assert.equal(state(f).pending, null); assert.equal(state(f).previous.sha256, old.value.sha256);
});
test("failed upgrade rollback uses the last successful payload and actual retained schema", t => {
  const f = fixture(t), old = payload(f.dir), next = payload(f.dir, "0.1.004", B);
  success(f.install(old)); assert.equal(f.install(next, { FIXTURE_FAIL_UP: "1" }).status, 2);
  assert.equal(fs.readFileSync(path.join(f.state, "fixture-health"), "utf8"), "unhealthy");
  const before = actions(f).length;
  assert.equal(success(f.run(["install", "--rollback"])).schema_revision, B);
  assert.equal(state(f).current.sha256, old.value.sha256); assert.equal(state(f).pending, null);
  assert.deepEqual(actions(f).slice(before).map(args => args[0]), ["schema-status", "prepare-images", "up", "check"]);
  assert.equal(fs.readFileSync(path.join(f.state, "fixture-health"), "utf8"), "healthy");
});
test("unreadable or invalid database inventory blocks rollback and pending retry before mutation", t => {
  const f = fixture(t), old = payload(f.dir), next = payload(f.dir, "0.1.004", B);
  success(f.install(old)); assert.equal(f.install(next, { FIXTURE_FAIL_UP: "1" }).status, 2);
  const before = ups(f).length;
  for (const env of [{ FIXTURE_UNREADABLE_DB: "1" }, { FIXTURE_INVALID_SCHEMA: "1" }]) {
    for (const result of [f.run(["install", "--rollback"], env), f.install(next, env)]) {
      assert.equal(result.status, 2); assert.match(result.stderr, /schema-status/);
      assert.equal(ups(f).length, before); assert.equal(state(f).pending.sha256, next.value.sha256);
    }
  }
});
test("successful schema discovery never replaces the required post-up health check", t => {
  const f = fixture(t), old = payload(f.dir), next = payload(f.dir, "0.1.004", B);
  success(f.install(old));
  const result = f.install(next, { FIXTURE_FAIL_CHECK: "1" });
  assert.equal(result.status, 2); assert.match(result.stderr, /runtime check failed/);
  assert.equal(state(f).pending.sha256, next.value.sha256); assert.equal(state(f).current.sha256, old.value.sha256);
});
test("backup and explicit restore use the verified current payload and preserve provider bytes", t => {
  const f = fixture(t), p = payload(f.dir); success(f.install(p));
  const file = path.join(f.dir, "backup.json"), before = fs.readFileSync(path.join(f.provider, ".env"));
  assert.equal(success(f.run(["backup", "--backup-file", file])).backup, file);
  assert.equal(f.run(["restore", "--backup-file", file]).status, 2);
  assert.equal(success(f.run(["restore", "--backup-file", file, "--confirm-restore"])).restored, true);
  assert.deepEqual(fs.readFileSync(path.join(f.provider, ".env")), before);
  assert.equal(JSON.parse(fs.readFileSync(path.join(f.state, "provider-settings.json"))).path, path.join(f.provider, ".env"));
});
test("backup refuses a tampered cached runtime and existing destination", t => {
  const f = fixture(t), p = payload(f.dir); success(f.install(p));
  const file = path.join(f.dir, "existing"); fs.writeFileSync(file, "preserve");
  assert.equal(f.run(["backup", "--backup-file", file]).status, 2);
  assert.equal(fs.readFileSync(file, "utf8"), "preserve");
  fs.appendFileSync(path.join(f.state, "releases", p.value.sha256, "packages/deploy/cortex-runtime"), "changed");
  assert.equal(f.run(["backup", "--backup-file", path.join(f.dir, "new")]).status, 2);
});
test("concurrent lock refuses without breaking the existing lock", t => {
  const f = fixture(t), p = payload(f.dir);
  fs.mkdirSync(f.state, { mode: 0o700 }); fs.writeFileSync(path.join(f.state, "install.lock"), "another owner", { mode: 0o600 });
  assert.equal(f.install(p).status, 2); assert.equal(fs.readFileSync(path.join(f.state, "install.lock"), "utf8"), "another owner");
});
test("existing OpenKai/KOS settings are byte-identical after adoption", t => {
  const dir = scratch(t), home = path.join(dir, "openkai"); fs.mkdirSync(home, { mode: 0o700 });
  const original = Buffer.from("# existing owner\nOPENROUTER_API_KEY=synthetic-test-key\n");
  fs.writeFileSync(path.join(home, ".env"), original, { mode: 0o600 });
  assert.equal(cli.materialiseProvider(home).authored, false);
  assert.deepEqual(fs.readFileSync(path.join(home, ".env")), original);
  assert.deepEqual(fs.readdirSync(home), [".env"]);
});
test("first author uses exact hash-pinned OpenKai template and separate receipt", t => {
  const dir = scratch(t), home = path.join(dir, "openkai");
  assert.equal(cli.materialiseProvider(home).authored, true);
  assert.equal(cli.digest(fs.readFileSync(path.join(home, ".env"))), "fd25fc78a15f85a47fa8bae91179dcea3892cc37f81cd42186face03e962fc64");
  assert.equal(fs.statSync(path.join(home, ".env")).mode & 0o777, 0o600);
  assert.equal(JSON.parse(fs.readFileSync(path.join(home, "cortex-provider-bootstrap.json"))).source_revision, "f3660f3c19939d2a6ff3b95be9aab3f85fb8312a");
  assert.equal(cli.materialiseProvider(home).authored, false);
});
test("provider symlink and unsafe permissions fail closed without changing bytes", t => {
  const dir = scratch(t), home = path.join(dir, "openkai"), target = path.join(dir, "target");
  fs.mkdirSync(home, { mode: 0o700 }); fs.writeFileSync(target, "outside", { mode: 0o600 });
  fs.symlinkSync(target, path.join(home, ".env")); assert.throws(() => cli.materialiseProvider(home), /symlink/);
  assert.equal(fs.readFileSync(target, "utf8"), "outside");
  fs.unlinkSync(path.join(home, ".env")); fs.writeFileSync(path.join(home, ".env"), "unchanged", { mode: 0o644 });
  assert.throws(() => cli.materialiseProvider(home), /permissions/); assert.equal(fs.readFileSync(path.join(home, ".env"), "utf8"), "unchanged");
});
test("USTAR rejects traversal, absolute paths, link escapes, PAX and special files before extraction", t => {
  const dir = scratch(t);
  for (const member of [
    { name: "../escape" }, { name: "/absolute" }, { name: "a/../../escape" }, { name: "a\\escape" },
    { name: "link", type: "2", link: "/outside" }, { name: "hard", type: "1", link: "../outside" },
    { name: "pax", type: "x" }, { name: "device", type: "3" }, { name: "fifo", type: "6" },
  ]) {
    const target = path.join(dir, "never-created");
    assert.throws(() => cli.extract(tar([{ name: "packages/deploy/cortex-runtime", mode: 0o700, data: RUNTIME }, member]), target));
    assert.equal(fs.existsSync(target), false);
  }
});
test("USTAR rejects duplicate paths, file/directory collisions, checksums and truncation", () => {
  assert.throws(() => cli.tarEntries(tar([{ name: "a" }, { name: "a" }])), /duplicate/);
  assert.throws(() => cli.tarEntries(tar([{ name: "a" }, { name: "a/b" }])), /collision/);
  const raw = zlib.gunzipSync(tar([{ name: "a" }])); raw[0] ^= 1;
  assert.throws(() => cli.tarEntries(zlib.gzipSync(raw)), /checksum/);
  assert.throws(() => cli.tarEntries(zlib.gzipSync(raw.subarray(0, 400))), /truncated/);
});
test("payload URL validation refuses insecure and ambiguous sources", () => {
  for (const url of ["http://host/archive", "https://user:secret@host/file", "https://host/file#hash", "file:///tmp/archive", "ftp://host/archive", "/relative", "https://host/\nfile", "https://host/a b", "https://host/\u007f"])
    assert.throws(() => cli.parseURL(url, false));
  assert.equal(cli.parseURL("https://github.com/Kaidera-AI/cortex/releases/download/v/archive.tar.gz", false).protocol, "https:");
});
test("HTTPS asset redirects preserve bytes and close every transport without forwarding credentials", async t => {
  const dir = scratch(t), target = path.join(dir, "download"), bytes = Buffer.from("verified archive bytes");
  const remote = transport([
    { status: 302, location: "https://release-assets.githubusercontent.com/path?signature=synthetic" },
    { status: 307, location: "/final" },
    { body: bytes },
  ]);
  await cli.download("https://github.com/Kaidera-AI/cortex/releases/download/v/archive.tar.gz", target, false, remote.get);
  assert.deepEqual(fs.readFileSync(target), bytes);
  assert.equal(cli.digest(fs.readFileSync(target)), cli.digest(bytes));
  assert.equal(remote.calls.at(-1).url, "https://release-assets.githubusercontent.com/final");
  assert.ok(remote.calls.every(call => call.options.headers === undefined));
  assert.ok(remote.streams.every(({ request, response }) => request.destroyed && response.destroyed));
  assert.equal(fs.statSync(target).mode & 0o777, 0o600);
});
test("HTTPS redirects refuse downgrade, credentials, fragments, control characters and missing hosts", async t => {
  const dir = scratch(t);
  for (const location of ["http://host/file", "file:///tmp/archive", "https://user:secret@host/file", "https://host/file#fragment", "https://host/\nfile", "https://host/a b", "https://", "", undefined]) {
    const remote = transport([{ status: 302, location }]), target = path.join(dir, "absent");
    await assert.rejects(cli.download("https://github.com/asset", target, false, remote.get));
    assert.equal(remote.calls.length, 1); assert.equal(fs.existsSync(target), false);
    assert.ok(remote.streams.every(({ request, response }) => request.destroyed && response.destroyed));
  }
});
test("HTTPS redirect loops are bounded to five hops and HTTP errors stop their bodies", async t => {
  const dir = scratch(t), loop = transport([{ status: 302, location: "/loop", keepOpen: true }]);
  await assert.rejects(cli.download("https://host/loop", path.join(dir, "absent"), false, loop.get), /redirect limit/);
  assert.equal(loop.calls.length, 6);
  assert.ok(loop.streams.every(({ request, response }) => request.destroyed && response.destroyed));
  const denied = transport([{ status: 403, keepOpen: true }]);
  await assert.rejects(cli.download("https://host/asset", path.join(dir, "absent"), false, denied.get), /HTTP 200/);
  assert.ok(denied.streams.every(({ request, response }) => request.destroyed && response.destroyed));
});
test("HTTPS interrupted bodies and destination errors close all active streams", async t => {
  const dir = scratch(t), interrupted = transport([{ error: "interrupted body" }]);
  await assert.rejects(cli.download("https://host/asset", path.join(dir, "partial"), false, interrupted.get), /interrupted body/);
  assert.ok(interrupted.streams.every(({ request, response }) => request.destroyed && response.destroyed));
  const target = path.join(dir, "existing"), stalled = transport([{ keepOpen: true }]);
  fs.writeFileSync(target, "preserve", { mode: 0o600 });
  await assert.rejects(cli.download("https://host/asset", target, false, stalled.get), /EEXIST/);
  assert.equal(fs.readFileSync(target, "utf8"), "preserve");
  assert.ok(stalled.streams.every(({ request, response }) => request.destroyed && response.destroyed));
});
test("redirected payload digest mismatch is refused before extraction or runtime execution", async t => {
  const f = fixture(t), p = payload(f.dir), remote = transport([{ status: 302, location: "https://assets.example/archive" }, { body: "tampered" }]);
  fs.writeFileSync(p.manifest, JSON.stringify({ ...p.value, url: "https://github.com/asset" }));
  const originalPath = process.env.PATH, originalHome = process.env.OPENKAI_HOME;
  try {
    process.env.PATH = f.env.PATH; process.env.OPENKAI_HOME = f.provider;
    // Inject the transport explicitly: mutating a builtin module export is not
    // portable between Node and Bun and can accidentally make a real request.
    await assert.rejects(cli.main(["install", "--payload", p.manifest, "--state-dir", f.state, "--project", "cortex-test", "--skip-openkai"], { get: remote.get }), /SHA-256 mismatch/);
  } finally {
    process.env.PATH = originalPath;
    if (originalHome === undefined) delete process.env.OPENKAI_HOME; else process.env.OPENKAI_HOME = originalHome;
  }
  assert.equal(fs.existsSync(path.join(f.state, "fixture-actions")), false);
  assert.deepEqual(fs.readdirSync(path.join(f.state, "releases")), []);
  assert.deepEqual(fs.readdirSync(path.join(f.state, "archives")), []);
});
test("Podman-only preflight supports Linux and macOS without Apple Container", () => {
  const calls = [], run = (cmd, args) => {
    calls.push(cmd);
    return { ok: true, out: cmd === "python3" ? "Python 3.12.0" : cmd === "loginctl" ? "yes" : args[0] === "--version" ? "podman version 5.6.0" : args.includes("{{.Host.Security.Rootless}}") ? "true" : args.includes("{{.Host.CgroupManager}}") ? "systemd" : "compose 1.5" };
  };
  assert.ok(cli.preflightChecks("darwin", run).every(x => x.ok));
  assert.ok(cli.preflightChecks("linux", run).every(x => x.ok));
  assert.equal(calls.includes("container"), false);
  assert.equal(cli.preflightChecks("win32", run)[0].ok, false);
  assert.equal(cli.preflightChecks("linux", () => ({ ok: true, out: "podman version 4.9.3" }))[0].ok, false);
});
