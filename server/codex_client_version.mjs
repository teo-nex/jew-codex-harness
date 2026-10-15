// Match a terminal launch before falling back to a bundled desktop CLI.
// The shared resolver still owns platform-specific spawning and shim avoidance.
import { existsSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { codexCandidatePaths, codexVersion } from "../router/src/codex-binary.mjs";
import { isShimFile, resolveRealCodex } from "../router/src/codex-shim.mjs";
import { commandOnPath } from "../router/src/spawnable-command.mjs";

const installed = process.env.CODEX_INSTALL_DIR
  ? path.join(process.env.CODEX_INSTALL_DIR, process.platform === "win32" ? "codex.exe" : "codex")
  : undefined;
const terminalBins = [path.join(os.homedir(), ".local", "bin"),
  ...(process.platform === "win32" ? [] : ["/opt/homebrew/bin", "/usr/local/bin"])];
const candidate = [process.env.CODEX_BIN, installed, commandOnPath("codex"),
  process.platform !== "win32" ? resolveRealCodex()?.file : undefined,
  ...terminalBins.flatMap((directory) => codexCandidatePaths().filter((file) => path.dirname(file) === directory))]
  .find((file) => file && existsSync(file) && !isShimFile(file));
if (candidate) process.env.CODEX_BIN = candidate;
process.stdout.write(codexVersion() || "");
