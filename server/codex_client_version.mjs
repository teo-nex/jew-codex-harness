// Match a terminal launch before falling back to a bundled desktop CLI.
// The shared resolver still owns platform-specific spawning and shim avoidance.
import { existsSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { codexCandidatePaths, codexVersion } from "../router/src/codex-binary.mjs";
import { isShimFile } from "../router/src/codex-shim.mjs";
import { commandOnPath } from "../router/src/spawnable-command.mjs";

const installed = process.env.CODEX_INSTALL_DIR
  ? path.join(process.env.CODEX_INSTALL_DIR, process.platform === "win32" ? "codex.exe" : "codex")
  : undefined;
const localBin = path.join(os.homedir(), ".local", "bin");
const candidate = [process.env.CODEX_BIN, installed, commandOnPath("codex"),
  ...codexCandidatePaths().filter((file) => path.dirname(file) === localBin)]
  .find((file) => file && existsSync(file) && !isShimFile(file));
if (candidate) process.env.CODEX_BIN = candidate;
process.stdout.write(codexVersion() || "");
