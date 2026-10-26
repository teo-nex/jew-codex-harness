import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { callerBaseUrl } from "../src/caller-auth.mjs";
import { openPort } from "./port-pool.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const INTERNAL_KEY = "test-internal-service-key-with-sufficient-length";
const CALLER_KEY = "test-router-caller-capability-with-sufficient-length";

async function mockServer(handler) {
  const server = http.createServer(handler);
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  return { server, port: server.address().port };
}

// A stand-in for chatgpt.com that only counts. An idle router must answer
// locally, so the count staying at zero is the whole point of this file.
async function nativeRecorder() {
  const hits = [];
  const { server, port } = await mockServer((request, response) => {
    hits.push(request.url);
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end('{"id":"native-response"}');
  });
  return { server, port, hits };
}

function run(env, prepare = () => {}) {
  const testRoot = mkdtempSync(path.join(os.tmpdir(), "codex-router-idle-"));
  const stateDir = path.join(testRoot, "state");
  mkdirSync(stateDir, { recursive: true, mode: 0o700 });
  // The state an idle install leaves behind: an explicit empty selection.
  writeFileSync(
    path.join(stateDir, "enabled-providers.json"),
    '{"version":1,"providers":[]}\n',
    { encoding: "utf8", mode: 0o600 },
  );
  prepare({ testRoot, stateDir });
  const child = spawn(process.execPath, [path.join(root, "src", "router.mjs")], {
    cwd: root,
    env: {
      ...process.env,
      CODEX_HOME: path.join(testRoot, "codex"),
      MODEL_ROUTER_STATE_DIR: stateDir,
      MODEL_ROUTER_CODEX_AUTH: path.join(testRoot, "codex", "auth.json"),
      CODEX_ROUTER_CALLER_KEY: CALLER_KEY,
      CODEX_ROUTER_INTERNAL_KEY: INTERNAL_KEY,
      KIMI_INTERNAL_KEY: INTERNAL_KEY,
      CODEX_ROUTER_QUIET: "1",
      ...env,
    },
    stdio: ["ignore", "ignore", "pipe"],
  });
  child.stderr.setEncoding("utf8");
  let errors = "";
  child.stderr.on("data", (chunk) => {
    errors += chunk;
  });
  child.testErrors = () => errors;
  child.testRoot = testRoot;
  return child;
}

async function waitFor(url, child) {
  const deadline = Date.now() + 5_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`Child exited early (${child.exitCode}): ${child.testErrors()}`);
    }
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch {
      // Not bound yet.
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`Timed out waiting for ${url}: ${child.testErrors()}`);
}

async function stopChild(child) {
  if (child.exitCode === null && child.signalCode === null) {
    child.kill("SIGTERM");
    await new Promise((resolve) => child.once("exit", resolve));
  }
  rmSync(child.testRoot, { recursive: true, force: true });
}

function post(port, pathname, body) {
  const base = new URL(callerBaseUrl(port, CALLER_KEY));
  return fetch(`http://127.0.0.1:${port}${base.pathname}${pathname}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function routerEnv(gatewayPort, routerPort, nativePort) {
  return {
    CODEX_ROUTER_PORT: String(routerPort),
    CODEX_ROUTER_GATEWAY_BASE_URL: `http://127.0.0.1:${gatewayPort}/v1`,
    CODEX_ROUTER_OAUTH_HEALTH_URL: `http://127.0.0.1:${gatewayPort}/health`,
    CODEX_ROUTER_API_HEALTH_URL: `http://127.0.0.1:${gatewayPort}/health`,
    CODEX_ROUTER_GROK_OAUTH_HEALTH_URL: `http://127.0.0.1:${gatewayPort}/health`,
    CODEX_ROUTER_GATEWAY_HEALTH_URL: `http://127.0.0.1:${gatewayPort}/health`,
    CODEX_NATIVE_BASE_URL: `http://127.0.0.1:${nativePort}`,
  };
}

