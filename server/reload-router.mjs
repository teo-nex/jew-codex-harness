// The base router starts before Jev's user-model overlay is registered.
// Publish that overlay into the owned, idle process before reporting success.
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { CODEX_HOME, STATE_DIR } from "../router/src/paths.mjs";
import { callerBaseUrl } from "../router/src/caller-auth.mjs";
import { readServiceProcessState, serviceProcessOwns } from "../router/src/service-process.mjs";

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const router = path.join(repo, "router");

export function assertOwned() {
  const journal = JSON.parse(readFileSync(path.join(CODEX_HOME, "jev-harness/journal.json"), "utf8"));
  if (!journal.router_owned || journal.repo !== repo || journal.codex_home !== path.resolve(CODEX_HOME)) {
    throw new Error("Cannot prove this installation owns the base router");
  }
  if (process.platform === "win32") {
    const script = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); " +
      "$ErrorActionPreference='Stop'; " +
      "@((Get-ScheduledTask -TaskName 'Codex Router' -TaskPath '\\').Actions | " +
      "Select-Object Execute,Arguments,WorkingDirectory) | ConvertTo-Json -Compress";
    const output = execFileSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", script],
      { encoding: "utf8", timeout: 15_000, stdio: ["ignore", "pipe", "pipe"] });
    const parsed = JSON.parse(output);
    const actions = Array.isArray(parsed) ? parsed : [parsed];
    if (actions.length !== 1 || actions[0].Execute !== "wscript.exe" ||
        actions[0].Arguments !== `//B //NoLogo "${path.join(STATE_DIR, "start-codex-router-hidden.vbs")}"` ||
        actions[0].WorkingDirectory || !serviceProcessOwns(readServiceProcessState())) {
      throw new Error("Base router task or process belongs to another installation");
    }
  } else {
    const definition = process.platform === "darwin"
      ? path.join(os.homedir(), "Library/LaunchAgents/io.github.codex-router.plist")
      : path.join(process.env.XDG_CONFIG_HOME || path.join(os.homedir(), ".config"), "systemd/user/codex-router.service");
    if (journal.router_definition_path !== definition ||
        createHash("sha256").update(readFileSync(definition)).digest("hex") !== journal.router_definition_sha256) {
      throw new Error("Base router service definition changed; refusing reload");
    }
  }
}

export async function readHealth() {
  try {
    const key = readFileSync(path.join(STATE_DIR, "caller-secret"), "utf8").trim();
    const response = await fetch(`${callerBaseUrl(4202, key)}/health`,
      { redirect: "error", signal: AbortSignal.timeout(5000) });
    if (!response.ok) throw new Error("unhealthy");
    return await response.json();
  } catch {
    // Never include the capability-bearing URL in an error.
    throw new Error("Cannot verify the owned router is idle");
  }
}

function restart() {
  try {
    execFileSync(process.execPath, [path.join(router, "src/service.mjs"), "restart"],
      { cwd: router, timeout: 330_000, stdio: ["ignore", "pipe", "pipe"] });
  } catch {
    throw new Error("Owned router reload failed; inspect its service log and resume installation");
  }
}

export async function reloadInstalledRoutes(deps = { assertOwned, readHealth, restart }) {
  await deps.assertOwned();
  const health = await deps.readHealth();
  if (health?.ok !== true || health.service !== "codex-router" ||
      health.activity?.activeCount !== 0 || health.resources?.inFlightRequests !== 0) {
    throw new Error("Router is busy or its activity is unknown; retry installation when idle");
  }
  await deps.restart();
  return { reloaded: true };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    process.stdout.write(`${JSON.stringify(await reloadInstalledRoutes())}\n`);
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
