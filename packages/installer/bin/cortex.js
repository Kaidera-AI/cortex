#!/usr/bin/env node
/**
 * cortex — one launcher for every install channel (brew, npm, bun).
 *
 * Zero runtime dependencies, on purpose: the same file must behave identically under
 * node >= 18 and bun, from a brew wrapper, npx, or bunx. Behaviour differences between
 * channels are install-stream bugs by definition, so there is exactly one implementation.
 *
 * Contract (design 23 §5d): `preflight` works today and reports honestly; `install`
 * refuses loudly while the v0.1.0 payload does not exist — a published launcher must
 * never pretend.
 */
"use strict";

const { execFileSync } = require("node:child_process");
const os = require("node:os");

const VERSION = "0.0.1";
// Flipped by the release pipeline when the v0.1.0 payload (wheel + images + compose)
// is published with digests. Until then `install` must fail loudly.
const PAYLOAD = null; // e.g. { version: "0.1.0", sha256: "...", url: "https://github.com/Kaidera-AI/cortex/releases/..." }

function run(cmd, args) {
  try {
    return { ok: true, out: execFileSync(cmd, args, { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim() };
  } catch (err) {
    return { ok: false, out: String((err && (err.stdout || err.message)) || "").trim() };
  }
}

function check(name, ok, detail, remedy) {
  return { name, ok, detail, ...(ok ? {} : { remedy }) };
}

function preflightLinux() {
  const checks = [];
  const podman = run("podman", ["--version"]);
  if (!podman.ok) {
    checks.push(check("podman", false, "podman not found",
      "install podman >= 5.0 — Ubuntu 24.04 ships 4.9.3 which is TOO OLD; use a newer distro repo or upstream builds"));
    return checks; // everything else depends on the engine
  }
  const version = (podman.out.match(/(\d+)\.(\d+)\.(\d+)/) || []).slice(1).map(Number);
  checks.push(check("podman >= 5.0", version.length === 3 && version[0] >= 5, podman.out,
    "podman update --restart does not exist before 5.0; the appliance cannot arm reboot recovery"));
  const cgroup = run("podman", ["info", "--format", "{{.Host.CgroupManager}}"]);
  checks.push(check("cgroup manager systemd", cgroup.ok && cgroup.out === "systemd", cgroup.out || "unknown",
    "without systemd cgroups healthcheck timers never schedule and every service_healthy waits forever"));
  const rootless = run("podman", ["info", "--format", "{{.Host.Security.Rootless}}"]);
  checks.push(check("rootless", rootless.ok && rootless.out === "true", rootless.out || "unknown",
    "rootless is the supported posture"));
  const uid = typeof process.getuid === "function" ? String(process.getuid()) : "";
  const linger = run("loginctl", ["show-user", uid, "-p", "Linger", "--value"]);
  checks.push(check("linger enabled", linger.ok && linger.out === "yes", linger.out || "unknown",
    `loginctl enable-linger ${uid} — without it the stack does not survive reboot without a login`));
  return checks;
}

function preflightMac() {
  const checks = [];
  const container = run("container", ["--version"]);
  if (container.ok) {
    checks.push(check("Apple Container", true, container.out));
    const sys = run("container", ["system", "status"]);
    checks.push(check("container services running", sys.ok, sys.ok ? "running" : sys.out,
      "container system start"));
  } else {
    checks.push(check("Apple Container", false, "the `container` tool is not installed",
      "the installer will install it at `cortex install` (v0.1.0); today: https://github.com/apple/container/releases"));
  }
  // One containerisation technology per machine — mixed engines are an install-stream refusal.
  const podman = run("podman", ["--version"]);
  checks.push(check("no second engine", !podman.ok, podman.ok ? `podman also present: ${podman.out}` : "clean",
    "macOS runs Apple Container for everything; remove podman from this machine"));
  return checks;
}

function preflight(json) {
  const platform = os.platform();
  let checks;
  if (platform === "linux") checks = preflightLinux();
  else if (platform === "darwin") checks = preflightMac();
  else checks = [check("platform", false, platform, "Cortex supports macOS (Apple Container) and Linux (rootless podman)")];
  const ok = checks.every((c) => c.ok);
  if (json) {
    process.stdout.write(JSON.stringify({ version: VERSION, platform, ok, checks }, null, 2) + "\n");
  } else {
    for (const c of checks) {
      process.stdout.write(`  ${c.ok ? "ok " : "FAIL"}  ${c.name}: ${c.detail}\n`);
      if (!c.ok && c.remedy) process.stdout.write(`        -> ${c.remedy}\n`);
    }
    process.stdout.write(ok ? "\npreflight: PASS\n" : "\npreflight: FAIL (fix the items above; nothing was changed)\n");
  }
  return ok ? 0 : 1;
}

function install() {
  if (!PAYLOAD) {
    process.stderr.write(
      "cortex install: REFUSED — the v0.1.0 release payload does not exist yet.\n" +
      "This launcher ships ahead of the extraction (github.com/Kaidera-AI/cortex, ROADMAP.md)\n" +
      "so channels and preflight can be proven early. It will never deploy a stack it cannot\n" +
      "verify by digest. Run `cortex preflight` to prepare this machine.\n"
    );
    return 2;
  }
  // v0.1.0: verify digest -> deploy six layers -> doctor -> print discovery URL.
  return 0;
}

function main(argv) {
  const [cmd, ...rest] = argv;
  const json = rest.includes("--json");
  switch (cmd) {
    case "preflight": return preflight(json);
    case "install": return install();
    case "version": case "--version": case "-v":
      process.stdout.write(VERSION + "\n"); return 0;
    default:
      process.stdout.write(
        "cortex — persistent memory and coordination for AI agent teams\n\n" +
        "  cortex preflight [--json]   check this machine against the deployment contract\n" +
        "  cortex install              deploy the six-layer appliance (v0.1.0 payload required)\n" +
        "  cortex version\n\n" +
        "Docs: https://github.com/Kaidera-AI/cortex\n"
      );
      return cmd && cmd !== "help" && cmd !== "--help" ? 1 : 0;
  }
}

process.exit(main(process.argv.slice(2)));