test("an idle --no-discovery router answers locally and never calls the native base", async () => {
  const gateway = await mockServer((request, response) => {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end('{"ok":true}');
  });
  const native = await nativeRecorder();
  const routerPort = await openPort();
  const child = run({
    ...routerEnv(gateway.port, routerPort, native.port),
    CODEX_ROUTER_NO_DISCOVERY: "1",
  });
  try {
    // Zero providers keeps /health ok: only the gateway is probed.
    await waitFor(`http://127.0.0.1:${routerPort}/health`, child);

    const turn = await post(routerPort, "/responses", { model: "gpt-5", input: "hello" });
    assert.equal(turn.status, 503);
    const payload = await turn.json();
    assert.equal(payload.error.type, "router_idle_no_provider");
    assert.match(payload.error.message, /no traffic leaves this machine/i);

    const image = await post(routerPort, "/images/generations", { prompt: "a test" });
    assert.equal(image.status, 503);
    assert.equal((await image.json()).error.type, "router_idle_no_provider");

    assert.deepEqual(native.hits, [], "an idle router reached for the native base");
  } finally {
    await stopChild(child);
    gateway.server.close();
    native.server.close();
  }
});

test("hiding every provider without --no-discovery keeps native passthrough working", async () => {
  const gateway = await mockServer((request, response) => {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end('{"ok":true}');
  });
  const native = await nativeRecorder();
  const routerPort = await openPort();
  // Same empty selection, discovery untouched: this is the pre-existing "hide
  // everything" state, and its native fallthrough must not regress.
  const child = run(routerEnv(gateway.port, routerPort, native.port));
  try {
    await waitFor(`http://127.0.0.1:${routerPort}/health`, child);
    const turn = await post(routerPort, "/responses", { model: "gpt-5", input: "hello" });
    assert.equal(turn.status, 200, await turn.text());
    assert.equal(native.hits.length, 1, "the native turn never reached the native base");
  } finally {
    await stopChild(child);
    gateway.server.close();
    native.server.close();
  }
});

test("a running signed Codex route observes discovery changes from the control process", async () => {
  const gateway = await mockServer((request, response) => {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end('{"ok":true}');
  });
  const native = await nativeRecorder();
  const routerPort = await openPort();
  const token = "fixture-signed-codex-session";
  const child = run({
    ...routerEnv(gateway.port, routerPort, native.port),
    CODEX_ROUTER_NO_DISCOVERY: "",
    CODEX_ROUTER_NATIVE_SESSION_FALLBACK: "0",
  }, ({ testRoot, stateDir }) => {
    const home = path.join(testRoot, "codex");
    mkdirSync(home, { recursive: true, mode: 0o700 });
    writeFileSync(path.join(home, "auth.json"), JSON.stringify({
      auth_mode: "chatgpt",
      tokens: { access_token: token, account_id: "fixture-account" },
    }), { mode: 0o600 });
    writeFileSync(path.join(stateDir, "discovery-mode.json"),
      '{"version":1,"discovery":"disabled"}\n', { mode: 0o600 });
  });
  const turn = (bearer = token) => fetch(`http://127.0.0.1:${routerPort}/v1/responses`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${bearer}` },
    body: JSON.stringify({ model: "gpt-5", input: "synthetic connectivity check" }),
  });
  const setDiscovery = (mode) => execFileSync(process.execPath,
    [path.join(root, "src/discovery-mode.mjs"), "set", mode], {
      env: {
        ...process.env,
        CODEX_HOME: path.join(child.testRoot, "codex"),
        MODEL_ROUTER_STATE_DIR: path.join(child.testRoot, "state"),
        CODEX_ROUTER_NO_DISCOVERY: "",
      },
      stdio: "pipe",
    });
  try {
    await waitFor(`http://127.0.0.1:${routerPort}/health`, child);
    const disabled = await turn();
    assert.equal(disabled.status, 401);
    await disabled.text();
    assert.deepEqual(native.hits, []);

    setDiscovery("enabled");
    const enabled = await turn();
    assert.equal(enabled.status, 200, await enabled.text());
    assert.equal(native.hits.length, 1, "the signed Codex turn never reached the local native stand-in");

    const wrong = await turn("fixture-unrelated-session");
    assert.equal(wrong.status, 401);
    await wrong.text();
    const anonymous = await fetch(`http://127.0.0.1:${routerPort}/v1/responses`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: "gpt-5", input: "hello" }),
    });
    assert.equal(anonymous.status, 401);
    await anonymous.text();
    assert.equal(native.hits.length, 1, "an unrelated bearer reached the native base");

    setDiscovery("disabled");
    const revoked = await turn();
    assert.equal(revoked.status, 401);
    await revoked.text();
    const idle = await post(routerPort, "/responses", { model: "gpt-5", input: "hello" });
    assert.equal(idle.status, 503);
    assert.equal((await idle.json()).error.type, "router_idle_no_provider");
    assert.equal(native.hits.length, 1, "revoked discovery still reached the native base");
  } finally {
    await stopChild(child);
    gateway.server.close();
    native.server.close();
  }
});
